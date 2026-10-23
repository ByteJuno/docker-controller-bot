"""The bot's own updates: build what CI tagged, deploy it on a Telegram button.

CI tags every tested homelab commit as homelab-v<upstream>-h<n>. Every six
hours this looks at the fork's tags; a newer one is built here, on the Docker
host the bot runs on, and announced with Deploy / Ignore buttons. Deploy
records what is being replaced and asks Portainer to switch the bot's stack to
the new image, which restarts the bot. The new bot reports the outcome when it
starts. If it does not come up, dcb_guard.py on Osaka Lab puts the old image
back and the old bot reports that instead.

Open issues labelled upstream-sync (an upstream release that does not merge or
does not pass the tests) are reported once each.

Configuration, in the volume as self-update.json:
	{"stack_id": 7, "endpoint_id": 3,
	 "build_args": {"APK_MIRROR": "...", "PIP_INDEX_URL": "..."}}
Without it the plugin stays idle. Portainer's address and key come from
portainer-multihost.json, which the update flow already uses.
"""
import io
import json
import logging
import os
import re
import ssl
import tarfile
import threading
import time
import urllib.request

import requests

from homelab import paths

REPO = "ByteJuno/docker-controller-bot"
IMAGE = "homelab/docker-controller-bot"
TAG = re.compile(r"^homelab-v(\d+)\.(\d+)\.(\d+)-h(\d+)$")
CHECK_EVERY = 6 * 3600
KEEP_IMAGES = 3   # 本机保留最近几个 homelab 版本镜像（含正在运行的），更早的在新版本上线后删除
VERSION = os.environ.get("HOMELAB_VERSION", "")
lock = threading.Lock()


def version_of(tag):
	return tag[len("homelab-"):] if tag.startswith("homelab-") else tag


def order(version):
	"""Sortable form of v5.0.0-h3; anything else sorts first."""
	m = TAG.match("homelab-" + version)
	return tuple(int(x) for x in m.groups()) if m else (0,)


def load_state():
	try:
		return json.loads(paths.data_file("self-update-state.json").read_text())
	except (OSError, ValueError):
		return {}


def save_state(state):
	path = paths.data_file("self-update-state.json")
	tmp = path.with_suffix(".tmp")
	tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
	tmp.chmod(0o644)
	tmp.replace(path)


def config():
	try:
		return json.loads(paths.data_file("self-update.json").read_text())
	except (OSError, ValueError):
		return None


def telegram(text, buttons=None):
	from homelab.docker_updates import telegram as send
	return send(text, buttons)


# ---------- Portainer ----------

def portainer(path, method="GET", data=None):
	cfg = json.loads(paths.data_file("portainer-multihost.json").read_text())
	ctx = ssl.create_default_context(cafile=paths.certificate(cfg["certificate"]))
	ctx.check_hostname = False
	req = urllib.request.Request(cfg["url"].rstrip("/") + path, method=method,
								data=json.dumps(data).encode() if data is not None else None,
								headers={"X-API-Key": cfg["api_key"], "Content-Type": "application/json"})
	with urllib.request.urlopen(req, context=ctx, timeout=300) as f:
		body = f.read()
	return json.loads(body) if body else None


def set_stack_image(cfg, image):
	stack = portainer(f"/api/stacks/{cfg['stack_id']}")
	content = portainer(f"/api/stacks/{cfg['stack_id']}/file")["StackFileContent"]
	lines = [ln for ln in content.splitlines() if ln.strip().startswith(f"image: {IMAGE}:")]
	if len(lines) != 1:
		raise RuntimeError(f"stack has {len(lines)} lines with image: {IMAGE}")
	content = content.replace(lines[0].strip(), f"image: {image}")
	portainer(f"/api/stacks/{cfg['stack_id']}?endpointId={cfg['endpoint_id']}", "PUT",
			{"stackFileContent": content, "env": stack.get("Env") or [], "prune": False, "pullImage": False})


def current_image(cfg):
	content = portainer(f"/api/stacks/{cfg['stack_id']}/file")["StackFileContent"]
	return next(ln.strip()[len("image: "):] for ln in content.splitlines() if ln.strip().startswith(f"image: {IMAGE}:"))


# ---------- GitHub and building ----------

def newest_tag():
	# Only the homelab tags: the fork also carries every upstream tag, more than one page of /tags.
	r = requests.get(f"https://api.github.com/repos/{REPO}/git/matching-refs/tags/homelab-", timeout=30)
	r.raise_for_status()
	tags = [ref["ref"][len("refs/tags/"):] for ref in r.json()]
	tags = [t for t in tags if TAG.match(t)]
	return max(tags, key=lambda t: order(version_of(t)), default=None)


def sync_issues():
	r = requests.get(f"https://api.github.com/repos/{REPO}/issues?labels=upstream-sync&state=open", timeout=30)
	r.raise_for_status()
	return [(i["number"], i["title"], i["html_url"]) for i in r.json()]


