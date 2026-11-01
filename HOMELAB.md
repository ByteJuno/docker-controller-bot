# homelab 分支

[dgongut/docker-controller-bot](https://github.com/dgongut/docker-controller-bot) 的个人 fork，在上游版本之上加了几个插件。**上游文件一个都不改**，所有改动都在新增文件里，合并上游新版本时不会冲突。

| 文件 | 作用 |
| :--- | :--- |
| `homelab/main.py` | 镜像入口：把上游的 `docker-controller-bot.py` 当模块加载（命令和回调就注册好了），再装插件、启动 |
| `homelab/plugins.py` | 把插件装到上游的 bot 上，并挪到上游兜底处理函数的前面；命令菜单里加 `/status` |
| `homelab/docker_updates.py` | 经 Portainer 查新、默认自动更新（opt-out），保留回退容器 2 天，清理构建缓存（每天 03:30 由外部触发 `--scan`） |
| `homelab/policy.py` | 读取/校验/原子保存 `docker-update-policy.json`，并提供 `python3 -m homelab.policy` 维护入口 |
| `homelab/n8n_status.py` | `/status`：转发给 n8n 的主机状态报告 |
| `homelab/self_update.py` | 机器人自己的更新（见下） |
| `homelab/paths.py` | 插件文件放在 bot 的数据卷里（5.x 是 `/app/config`） |
| `Dockerfile.homelab` | 用本地源码构建（上游 Dockerfile 是下载发布包）；`TELEMETRY=0`；可选 `APK_MIRROR` / `PIP_INDEX_URL` |
| `tests/test_homelab.py` | 插件测试，单独运行，不改上游的 `tests/run_all.py` |

容器更新的 Telegram 通知：`telegram()` 遇到断连、超时、429/5xx 会再试两次；「正在更新」「已更新」「已清理」和各项「未完成」汇总这类只告知结果的消息走 `tell()`，发不出去只记日志，不改变更新或清理的状态。2026-10-22 rsshub 的自动更新其实成功了，只是那条通知赶上连接断开，被标成 failed、报了「更新未完成」，回退容器也不会被清理；这类记录（failed 但带 `applied` / `new_id`）的回退容器现在照常在 2 天后清理。

## 容器更新策略（2026-11-01）

默认自动：每日 03:30 的 `--scan` 找到新镜像后，除下列情况外都在检查末尾直接更新（校验镜像、等健康、
失败恢复旧容器，回退容器保留 2 天）。

- `DCB-Auto-Update=false`：只通知，保留上游 `=false` 的语义；空值或 `true` 仍是显式开启。
- `DCB-Ignore-Check-Updates`：完全不查这个容器。
- `homelab.update.exclude=1`：同上，完全不查。
- 数据库不自动跨大版本：名字命中 `n8n-postgres`、`ghost-db`、`paperless-redis`，或镜像家族命中
  postgres/mysql/mariadb/mongo/redis/valkey/clickhouse/influxdb/couchdb 的容器，照常查新并发按钮，
  但不会自动更新；确需自动的在 compose 上加 `DCB-Auto-Update: "true"` 单独放开。
- `daidai-panel` 交给 Watchtower（每小时），插件不自动更新它，避免两套更新器互相覆盖。
- `docker-controller-bot`、`portainer`、`portainer_edge_agent`、`n8n-postgres`、本地构建镜像
  （`local/*`、`homelab/*`、`:local`）、回退容器、停止的容器、共享命名空间、固定摘要：一律不自动。
- 新增的容器同样按上面的默认处理，不需要逐个打标签。

策略放在数据卷的 `docker-update-policy.json`（不在仓库里，升级镜像不会丢），可写：

```json
{"default": "announce",
 "database_names": ["my-app-db"],
 "database_images": ["postgres"],
 "watchtower_managed": ["some-panel"]}
```

`default` 写 `announce` 就把默认退回「只通知」；三个列表是**追加**到内置保护项，不会删掉任何保护。

文件缺失时按 `auto` 运行。文件存在但损坏（不是有效 JSON/UTF-8）、无权限读取、顶层不是对象，或 `default`/列表类型不对时，
不再静默按 `auto`：整轮检查改为只通知（不自动更新，连 `DCB-Auto-Update=true` 也暂不生效），并给管理员发一次告警；
修好后下一次检查自动恢复。告警按问题去重，不会每天重复。

改策略用内置工具（校验 + 同目录临时文件 + `fsync` + `os.replace` 原子替换，权限 0600），不要手工改 JSON：

```bash
docker exec docker-controller-bot python3 -m homelab.policy --show
docker exec docker-controller-bot python3 -m homelab.policy --set-default announce
docker exec docker-controller-bot python3 -m homelab.policy --add-database-name my-app-db
docker exec docker-controller-bot python3 -m homelab.policy --add-watchtower some-panel
docker exec docker-controller-bot python3 -m homelab.policy --remove-database-name my-app-db
```

文件已经损坏时工具会拒绝覆盖；确认要重写时加 `--replace`。工具只改自定义追加项，不会删除内置保护。

## 更新流程

1. **CI**（`.github/workflows/homelab.yml`）：每次推送 `homelab` 跑上游的全部测试和插件测试、试构建镜像，通过后打 `homelab-v<上游版本>-h<提交数>` 标签；测试不过就开（或更新）一个 `upstream-sync` issue，不推送。上游出了新的正式版本，由 lab 上的 `upstream_merge.py` 交给 gpt-6.1-sol 审核并合并到 `upstream-sync/<版本>` 分支（有冲突也由它解决），再向 `homelab` 开 PR；PR 跑全部测试但不打标签，测试通过后脚本自动合并，结果私聊机主。手动运行这个 workflow 仍会直接合并上游最新正式版。
2. **机器人**（`self_update.py`）：每 6 小时看一次这些标签，有更新的就在自己所在的 Docker 主机上构建，构建好后在 Telegram 发「部署 / 忽略此版本」按钮。`upstream-sync` issue 也会提醒一次。
3. **部署**：点「部署」后经 Portainer 把 bot 的 Stack 换成新镜像；新 bot 启动后报告「已更新」。没起来的话，外部的看门狗 5 分钟后换回旧镜像，旧 bot 报告「已回滚」。

插件配置都在数据卷里，不在仓库中：`portainer-multihost.json`（Portainer 地址和密钥）、`portainer-cert.pem`、`n8n-status.json`、`self-update.json`（Stack 编号和构建镜像源）。

```bash
python3 tests/run_all.py && python3 tests/test_homelab.py
docker build -f Dockerfile.homelab --build-arg VERSION=dev -t docker-controller-bot:dev .
```
