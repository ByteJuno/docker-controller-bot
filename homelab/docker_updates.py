"""Daily registry checks and explicitly approved Docker updates through Portainer."""
import copy, contextlib, fcntl, hashlib, json, os, pathlib, re, secrets, sqlite3
import ssl, sys, threading, time, urllib.parse, urllib.request
import requests
if __package__ in (None, ""):
    # Run as a script (python3 /app/homelab/docker_updates.py --scan): make the package importable.
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from homelab import paths as homelab_paths
from datetime import datetime, timezone

ROOT=pathlib.Path(os.environ.get('UPDATE_STATE_DIR') or homelab_paths.data_file('image-updates'))
ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
PROTECTED={'docker-controller-bot':'定制机器人需合并代码更新','portainer':'管理入口需单独维护',
           'portainer_edge_agent':'Agent 与 Portainer 版本需配套','n8n-postgres':'数据库需单独维护'}
# 带上游同名标签 DCB-Auto-Update 的容器：发现新镜像后不等按钮，在每日检查末尾直接更新（2026-10-03）。
# 沿用上游的标签名而不是自起一个，换到上游 5.0 时 compose 不用改。上游自带的查新（CHECK_UPDATES）仍关闭：
# 它每次都要拉镜像、4.2.0 只管本机，也没有健康检查和回退。这里的流程与手动确认完全相同
# （校验镜像、等健康、失败恢复旧容器、旧容器保留 2 天）；某个版本自动更新失败后不再重试，改发确认按钮。
AUTO_LABEL='DCB-Auto-Update'
ACCEPT=', '.join(['application/vnd.oci.image.index.v1+json','application/vnd.docker.distribution.manifest.list.v2+json',
                 'application/vnd.oci.image.manifest.v1+json','application/vnd.docker.distribution.manifest.v2+json'])

def db():
    c=sqlite3.connect(ROOT/'state.sqlite',timeout=30);c.row_factory=sqlite3.Row
    c.execute('CREATE TABLE IF NOT EXISTS candidates (id TEXT PRIMARY KEY,eid INTEGER,cid TEXT,target TEXT,data TEXT,status TEXT,expires REAL,notified INTEGER DEFAULT 0)')
    c.execute('CREATE TABLE IF NOT EXISTS approvals (id TEXT PRIMARY KEY,candidate TEXT,uid TEXT,chat TEXT,expires REAL,used INTEGER DEFAULT 0)')
    c.commit();os.chmod(ROOT/'state.sqlite',0o600);return c

class API:
    def __init__(self):
        self.cfg=json.loads(homelab_paths.data_file('portainer-multihost.json').read_text())
        self.envs={int(k):v for k,v in self.cfg['environments'].items()}
        self.ctx=ssl.create_default_context(cafile=homelab_paths.certificate(self.cfg['certificate']));self.ctx.check_hostname=False
    def call(self,eid,path,method='GET',data=None,raw=False,timeout=90):
        if eid not in self.envs:raise ValueError('Unknown environment')
        request=urllib.request.Request(self.cfg['url'].rstrip('/')+f'/api/endpoints/{eid}/docker'+path,
            data=json.dumps(data).encode() if data is not None else None,method=method,
            headers={'X-API-Key':self.cfg['api_key'],'Content-Type':'application/json'})
        with urllib.request.urlopen(request,context=self.ctx,timeout=timeout) as f:b=f.read()
        return b if raw else json.loads(b) if b else None

def refparts(ref):
    if ref.startswith('sha256:') or '@' in ref:raise ValueError('固定摘要镜像，不自动追踪标签')
    first,*rest=ref.split('/')
    if rest and ('.' in first or ':' in first or first=='localhost'):host,path=first,'/'.join(rest)
    else:host,path='registry-1.docker.io',ref
    if host in ('docker.io','index.docker.io'):host='registry-1.docker.io'
    if host=='registry-1.docker.io' and '/' not in path:path='library/'+path
    last=path.rsplit('/',1)[-1]
    if ':' in last:path,tag=path.rsplit(':',1)
    else:tag='latest'
    return host,path,tag