def build(tag, cfg):
	"""Builds the tag's Dockerfile.homelab on this Docker host and returns the image reference."""
	import docker
	r = requests.get(f"https://codeload.github.com/{REPO}/tar.gz/refs/tags/{tag}", timeout=120)
	r.raise_for_status()
	# GitHub's archive has one top-level directory; the build context is its content.
	context = io.BytesIO()
	with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:gz") as src, \
			tarfile.open(fileobj=context, mode="w") as dst:
		for member in src.getmembers():
			parts = member.name.split("/", 1)
			if len(parts) < 2 or not parts[1]:
				continue
			member.name = parts[1]
			dst.addfile(member, src.extractfile(member) if member.isfile() else None)
	context.seek(0)
	version = version_of(tag)
	image = f"{IMAGE}:{version}"
	args = dict(cfg.get("build_args") or {}, VERSION=version, VCS_REF=tag,
				BUILD_DATE=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
	client = docker.from_env(timeout=1800)
	client.images.build(fileobj=context, custom_context=True, dockerfile="Dockerfile.homelab",
						tag=image, buildargs=args, rm=True, forcerm=True)
	return image


def check():
	cfg = config()
	if not cfg:
		return
	with lock:
		state = load_state()
		try:
			for number, title, url in sync_issues():
				if number not in state.setdefault("issues", []):
					telegram(f"⚠️ 机器人上游同步需要手动处理\n{title}\n{url}")
					state["issues"].append(number)
		except Exception as ex:
			logging.warning("self-update: issue check failed: %s", ex)
		tag = newest_tag()
		if not tag or order(version_of(tag)) <= order(VERSION):
			save_state(state)
			return
		if tag in state.setdefault("ignored", []) or tag in state.setdefault("offered", {}):
			save_state(state)
			return
		try:
			image = build(tag, cfg)
		except Exception as ex:
			if state.setdefault("failed", {}).get(tag) is None:
				telegram(f"❌ 机器人新版本 {version_of(tag)} 构建失败\n{type(ex).__name__}: {str(ex)[:300]}")
				state["failed"][tag] = int(time.time())
			save_state(state)
			return
		state["offered"][tag] = image
		save_state(state)
	telegram(f"🆕 机器人新版本 {version_of(tag)} 已构建好（当前 {VERSION or '?'}）\n"
			f"https://github.com/{REPO}/commits/{tag}",
			[[{"text": "部署", "callback_data": f"su:deploy:{tag}"},
			{"text": "忽略此版本", "callback_data": f"su:ignore:{tag}"}]])


def loop():
	time.sleep(120)
	while True:
		try:
			check()
		except Exception as ex:
			logging.warning("self-update check failed: %s: %s", type(ex).__name__, ex)
		time.sleep(CHECK_EVERY)


def prune_images():
	"""每次部署都会在本机多一个镜像。只留最近 KEEP_IMAGES 个 v5 版本（一定包括正在运行的）；
	4.2.0 时代的 homelab 镜像不归这里管，留给手工回退。早期手工构建的 5.0.0-homelab.N 一并清掉。"""
	import docker
	client = docker.from_env()
	versions = [t.split(":", 1)[1] for img in client.images.list(name=IMAGE) for t in img.tags]
	ours = [v for v in versions if order(v) > (0,)]
	keep = set(sorted(ours, key=order)[-KEEP_IMAGES:]) | {VERSION}
	for v in versions:
		if v in keep or not (order(v) > (0,) or v.startswith("5.0.0-homelab.")):
			continue
		try:
			client.images.remove(f"{IMAGE}:{v}")
			logging.info("self-update: removed old image %s", v)
		except Exception as ex:
			logging.warning("self-update: could not remove %s: %s", v, ex)


def report_startup():
	"""Says how the last deploy ended, once the bot that came up can say it."""
	time.sleep(15)
	with lock:
		state = load_state()
		pending = state.pop("pending", None)
		if not pending:
			return
		save_state(state)
	if VERSION == pending["to"]:
		telegram(f"✅ 机器人已更新到 {pending['to']}（原 {pending['from']}）")
		try:
			prune_images()
		except Exception as ex:
			logging.warning("self-update: image cleanup failed: %s", ex)
	else:
		telegram(f"⚠️ 机器人 {pending['to']} 没有正常启动，已回滚到 {VERSION or pending['from']}")


def install(bot, menu):
	@bot.callback_query_handler(func=lambda c: bool(c.data and c.data.startswith("su:")))
	def on_button(call):
		if not call.message or not menu.allowed(call.message, call.from_user.id):
			return
		_, action, tag = call.data.split(":", 2)
		cfg = config()
		with lock:
			state = load_state()
			image = state.get("offered", {}).get(tag)
			if not image or not cfg:
				bot.answer_callback_query(call.id, "这个版本已经处理过了")
				return
			if action == "deploy" and order(version_of(tag)) <= order(VERSION):
				# An older offer clicked after a newer version went live would be a downgrade.
				state["offered"].pop(tag, None)
				save_state(state)
				bot.answer_callback_query(call.id, "已经是更新的版本了")
				bot.edit_message_text(f"机器人已是 {VERSION}，不再部署旧版本 {version_of(tag)}",
									call.message.chat.id, call.message.message_id)
				return
			if action == "ignore":
				state["offered"].pop(tag, None)
				state.setdefault("ignored", []).append(tag)
				save_state(state)
				bot.answer_callback_query(call.id, "已忽略")
				bot.edit_message_text(f"已忽略机器人版本 {version_of(tag)}", call.message.chat.id, call.message.message_id)
				return
			state["pending"] = {"from": VERSION, "from_image": current_image(cfg), "to": version_of(tag),
								"to_image": image, "at": int(time.time())}
			state["offered"].pop(tag, None)
			save_state(state)
		bot.answer_callback_query(call.id, "开始部署")
		bot.edit_message_text(f"🚀 正在部署机器人 {version_of(tag)}，机器人会重启，稍后报告结果。",
							call.message.chat.id, call.message.message_id)
		try:
			set_stack_image(cfg, image)   # Portainer recreates this container: nothing after this is guaranteed to run.
		except Exception as ex:
			with lock:
				state = load_state()
				state.pop("pending", None)
				save_state(state)
			telegram(f"❌ 部署机器人 {version_of(tag)} 失败：{type(ex).__name__}: {str(ex)[:300]}")

	threading.Thread(target=report_startup, name="self-update-report", daemon=True).start()
	threading.Thread(target=loop, name="self-update", daemon=True).start()
	return on_button