def remote_image(ref,local):
    host,path,tag=refparts(ref);s=requests.Session();headers={'Accept':ACCEPT}
    def get(reference):
        url=f'https://{host}/v2/{path}/manifests/{reference}'
        r=s.get(url,headers=headers,timeout=25)
        if r.status_code==401:
            challenge=r.headers.get('WWW-Authenticate','')
            vals=dict(re.findall(r'(\w+)="([^"]+)"',challenge))
            if not challenge.lower().startswith('bearer ') or not vals.get('realm','').startswith('https://'):
                raise ValueError('仓库要求登录')
            t=s.get(vals['realm'],params={k:v for k,v in vals.items() if k in ('service','scope')},timeout=25)
            t.raise_for_status();j=t.json();headers['Authorization']='Bearer '+(j.get('token') or j['access_token'])
            r=s.get(url,headers=headers,timeout=25)
        r.raise_for_status();return r.json(),r.headers.get('Docker-Content-Digest') or 'sha256:'+hashlib.sha256(r.content).hexdigest()
    try:
        manifest,digest=get(tag)
        if 'manifests' in manifest:
            arch,system,variant=local['Architecture'],local.get('Os','linux'),local.get('Variant','')
            choices=[m for m in manifest['manifests'] if m.get('platform',{}).get('architecture')==arch and m['platform'].get('os')==system]
            if variant:choices=[m for m in choices if m['platform'].get('variant','')==variant]
            if len(choices)!=1:raise ValueError('无法唯一匹配当前镜像平台')
            manifest,digest=get(choices[0]['digest'])
        config=manifest.get('config',{}).get('digest','')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',config):raise ValueError('不支持的镜像清单')
        return {'config':config,'digest':digest,'ref':ref,'platform':local['Architecture']}
    finally:s.close()

def resolve_image(api,eid,ref,local):
    # Modern containerd engines identify images by the distribution descriptor.
    if local.get('Descriptor',{}).get('digest'):
        descriptor=api.call(eid,'/distribution/'+urllib.parse.quote(ref,safe='/:@')+'/json')['Descriptor']
        digest=descriptor['digest']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',digest):raise ValueError('无效远程摘要')
        return {'config':digest,'digest':digest,'ref':ref,'platform':local['Architecture'],'kind':'descriptor'}
    return remote_image(ref,local)

def matches(local,target):
    if target.get('kind')=='descriptor':return local.get('Descriptor',{}).get('digest')==target['digest']
    return local['Id']==target['config']

# 连接断开、超时、429/5xx 隔 2 秒、5 秒各重试一次。读超时后重发可能多出一条重复消息，可以接受。
TELEGRAM_RETRY_DELAYS=(2,5)

def telegram(text,buttons=None,chat=None):
    data={'chat_id':chat or os.environ['TELEGRAM_ADMIN'].split(',')[0],'text':text[:4000]}
    if buttons:data['reply_markup']={'inline_keyboard':buttons}
    for delay in (*TELEGRAM_RETRY_DELAYS,None):
        try:r=requests.post('https://api.telegram.org/bot'+os.environ['TELEGRAM_TOKEN']+'/sendMessage',json=data,timeout=25)
        except (requests.ConnectionError,requests.Timeout):
            if delay is None:raise
            time.sleep(delay);continue
        if delay is None or not (r.status_code==429 or r.status_code>=500):break
        time.sleep(delay)
    if not r.ok:raise RuntimeError('Telegram发送失败 HTTP '+str(r.status_code))
    if not r.json().get('ok'):raise RuntimeError('Telegram未确认发送')
    return r.json()['result']['message_id']

def tell(text,chat=None):
    """只告知结果的消息：发不出去记一行日志，不改变已经发生的事。
    2026-10-22 rsshub 自动更新其实成功了，只是「已自动更新」那条赶上 Telegram 断开连接，
    异常冒到 worker 的 except，把换好的更新标成 failed、发了「更新未完成」，回退容器也就不会被清理。"""
    try:return telegram(text,chat=chat)
    except Exception as ex:print('Telegram 通知未发出（'+type(ex).__name__+'）：'+text.split('\n',1)[0],file=sys.stderr,flush=True)

def exclusion(c):
    name=c['Name'].lstrip('/');labels=c['Config'].get('Labels') or {};ref=labels.get('homelab.update.image') or c['Config']['Image']
    if '.__rollback__.' in name:return '保留的回退容器'
    if name in PROTECTED:return PROTECTED[name]
    # 本机构建、不在任何仓库里的镜像：查仓库标签只会报 HTTPError。
    # local/yyb-go 由 /opt/panels/yyb-go/auto_update.py 每天跟随上游源码重建。
    # course-platform:local 这种「:local」标签同理（Osaka Lab 本机构建，每天检查都报 HTTPError，2026-10-03 加）。
    if ref.startswith(('homelab/','local/')) or ref.endswith(':local'):return '本地构建镜像，不追踪仓库标签'
    if labels.get('homelab.update.exclude','').lower() in ('1','true'):return '已设置忽略'
    if c['State']['Status']!='running':return '仅检查运行中的服务'
    h=c['HostConfig']
    if any(str(h.get(k,'')).startswith('container:') for k in ('NetworkMode','PidMode','IpcMode')):return '共享容器命名空间需手动维护'
    if h.get('AutoRemove'):return '自动删除容器需手动维护'
    if '@' in ref or ref.startswith('sha256:'):return '固定摘要或无可追踪标签'
    return None

def scan(notify=True):
    api=API();result={'checked':0,'updates':0,'notified':0,'errors':[],'skipped':[]}
    with open(ROOT/'scan.lock','w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'busy':True}
        cache={};autos=[]
        for eid,label in api.envs.items():
            try:containers=api.call(eid,'/containers/json?all=1')
            except Exception as ex:result['errors'].append(label+'：环境不可达');continue
            for summary in containers:
                name=(summary.get('Names')or['?'])[0].lstrip('/')
                try:
                    c=api.call(eid,'/containers/'+summary['Id']+'/json')
                    if (c['Config'].get('Labels') or {}).get('homelab.test')=='update-fixture':continue
                    reason=exclusion(c)
                    if reason:result['skipped'].append(label+'/'+name+'：'+reason);continue
                    local=api.call(eid,'/images/'+c['Image']+'/json');ref=(c['Config'].get('Labels') or {}).get('homelab.update.image') or c['Config']['Image']
                    key=(eid,ref,local['Architecture'],local.get('Variant',''))
                    if key not in cache:cache[key]=resolve_image(api,eid,ref,local)
                    target=cache[key];result['checked']+=1
                    if matches(local,target):
                        with db() as conn:conn.execute("UPDATE candidates SET status='superseded' WHERE eid=? AND cid=? AND status='pending'",(eid,c['Id']))
                        continue
                    result['updates']+=1
                    pending={'eid':eid,'cid':c['Id'],'name':name,'old_image':c['Image'],'target':target,'environment':label,'created':time.time()}
                    with db() as conn:
                        row=conn.execute("SELECT * FROM candidates WHERE eid=? AND cid=? AND target=? AND (status='ignored' OR (status='pending' AND expires>?))",(eid,c['Id'],target['config'],time.time())).fetchone()
                        if row and row['status']=='ignored':continue
                        if row:ident=row['id'];sent=row['notified']
                        else:
                            ident=secrets.token_hex(10);sent=0
                            conn.execute('INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?)',(ident,eid,c['Id'],target['config'],json.dumps(pending),'pending',time.time()+72*3600,0))
                        failed=conn.execute("SELECT 1 FROM candidates WHERE eid=? AND cid=? AND target=? AND status='failed'",(eid,c['Id'],target['config'])).fetchone()
                        data=json.loads(conn.execute('SELECT data FROM candidates WHERE id=?',(ident,)).fetchone()[0])
                    if AUTO_LABEL in (c['Config'].get('Labels') or {}) and not failed:
                        if notify:autos.append((ident,data))
                        continue
                    if notify and not sent:
                        telegram(f"🐳 Docker 镜像发现更新\n{label} / {name}\n镜像：{ref}\n平台：{target['platform']}\n当前：{c['Image'][7:19]}\n新版本：{target['config'][7:19]}\n\n点下方查看确认，才会更新。候选有效期 72 小时。",
                                 [[{'text':'查看并确认更新','callback_data':'du:a:'+ident},{'text':'忽略此候选','callback_data':'du:x:'+ident}]])
                        with db() as conn:conn.execute('UPDATE candidates SET notified=1 WHERE id=?',(ident,))
                        result['notified']+=1
                except Exception as ex:result['errors'].append(label+'/'+name+'：检查失败（'+type(ex).__name__+'）')
        if notify and result['errors']:
            tell('⚠️ Docker 更新检查有未完成项目\n'+'\n'.join(result['errors'])+'\n其余项目已照常检查，出错的项目未做任何更新。')
        for ident,p in autos:
            with db() as conn:conn.execute("UPDATE candidates SET status='approved' WHERE id=? AND status='pending'",(ident,))
            worker(ident,p,None,auto=True)
        result['auto']=len(autos)
        result['cleanup']=scan_cleanup(api,notify)
        result['images']=prune_images(api,notify)
        result['build_cache']=prune_build_cache(api,notify)
        return result


def timestamp(value):
    try:return datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
    except (ValueError,AttributeError):return 0

# Rollback containers are removed by the daily scan once they have been stopped this long.
KEEP_SECONDS=2*86400
# Untagged leftovers from past updates are pruned once they are this old. Longer than
# KEEP_SECONDS on purpose: the image is the only way back after the rollback container is gone,
# and for :latest-style tags the previous version is usually no longer pullable.
IMAGE_KEEP_HOURS=14*24

class RollbackGone(Exception):pass

def cleanup_ready(api,p,now=None):
    now=time.time() if now is None else now
    try:old=api.call(p['eid'],'/containers/'+p['cid']+'/json')
    except urllib.error.HTTPError as ex:
        if ex.code==404:raise RollbackGone() from ex
        raise
    expected=p['name']+'.__rollback__.'+p['cid'][:8]
    if old['Id']!=p['cid'] or old['Name']!='/'+expected or old['State'].get('Running') or old['State']['Status']!='exited':
        raise ValueError('旧容器已变化或仍在运行')
    # 优先用更新落地时间；老记录没有这个字段时退回 FinishedAt（行为同以前）。
    since=p.get('applied') or timestamp(old['State'].get('FinishedAt',''))
    if not since or now-since<KEEP_SECONDS:raise ValueError('旧容器保留未满2天')
    new=api.call(p['eid'],'/containers/'+urllib.parse.quote(p['name'],safe='')+'/json')
    state=new['State']
    # 只要求「现在叫这个名字的容器不是回退容器本身」。曾经还要求它必须是本次更新创建的那个，
    # 但服务再更新一轮后 new_id 就对不上，回退容器会被永久搁浅——而那时它恰恰是最该删的。
    if new['Name']!='/'+p['name'] or new['Id']==p['cid']:raise ValueError('替代容器已变化')
    # Require the replacement to be up now, not continuously since the update: a host reboot
    # resets StartedAt and would otherwise postpone the cleanup indefinitely.
    if not state.get('Running') or state.get('Restarting') or state.get('Paused') or state.get('Dead') or state.get('Status')!='running':raise ValueError('新容器运行异常')
    health=state.get('Health',{}).get('Status')
    if health and health!='healthy':raise ValueError('新容器健康检查未通过')
    return new['Id'],bool(health)

def scan_cleanup(api,notify=True):
    """Delete rollback containers kept for KEEP_SECONDS whose replacement is healthy; --scan-dry only counts them."""
    result={'eligible':0,'removed':0,'gone':0,'errors':[]}
    removed=[]
    with db() as conn:rows=conn.execute("SELECT id,status,data FROM candidates WHERE status IN ('done','failed')").fetchall()
    for row in rows:
        p=json.loads(row['data'])
        if p.get('operation')=='cleanup':continue
        # 只有更新成功才会写 applied / new_id。带着它们的 failed 是修复 tell() 之前通知没发出去被误标的，照样清理。
        if row['status']=='failed' and not (p.get('applied') and p.get('new_id')):continue
        with db() as conn:
            # Respect an earlier "保留旧容器" choice from the old confirm-to-clean flow.
            if conn.execute("SELECT 1 FROM candidates WHERE eid=? AND cid=? AND target LIKE 'cleanup:%' AND status='ignored'",(p['eid'],p['cid'])).fetchone():continue
        label=p['environment']+' / '+p['name']
        try:
            cleanup_ready(api,p)
            result['eligible']+=1
            if not notify:continue
            with open(ROOT/'apply.lock','w') as lock:
                try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    result['errors'].append(label+'：有更新正在执行，下次检查再清理');continue
                cleanup_ready(api,p)
                # No force and no volume removal: a concurrently started old container is protected by Docker.
                api.call(p['eid'],'/containers/'+p['cid']+'?force=0&v=0','DELETE')
            with db() as conn:conn.execute("UPDATE candidates SET status='cleaned' WHERE id=?",(row['id'],))
            result['removed']+=1;removed.append(label)
        except RollbackGone:
            # Already removed by hand; stop checking this update.
            result['gone']+=1
            if notify:
                with db() as conn:conn.execute("UPDATE candidates SET status='cleaned' WHERE id=?",(row['id'],))
        except ValueError:continue
        except Exception:result['errors'].append(label+'：清理检查失败')
    if notify and removed:
        try:telegram('🧹 已自动删除保留满2天的旧容器\n'+'\n'.join(removed)+'\n新容器运行正常；镜像和数据卷均保留。')
        except Exception:result['errors'].append('清理结果通知发送失败')
    if notify and result['errors']:tell('⚠️ 旧容器清理未完成\n'+'\n'.join(result['errors']))
    return result

def prune_images(api,notify=True):
    """Prune dangling images older than IMAGE_KEEP_HOURS. Only untagged ones: a tagged image may
    still be needed by a one-off container (e.g. the bilitool login tool) that is not running."""
    result={'removed':0,'freed':0,'errors':[]}
    lines=[]
    filters=json.dumps({'dangling':['true'],'until':[str(IMAGE_KEEP_HOURS)+'h']})
    cutoff=time.time()-IMAGE_KEEP_HOURS*3600
    for eid,label in api.envs.items():
        try:
            if not notify:
                # --scan-dry: count what would go, delete nothing.
                listed=api.call(eid,'/images/json?filters='+urllib.parse.quote(json.dumps({'dangling':['true']}))) or []
                old=[i for i in listed if (i.get('Created') or 0)<cutoff]
                result['removed']+=len(old);result['freed']+=sum(i.get('Size') or 0 for i in old)
                continue
            out=api.call(eid,'/images/prune?filters='+urllib.parse.quote(filters),'POST') or {}
            n=len(out.get('ImagesDeleted') or [])
            freed=out.get('SpaceReclaimed') or 0
            result['removed']+=n;result['freed']+=freed
            if freed:lines.append(f"{label}：{n} 层，释放 {freed/1073741824:.2f} GB")
        except Exception:result['errors'].append(label+'：镜像清理失败')
    if notify and lines:
        try:telegram('🧹 已清理 %d 天前的无标签旧镜像\n' % (IMAGE_KEEP_HOURS//24)+'\n'.join(lines)+'\n带标签的镜像与数据卷不受影响。')
        except Exception:result['errors'].append('镜像清理通知发送失败')
    if notify and result['errors']:tell('⚠️ 旧镜像清理未完成\n'+'\n'.join(result['errors']))
    return result

# Build cache from the local auto-builds (bilitool, yyb-go, course-platform) is not an image, so
# neither the rollback cleanup nor the dangling-image prune touched it; on the Docker VM it had grown
# to 9.5 GB (2026-09-23). An age window (48 h, first version) was not enough: a single bilitool build
# after an upstream dependency bump left ~4.5 GB on its own the next day (disk back to 81%), so a size
# cap was added (2026-09-24). The cap alone was not enough either (2026-09-29): BuildKit measures its own
# deduplicated usage, which stays under 3 GB while `docker system df` (which also counts layers shared
# with images) showed 6.4 GB, and entries unused for 9 days were never evicted. Run both: first drop
# everything (all=1) unused for BUILD_CACHE_KEEP_HOURS, then enforce the size cap for oversized builds.
BUILD_CACHE_KEEP_HOURS=72
BUILD_CACHE_MAX_BYTES=3*1024**3

def prune_build_cache(api,notify=True):
    """Drop build cache unused for BUILD_CACHE_KEEP_HOURS, then cap it at BUILD_CACHE_MAX_BYTES; --scan-dry only estimates."""
    result={'removed':0,'freed':0,'errors':[]}
    lines=[]
    for eid,label in api.envs.items():
        try:
            if not notify:
                # Estimate by walking unused entries oldest-first; records share layers, so this overstates.
                # Only the age rule is estimated: the size cap follows BuildKit's own accounting, which the API does not expose.
                caches=(api.call(eid,'/system/df') or {}).get('BuildCache') or []
                cutoff=time.time()-BUILD_CACHE_KEEP_HOURS*3600
                old=[c for c in caches if not c.get('InUse') and timestamp(c.get('LastUsedAt') or c.get('CreatedAt') or '')<cutoff]
                result['removed']+=len(old);result['freed']+=sum(c.get('Size') or 0 for c in old)
                continue
            filters=urllib.parse.quote(json.dumps({'until':[str(BUILD_CACHE_KEEP_HOURS)+'h']}))
            n=freed=0
            for q in ('all=1&filters='+filters,'max-used-space=%d' % BUILD_CACHE_MAX_BYTES):
                out=api.call(eid,'/build/prune?'+q,'POST',timeout=600) or {}
                n+=len(out.get('CachesDeleted') or []);freed+=out.get('SpaceReclaimed') or 0
            result['removed']+=n;result['freed']+=freed
            if freed:lines.append(f"{label}：{n} 项，释放 {freed/1073741824:.2f} GB")
        except Exception:result['errors'].append(label+'：构建缓存清理失败')
    if notify and lines:
        try:telegram('🧹 已清理构建缓存（%d 小时未使用，或超过 %d GB 时按最久未用）\n' % (BUILD_CACHE_KEEP_HOURS,BUILD_CACHE_MAX_BYTES//1024**3)+'\n'.join(lines))
        except Exception:result['errors'].append('构建缓存清理通知发送失败')
    if notify and result['errors']:tell('⚠️ 构建缓存清理未完成\n'+'\n'.join(result['errors']))
    return result

def cleanup_worker(ident,p,chat):
    try:
        with open(ROOT/'apply.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with db() as conn:conn.execute("UPDATE candidates SET status='running' WHERE id=?",(ident,))
            api=API();cleanup_ready(api,p)
            # No force and no volume removal: a concurrently started old container is protected by Docker.
            api.call(p['eid'],'/containers/'+p['cid']+'?force=0&v=0','DELETE')
            with db() as conn:conn.execute("UPDATE candidates SET status='done' WHERE id=?",(ident,))
        tell('✅ 已清理旧容器：'+p['environment']+' / '+p['name']+'\n新容器、镜像和数据卷均保留。',chat=chat)
    except Exception as ex:
        with db() as conn:conn.execute("UPDATE candidates SET status='failed' WHERE id=?",(ident,))
        tell('⚠️ 旧容器清理未完成：'+p['name']+'\n'+(str(ex) if isinstance(ex,ValueError) else type(ex).__name__)+'\n请检查 Portainer，未自动重试。',chat=chat)

def candidate(ident):
    with db() as conn:row=conn.execute('SELECT * FROM candidates WHERE id=?',(ident,)).fetchone()
    if not row or row['status']!='pending' or row['expires']<time.time():raise ValueError('候选已失效，请重新检查')
    return json.loads(row['data'])

def approve(ident,uid,chat):
    candidate(ident);token=secrets.token_hex(12)
    with db() as conn:conn.execute('INSERT INTO approvals VALUES (?,?,?,?,?,0)',(token,ident,str(uid),str(chat),time.time()+120))
    return token

def consume(token,uid,chat):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        a=conn.execute('SELECT * FROM approvals WHERE id=?',(token,)).fetchone()
        if not a or a['used'] or a['expires']<time.time() or a['uid']!=str(uid) or a['chat']!=str(chat):raise ValueError('确认已失效或无权操作')
        c=conn.execute('SELECT * FROM candidates WHERE id=?',(a['candidate'],)).fetchone()
        if not c or c['status']!='pending' or c['expires']<time.time():raise ValueError('候选已失效')
        conn.execute('UPDATE approvals SET used=1 WHERE id=?',(token,))
        conn.execute("UPDATE candidates SET status='approved' WHERE id=?",(a['candidate'],))
        return a['candidate'],json.loads(c['data'])

def payload(c,image):
    result=copy.deepcopy(c['Config']);result['Image']=image
    result['HostConfig']=copy.deepcopy(c['HostConfig'])
    result['HostConfig'].pop('ContainerIDFile',None)
    # Bind anonymous volumes to their original names so recreation cannot lose their data.
    destinations={m['Target'] for m in result['HostConfig'].get('Mounts',[]) }
    for b in result['HostConfig'].get('Binds') or []:
        parts=b.split(':');destinations.add(parts[1] if len(parts)>1 else parts[0])
    for m in c.get('Mounts',[]):
        if m['Type']=='volume' and m['Destination'] not in destinations:
            result['HostConfig'].setdefault('Mounts',[]).append({'Type':'volume','Source':m['Name'],'Target':m['Destination'],'ReadOnly':not m['RW']})
    networks={}
    for name,n in c['NetworkSettings']['Networks'].items():
        if name in ('host','none','bridge'):continue
        ep={k:copy.deepcopy(n[k]) for k in ['IPAMConfig','Links','DriverOpts','GwPriority'] if n.get(k) is not None}
        ep['Aliases']=[a for a in n.get('Aliases') or [] if a not in (c['Id'],c['Id'][:12])]
        networks[name]=ep
    if networks:result['NetworkingConfig']={'EndpointsConfig':networks}
    return result

def wait_healthy(api,eid,cid,timeout=150):
    deadline=time.time()+timeout;stable=0
    while time.time()<deadline:
        c=api.call(eid,'/containers/'+cid+'/json');state=c['State']
        if not state['Running'] or state.get('Restarting') or c.get('RestartCount',0):raise RuntimeError('新容器退出或反复重启')
        health=state.get('Health',{}).get('Status')
        if health=='unhealthy':raise RuntimeError('新容器健康检查失败')
        if health=='healthy':return
        if not health:
            stable+=1
            if stable>=4:return
        time.sleep(4)
    raise RuntimeError('等待健康检查超时')

class TagMoved(ValueError):
    """The tag points elsewhere than the approved version; raised before any disruptive action."""
    def __init__(self,target,uptodate):
        super().__init__('标签已有更新版本，请重新检查后确认');self.target=target;self.uptodate=uptodate

def apply_update(api,p,ident):
    eid,cid=p['eid'],p['cid'];name=p['name'];old=api.call(eid,'/containers/'+cid+'/json')
    if old['Name'].lstrip('/')!=name or old['Image']!=p['old_image'] or exclusion(old):raise ValueError('容器已变化或不适合自动重建')
    # Reject namespace dependents; these need a coordinated stack update.
    for other in api.call(eid,'/containers/json?all=1'):
        if other['Id']==cid:continue
        h=api.call(eid,'/containers/'+other['Id']+'/json')['HostConfig']
        if any(str(h.get(k,'')) in ('container:'+cid,'container:'+name) for k in ('NetworkMode','PidMode','IpcMode')):
            raise ValueError('其他容器依赖此容器命名空间，请在 Portainer 协调更新')
    local=api.call(eid,'/images/'+old['Image']+'/json')
    current=resolve_image(api,eid,p['target']['ref'],local)
    if current['config']!=p['target']['config']:raise TagMoved(current,matches(local,current))
    # Pull the original tag, then verify it matches exactly the image approved by the user.
    # Engine API 的 images/create 在 fromImage 不带标签时会拉取该仓库的**全部标签**。
    # 容器写成 henrygd/beszel-agent 这种无标签引用时，一次更新拉下了 124 个历史版本，
    # 还触发了 Docker Hub 429 限流（2026-09-21）。没有标签和摘要时显式补 latest。
    ref=p['target']['ref']
    if ':' not in ref.rsplit('/',1)[-1] and '@' not in ref:ref+=':latest'
    pull=api.call(eid,'/images/create?fromImage='+urllib.parse.quote(ref,safe=''),method='POST',raw=True,timeout=600)
    for line in pull.splitlines():
        if line.strip() and json.loads(line).get('error'):raise RuntimeError('镜像拉取失败')
    pulled=api.call(eid,'/images/'+urllib.parse.quote(p['target']['ref'],safe='/:@')+'/json')
    if not matches(pulled,p['target']):raise ValueError('拉取结果与确认版本不一致，未停止旧容器')
    # Recheck immediately before the first disruptive action.
    check=api.call(eid,'/containers/'+cid+'/json')
    if check['Image']!=old['Image'] or check['Config']!=old['Config'] or check['HostConfig']!=old['HostConfig']:raise ValueError('容器配置已变化，请重新检查')
    backup=ROOT/(ident+'-container.json');backup.write_text(json.dumps(old));backup.chmod(0o600)
    rollback=name+'.__rollback__.'+cid[:8];newid=None;renamed=False;disconnected=[]
    netconfig=payload(old,p['target']['ref']).get('NetworkingConfig',{}).get('EndpointsConfig',{})
    try:
        api.call(eid,'/containers/'+cid+'/update','POST',{'RestartPolicy':{'Name':'no'}})
        api.call(eid,'/containers/'+cid+'/stop?t=30','POST')
        api.call(eid,'/containers/'+cid+'/rename?name='+urllib.parse.quote(rollback),'POST');renamed=True
        for network,ep in netconfig.items():
            api.call(eid,'/networks/'+urllib.parse.quote(network,safe='')+'/disconnect','POST',{'Container':cid,'Force':False});disconnected.append(network)
        spec=payload(old,pulled['Id']);spec.setdefault('Labels',{});spec['Labels']=spec['Labels'] or {};spec['Labels']['homelab.update.image']=p['target']['ref']
        created=api.call(eid,'/containers/create?name='+urllib.parse.quote(name),'POST',spec);newid=created['Id']
        api.call(eid,'/containers/'+newid+'/start','POST');wait_healthy(api,eid,newid)
        return {'new_id':newid,'rollback_container':rollback,'image':pulled['Id']}
    except Exception:
        if newid:api.call(eid,'/containers/'+newid+'?force=1','DELETE')
        if renamed:api.call(eid,'/containers/'+cid+'/rename?name='+urllib.parse.quote(name),'POST')
        for network in disconnected:
            api.call(eid,'/networks/'+urllib.parse.quote(network,safe='')+'/connect','POST',{'Container':cid,'EndpointConfig':netconfig[network]})
        api.call(eid,'/containers/'+cid+'/update','POST',{'RestartPolicy':old['HostConfig']['RestartPolicy']})
        api.call(eid,'/containers/'+cid+'/start','POST')
        raise

def reoffer(ident,p,ex,chat):
    """Replace a candidate whose tag moved before apply with one for the current version, asking again."""
    label=p['environment']+' / '+p['name'];target=ex.target;head='🔄 '+label+' 在等待确认期间镜像标签又变了\n本次没有做任何改动。\n'
    with db() as conn:
        # Retire this candidate and any other stale offers for the same container.
        conn.execute("UPDATE candidates SET status='superseded' WHERE id=? OR (eid=? AND cid=? AND target NOT LIKE 'cleanup:%' AND target!=? AND status='pending')",(ident,p['eid'],p['cid'],target['config']))
        if ex.uptodate:
            conn.execute("UPDATE candidates SET status='superseded' WHERE eid=? AND cid=? AND target=? AND status='pending'",(p['eid'],p['cid'],target['config']))
            return telegram(head+'标签已回到当前运行的版本，无需更新。',chat=chat)
        rows=conn.execute("SELECT id,status FROM candidates WHERE eid=? AND cid=? AND target=? AND (status='ignored' OR (status='pending' AND expires>?))",(p['eid'],p['cid'],target['config'],time.time())).fetchall()
        if any(r['status']=='ignored' for r in rows):return telegram(head+'最新版本 '+target['config'][7:19]+' 之前已被忽略，不再提示。',chat=chat)
        if rows:new=rows[0]['id']
        else:
            new=secrets.token_hex(10)
            conn.execute('INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?)',(new,p['eid'],p['cid'],target['config'],json.dumps({**p,'target':target,'created':time.time()}),'pending',time.time()+72*3600,0))
    telegram(head+'镜像：'+target['ref']+'\n当前：'+p['old_image'][7:19]+'\n新版本：'+target['config'][7:19]+'\n\n已按最新版本重新生成确认，点下方确认才会更新。候选有效期 72 小时。',
             [[{'text':'查看并确认更新','callback_data':'du:a:'+new},{'text':'忽略此候选','callback_data':'du:x:'+new}]],chat)
    with db() as conn:conn.execute('UPDATE candidates SET notified=1 WHERE id=?',(new,))

def worker(ident,p,chat,auto=False):
    try:
        with open(ROOT/'apply.lock','w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with db() as conn:conn.execute("UPDATE candidates SET status='running' WHERE id=?",(ident,))
            if not auto:tell('⏳ 正在更新 '+p['environment']+' / '+p['name']+'\n将验证镜像和启动状态。',chat=chat)
            result=apply_update(API(),p,ident)
            # 'applied' 是回退容器诞生的时刻，保留期以它为准：容器被重启过会刷新 FinishedAt，
            # 用 FinishedAt 计时会让保留期无限延后（2026-09-21 遇到过两个放了 16 天没删的）。
            with db() as conn:conn.execute("UPDATE candidates SET status='done',data=? WHERE id=?",(json.dumps({**p,'new_id':result['new_id'],'applied':time.time()}),ident))
        # 已经记为 done：这条通知发不出去也不能再进下面的 except（见 tell）
        tell(('🤖 已自动更新：' if auto else '✅ 更新完成：')+p['environment']+' / '+p['name']+'\n镜像：'+result['image'][7:19]+'\n旧容器已停止并保留供回退，满2天后由每日检查自动删除。',chat=chat)
    except TagMoved as ex:
        if auto:
            # 检查和执行之间标签又变了：本轮不动，下次检查按新版本自动更新。
            with db() as conn:conn.execute("UPDATE candidates SET status='superseded' WHERE id=?",(ident,))
            return
        try:reoffer(ident,p,ex,chat)
        except Exception:
            with db() as conn:conn.execute("UPDATE candidates SET status='failed' WHERE id=?",(ident,))
            tell('⚠️ 更新未完成：'+p['environment']+' / '+p['name']+'\n标签已有更新版本，重新生成确认失败；等下次每日检查重新提示。',chat=chat)
    except Exception as ex:
        with db() as conn:conn.execute("UPDATE candidates SET status='failed' WHERE id=?",(ident,))
        safe=str(ex) if isinstance(ex,ValueError) else type(ex).__name__
        tell(('⚠️ 自动更新未完成：' if auto else '⚠️ 更新未完成：')+p['environment']+' / '+p['name']+'\n'+safe+'\n请在 Portainer 检查当前状态；不会自动重试更新。'
                 +('\n这个版本下次检查改为发确认按钮。' if auto else ''),chat=chat)

def install(bot,menu):
    @bot.callback_query_handler(func=lambda c: bool(c.data and c.data.startswith('du:')))
    def callback(call):
        if not call.message or not menu.allowed(call.message,call.from_user.id):
            bot.answer_callback_query(call.id,'无权操作',show_alert=True);return
        uid,chat=call.from_user.id,call.message.chat.id
        try:
            _,action,token=call.data.split(':',2)
            if action=='a':
                p=candidate(token);approval=approve(token,uid,chat)
                if p.get('operation')=='cleanup':
                    telegram('确认删除停止的旧容器？\n'+p['environment']+' / '+p['name']+'\n将再次检查保留天数和新容器状态。删除后旧容器无法直接恢复；镜像与数据卷保留。\n确认按钮2分钟内有效。',[[{'text':'确认清理旧容器','callback_data':'du:c:'+approval},{'text':'取消','callback_data':'du:n:'+approval}]],chat)
                    bot.answer_callback_query(call.id,'请确认清理');return
                telegram('确认更新以下服务？\n'+p['environment']+' / '+p['name']+'\n镜像：'+p['target']['ref']+'\n目标：'+p['target']['config'][7:19]+'\n\n会短暂停机，并沿用现有配置和数据卷。\n启动异常会尝试恢复旧容器；应用数据迁移不保证可回退。\n确认按钮 2 分钟内有效。',
                    [[{'text':'确认更新这一容器','callback_data':'du:c:'+approval},{'text':'取消','callback_data':'du:n:'+approval}]],chat)
            elif action=='c':
                ident,p=consume(token,uid,chat);threading.Thread(target=cleanup_worker if p.get('operation')=='cleanup' else worker,args=(ident,p,chat),daemon=True).start()
            elif action=='n':
                with db() as conn:conn.execute('UPDATE approvals SET used=1 WHERE id=? AND uid=? AND chat=?',(token,str(uid),str(chat)))
            elif action=='x':
                with db() as conn:conn.execute("UPDATE candidates SET status='ignored' WHERE id=? AND status='pending'",(token,))
            else:raise ValueError('无效按钮')
            bot.answer_callback_query(call.id,'已处理')
        except ValueError as ex:bot.answer_callback_query(call.id,str(ex)[:180],show_alert=True)
        except Exception:bot.answer_callback_query(call.id,'处理失败，请稍后重试',show_alert=True)
    return callback

if __name__=='__main__':
    if sys.argv[1:] not in (['--scan'],['--scan-dry']):raise SystemExit('Unsupported operation')
    print(json.dumps(scan(notify=sys.argv[1]=='--scan'),ensure_ascii=False),flush=True)
