"""
Homelab plugins on top of the upstream bot.

Run on its own (python3 tests/test_homelab.py) so that tests/run_all.py, an
upstream file, stays untouched. It loads the bot exactly as the upstream tests
do and checks the things a new upstream release could quietly break: that the
plugin handlers still win over the core's catch-alls, that /status and
the update buttons reach the plugins, that the command menu carries them, and
that only administrators get through.
"""
import contextlib
import io
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
sys.path.insert(0, harness.REPO)

harness.load_bot()
os.environ["HOMELAB_DATA_DIR"] = harness.tempdir()
from homelab import plugins as homelab_plugins  # noqa: E402
from homelab import paths as homelab_paths  # noqa: E402

core = sys.modules["core"]
bot = core.bot


def message(text, uid=4405089, chat_type="private", chat_id=None):
	import telebot
	entities = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
	return telebot.types.Message.de_json({
		"message_id": 1, "date": 0, "text": text, "entities": entities,
		"chat": {"id": chat_id if chat_id is not None else uid, "type": chat_type},
		"from": {"id": uid, "is_bot": False, "first_name": "test"},
	})


def first_message_handler(msg):
	for handler in bot.message_handlers:
		if bot._test_message_handler(handler, msg):
			return handler["function"]
	return None


def first_callback_handler(data):
	call = types.SimpleNamespace(data=data)
	for handler in bot.callback_query_handlers:
		if all(test(call) for name, test in handler["filters"].items() if name == "func"):
			return handler["function"]
	return None


def test_plugin_commands_win_over_the_core():
	assert first_message_handler(message("/status")).__module__ == "homelab.n8n_status"
	# /quota moved to the separate usage bot: the core must not see it as a plugin command.
	assert first_message_handler(message("/quota")).__module__ == "core"
	# The core still answers its own commands and the catch-all stays last.
	assert first_message_handler(message("/list")).__module__ == "core"
	assert bot.message_handlers[-1]["function"].__module__ == "core"


def test_update_buttons_reach_docker_updates():
	assert first_callback_handler("du:approve:x").__module__ == "homelab.docker_updates"
	assert first_callback_handler("something-else").__module__ == "core"


def test_command_menu_lists_the_plugins_first():
	import telebot
	merged = homelab_plugins.merge_commands([telebot.types.BotCommand("/start", "x"),
											telebot.types.BotCommand("/status", "duplicate")])
	assert [c.command for c in merged] == ["/status", "/start"]
	assert getattr(bot.set_my_commands, "__name__", "") == "<lambda>"


def test_only_administrators_are_allowed():
	menu = homelab_plugins.menu
	assert menu.allowed(message("/quota"), 4405089)
	# 440508 is a prefix of the admin id: a substring check would let it in.
	assert not menu.allowed(message("/quota", uid=440508), 440508)
	# An admin writing from a group that is not TELEGRAM_GROUP is ignored.
	assert not menu.allowed(message("/quota", chat_type="group", chat_id=-100123), 4405089)


def test_certificate_falls_back_to_the_volume():
	assert homelab_paths.certificate("/nonexistent/portainer-cert.pem") == str(homelab_paths.data_file("portainer-cert.pem"))


def test_self_update_buttons_reach_the_plugin():
	assert first_callback_handler("su:deploy:homelab-v5.0.0-h1").__module__ == "homelab.self_update"


def test_self_update_orders_versions():
	from homelab import self_update
	assert self_update.order("v5.0.0-h10") > self_update.order("v5.0.0-h9")
	assert self_update.order("v5.1.0-h1") > self_update.order("v5.0.0-h30")
	# What ran before CI tagged anything sorts first, so the first tag is offered.
	assert self_update.order("5.0.0-homelab.1") < self_update.order("v5.0.0-h1")
	assert self_update.version_of("homelab-v5.0.0-h4") == "v5.0.0-h4"


def test_self_update_keeps_the_newest_images():
	from homelab import self_update
	removed = []

	class Image:
		def __init__(self, tags):
			self.tags = tags

	class Images:
		def list(self, name):
			return [Image([f"{name}:{v}"]) for v in ("v5.0.0-h6", "v5.0.0-h7", "v5.0.0-h8", "v5.0.0-h9",
													"5.0.0-homelab.1", "portainer-status-updates-autoclean-4.2.0-12")]

		def remove(self, ref):
			removed.append(ref.split(":", 1)[1])

	import docker
	original, version = docker.from_env, self_update.VERSION
	docker.from_env = lambda *a, **k: type("Client", (), {"images": Images()})()
	self_update.VERSION = "v5.0.0-h7"   # running an older one: it is kept even if not among the newest
	try:
		self_update.prune_images()
	finally:
		docker.from_env, self_update.VERSION = original, version
	assert sorted(removed) == ["5.0.0-homelab.1", "v5.0.0-h6"], removed


def dropped_connection(*args, **kwargs):
	import requests
	raise requests.ConnectionError("Remote end closed connection without response")


def test_telegram_retries_a_dropped_connection():
	from homelab import docker_updates

	class Reply:
		ok, status_code = True, 200

		def json(self):
			return {"ok": True, "result": {"message_id": 7}}

	sent = []

	def post(url, json=None, timeout=None):
		sent.append(json["text"])
		if len(sent) == 1:
			dropped_connection()
		return Reply()

	original = docker_updates.requests.post, docker_updates.time.sleep
	docker_updates.requests.post, docker_updates.time.sleep = post, lambda seconds: None
	try:
		assert docker_updates.telegram("hi", chat=1) == 7
	finally:
		docker_updates.requests.post, docker_updates.time.sleep = original
	assert sent == ["hi", "hi"]


def test_an_update_stays_done_when_its_notice_is_lost():
	# 2026-10-22 rsshub: the container was replaced, then the "updated" message hit a dropped
	# connection and the worker marked the update failed.
	from homelab import docker_updates
	p = {"eid": 3, "cid": "old", "name": "rsshub", "environment": "Docker-VM", "old_image": "sha256:" + "a" * 64,
		 "target": {"config": "sha256:" + "b" * 64, "ref": "diygod/rsshub:latest"}}
	original = docker_updates.telegram, docker_updates.apply_update, docker_updates.API
	docker_updates.telegram, docker_updates.API = dropped_connection, lambda: None
	docker_updates.apply_update = lambda api, p, ident: {"new_id": "new", "image": "sha256:" + "b" * 64}
	try:
		for auto in (True, False):
			ident = f"lost-notice-{auto}"
			with docker_updates.db() as conn:
				conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?)",
							 (ident, 3, "old", p["target"]["config"], json.dumps(p), "approved", 9e9, 1))
			docker_updates.worker(ident, p, None, auto=auto)
			with docker_updates.db() as conn:
				row = conn.execute("SELECT status, data FROM candidates WHERE id=?", (ident,)).fetchone()
			assert row["status"] == "done", (auto, row["status"])
			assert json.loads(row["data"])["new_id"] == "new"
	finally:
		docker_updates.telegram, docker_updates.apply_update, docker_updates.API = original


def test_cleanup_covers_updates_marked_failed_by_a_lost_notice():
	from homelab import docker_updates
	rows = {"done": ("done", {"applied": 1, "new_id": "n1"}),
			"lost-notice": ("failed", {"applied": 1, "new_id": "n2"}),
			"real-failure": ("failed", {}),
			"cleanup-op": ("failed", {"applied": 1, "new_id": "n3", "operation": "cleanup"})}
	with docker_updates.db() as conn:
		conn.execute("DELETE FROM candidates")
		for ident, (status, extra) in rows.items():
			p = {"eid": 3, "cid": ident, "name": ident, "environment": "Docker-VM", **extra}
			conn.execute("INSERT INTO candidates VALUES (?,?,?,?,?,?,?,?)", (ident, 3, ident, "t", json.dumps(p), status, 9e9, 1))
	checked = []
	original = docker_updates.cleanup_ready
	docker_updates.cleanup_ready = lambda api, p: checked.append(p["cid"])
	try:
		result = docker_updates.scan_cleanup(None, notify=False)
	finally:
		docker_updates.cleanup_ready = original
	assert sorted(checked) == ["done", "lost-notice"], checked
	assert result["eligible"] == 2



def container(name, image, labels=None, status="running", host_config=None):
	"""A container as the Docker API returns it, with only the fields the plugin reads."""
	return {"Id": "c" * 64, "Name": "/" + name, "Image": "sha256:" + "a" * 64,
			"Config": {"Image": image, "Labels": labels or {}},
			"State": {"Status": status}, "HostConfig": host_config or {}}


def test_containers_update_on_their_own_by_default():
	# 2026-11-01: the daily check updates without asking unless something excludes it.
	from homelab import docker_updates as du
	settings = du.policy()
	assert du.exclusion(container("rsshub", "diygod/rsshub:latest")) is None
	assert du.auto_reason(container("rsshub", "diygod/rsshub:latest"), settings) is None
	# A container added tomorrow is covered without touching its compose.
	assert du.auto_reason(container("brand-new", "ghcr.io/x/y:latest"), settings) is None


def test_the_auto_label_still_wins_both_ways():
	from homelab import docker_updates as du
	settings = du.policy()
	# Bare label, as the upstream README writes it, is still an opt-in.
	assert du.auto_reason(container("rsshub", "diygod/rsshub:latest", {du.AUTO_LABEL: ""}), settings) is None
	reason = du.auto_reason(container("rsshub", "diygod/rsshub:latest", {du.AUTO_LABEL: "false"}), settings)
	assert reason == "标签 DCB-Auto-Update=false", reason
	# An explicit yes overrides the database rule.
	assert du.auto_reason(container("ghost-db", "mysql:8.4.10", {du.AUTO_LABEL: "true"}), settings) is None
	# false only stops the automatic part: the container is still checked and announced.
	assert du.exclusion(container("rsshub", "diygod/rsshub:latest", {du.AUTO_LABEL: "false"})) is None


def test_databases_are_not_updated_on_their_own():
	from homelab import docker_updates as du
	settings = du.policy()
	for name, image in (("n8n-postgres", "postgres:17-alpine"), ("ghost-db", "mysql:8.4.10"),
						("paperless-redis", "redis:8"), ("some-new-db", "mariadb:11")):
		assert du.auto_reason(container(name, image), settings) == "数据库不自动跨大版本", name
	# They keep being checked: the candidate is announced with its usual button.
	assert du.exclusion(container("ghost-db", "mysql:8.4.10")) is None
	assert du.exclusion(container("paperless-redis", "redis:8")) is None
	# n8n-postgres stays out of the check altogether, as before.
	assert du.exclusion(container("n8n-postgres", "postgres:17-alpine")) is not None


def test_watchtower_keeps_daidai_panel():
	from homelab import docker_updates as du
	settings = du.policy()
	assert du.auto_reason(container("daidai-panel", "linzixuanzz/daidai-panel:latest"), settings) == "由 Watchtower 负责"
	assert du.exclusion(container("daidai-panel", "linzixuanzz/daidai-panel:latest")) is None


def test_the_ignore_label_is_respected():
	# The upstream label had no effect on this plugin before; it does now.
	from homelab import docker_updates as du
	assert du.exclusion(container("x", "nginx:alpine", {du.IGNORE_LABEL: ""})) == "标签 DCB-Ignore-Check-Updates"
	assert du.exclusion(container("x", "nginx:alpine", {du.IGNORE_LABEL: "false"})) is None


def test_the_policy_file_can_turn_the_default_off():
	from homelab import docker_updates as du
	path = homelab_paths.data_file(du.POLICY_FILE)
	plain = container("rsshub", "diygod/rsshub:latest")
	try:
		path.write_text(json.dumps({"default": "announce", "database_names": ["custom-db"],
									"watchtower_managed": ["other-panel"]}))
		settings = du.policy()
		assert settings["default"] == "announce"
		assert du.auto_reason(plain, settings) == "策略设为只通知"
		# Lists add to the built-in ones; they never remove a protection.
		assert "custom-db" in settings["database_names"] and "ghost-db" in settings["database_names"]
		assert "other-panel" in settings["watchtower_managed"] and "daidai-panel" in settings["watchtower_managed"]
		# With the default back on, the added name is what keeps that one container out.
		settings["default"] = "auto"
		assert du.auto_reason(container("other-panel", "x/y:latest"), settings) == "由 Watchtower 负责"
	finally:
		path.unlink()
	# Without the file the code default is back: automatic again.
	assert du.auto_reason(plain, du.policy()) is None


import pathlib
import tempfile
import shutil
import io
import contextlib


def _policy_tmpdir():
    return pathlib.Path(tempfile.mkdtemp(prefix="dcb-policy-"))


def test_a_missing_policy_file_keeps_the_default_auto():
    from homelab import docker_updates as du
    tmp = _policy_tmpdir()
    old_env, old_root = os.environ.get("HOMELAB_DATA_DIR"), du.ROOT
    os.environ["HOMELAB_DATA_DIR"] = str(tmp)
    du.ROOT = tmp
    try:
        settings = du.policy()
        assert settings["default"] == "auto", settings
        assert settings.get("problem") is None, settings
        assert du.auto_reason(container("rsshub", "diygod/rsshub:latest"), settings) is None
    finally:
        du.ROOT = old_root
        if old_env is None:
            os.environ.pop("HOMELAB_DATA_DIR", None)
        else:
            os.environ["HOMELAB_DATA_DIR"] = old_env
        shutil.rmtree(tmp)


def test_a_damaged_policy_only_checks_and_still_scans():
    from homelab import docker_updates as du
    tmp = _policy_tmpdir()
    old_env, old_root = os.environ.get("HOMELAB_DATA_DIR"), du.ROOT
    os.environ["HOMELAB_DATA_DIR"] = str(tmp)
    du.ROOT = tmp
    try:
        bad = ("{not json", "[]", '{"default":"annouce"}', '{"default":1}',
               '{"database_names":"x"}', '{"database_names":[1]}')
        for text in bad:
            (tmp / du.POLICY_FILE).write_text(text)
            settings = du.policy()
            assert settings["default"] == "announce", (text, settings)
            assert settings.get("problem"), (text, settings)
            plain = container("rsshub", "diygod/rsshub:latest")
            assert du.auto_reason(plain, settings) == "策略文件异常，仅检查"
            # A damaged policy is a global stop: even an explicit true waits
            # until the file is fixed. The container is still checked.
            explicit = container("ghost-db", "mysql:8.4.10", {du.AUTO_LABEL: "true"})
            assert du.auto_reason(explicit, settings) == "策略文件异常，仅检查"
            assert du.exclusion(plain) is None
    finally:
        du.ROOT = old_root
        if old_env is None:
            os.environ.pop("HOMELAB_DATA_DIR", None)
        else:
            os.environ["HOMELAB_DATA_DIR"] = old_env
        shutil.rmtree(tmp)


def test_an_unreadable_or_dangling_policy_only_checks():
    from homelab import docker_updates as du
    from homelab import policy as policy_file
    tmp = _policy_tmpdir()
    old_env, old_root = os.environ.get("HOMELAB_DATA_DIR"), du.ROOT
    os.environ["HOMELAB_DATA_DIR"] = str(tmp)
    du.ROOT = tmp
    target = tmp / du.POLICY_FILE
    original_read = policy_file._read_text
    try:
        target.write_text('{"default":"auto"}')
        policy_file._read_text = lambda _: (_ for _ in ()).throw(PermissionError(13, "denied"))
        try:
            settings = du.policy()
        finally:
            policy_file._read_text = original_read
        assert settings["default"] == "announce", settings
        assert "无法读取" in settings["problem"], settings

        target.unlink()
        try:
            target.symlink_to(tmp / "missing.json")
        except (OSError, NotImplementedError):
            pass
        else:
            settings = du.policy()
            assert settings["default"] == "announce", settings
            assert "符号链接" in settings["problem"], settings
    finally:
        du.ROOT = old_root
        if old_env is None:
            os.environ.pop("HOMELAB_DATA_DIR", None)
        else:
            os.environ["HOMELAB_DATA_DIR"] = old_env
        shutil.rmtree(tmp)


def test_policy_alert_is_sent_once_and_recovery_is_reported():
    from homelab import docker_updates as du
    tmp = _policy_tmpdir()
    old_env, old_root, old_telegram = os.environ.get("HOMELAB_DATA_DIR"), du.ROOT, du.telegram
    os.environ["HOMELAB_DATA_DIR"] = str(tmp)
    du.ROOT = tmp
    sent = []
    du.telegram = lambda text, buttons=None, chat=None: sent.append(text) or 7
    try:
        (tmp / du.POLICY_FILE).write_text("{bad")
        du.notify_policy_problem(du.policy())
        du.notify_policy_problem(du.policy())
        assert len(sent) == 1, sent
        assert "Baihaha" in sent[0] and "策略文件异常" in sent[0], sent[0]

        (tmp / du.POLICY_FILE).unlink()
        du.notify_policy_problem(du.policy())
        assert len(sent) == 2, sent
        assert "恢复正常" in sent[1], sent[1]
    finally:
        du.ROOT = old_root
        du.telegram = old_telegram
        if old_env is None:
            os.environ.pop("HOMELAB_DATA_DIR", None)
        else:
            os.environ["HOMELAB_DATA_DIR"] = old_env
        shutil.rmtree(tmp)


def test_a_damaged_policy_never_auto_updates_in_scan():
    from homelab import docker_updates as du
    tmp = _policy_tmpdir()
    old_env, old_root = os.environ.get("HOMELAB_DATA_DIR"), du.ROOT
    os.environ["HOMELAB_DATA_DIR"] = str(tmp)
    du.ROOT = tmp
    (tmp / du.POLICY_FILE).write_text("{bad")

    class FakeAPI:
        envs = {3: "Docker-VM"}

        def call(self, eid, path, method="GET", data=None, raw=False, timeout=90):
            if path.startswith("/containers/json"):
                return [{"Id": "c1", "Names": ["/rsshub"]}]
            if path == "/containers/c1/json":
                return container("rsshub", "diygod/rsshub:latest")
            if path.startswith("/images/"):
                return {"Id": "sha256:" + "a" * 64, "Architecture": "amd64",
                        "Os": "linux", "Variant": ""}
            raise AssertionError(path)

    sent, workers = [], []
    originals = (du.API, du.resolve_image, du.scan_cleanup, du.prune_images,
                 du.prune_build_cache, du.worker, du.telegram)
    du.API = FakeAPI
    du.resolve_image = lambda api, eid, ref, local: {
        "config": "sha256:" + "b" * 64, "digest": "sha256:" + "c" * 64,
        "ref": ref, "platform": "amd64"}
    du.scan_cleanup = lambda api, notify=True: {"stub": True}
    du.prune_images = lambda api, notify=True: {"stub": True}
    du.prune_build_cache = lambda api, notify=True: {"stub": True}
    du.worker = lambda *args, **kwargs: workers.append((args, kwargs))
    du.telegram = lambda text, buttons=None, chat=None: sent.append(text) or 7
    try:
        result = du.scan(notify=True)
        assert result["checked"] == 1, result
        assert result["updates"] == 1, result
        assert result["would_auto"] == 0, result
        assert result["auto"] == 0, result
        assert result["policy"]["problem"], result
        assert workers == [], workers
        assert any("策略文件异常" in text for text in sent), sent
        first = len(sent)
        du.scan(notify=True)
        assert len(sent) == first, sent
    finally:
        (du.API, du.resolve_image, du.scan_cleanup, du.prune_images,
         du.prune_build_cache, du.worker, du.telegram) = originals
        du.ROOT = old_root
        if old_env is None:
            os.environ.pop("HOMELAB_DATA_DIR", None)
        else:
            os.environ["HOMELAB_DATA_DIR"] = old_env
        shutil.rmtree(tmp)


def test_the_policy_tool_saves_atomically_and_refuses_damage():
    from homelab import policy as policy_file
    tmp = _policy_tmpdir()
    target = tmp / policy_file.FILENAME
    original = policy_file.path
    policy_file.path = lambda: target
    try:
        policy_file.save({"default": "announce", "database_names": ["custom-db"]})
        stored = json.loads(target.read_text())
        assert stored["default"] == "announce"
        assert stored["database_names"] == ["custom-db"]
        assert (target.stat().st_mode & 0o777) == 0o600
        assert not list(tmp.glob(".*.tmp"))
        settings = policy_file.load()
        assert settings["default"] == "announce"
        assert "custom-db" in settings["database_names"]

        for bad in ({"default": "bogus"}, {"database_names": "x"}, []):
            try:
                policy_file.save(bad)
            except policy_file.Problem:
                pass
            else:
                raise AssertionError(bad)

        target.write_text("{bad")
        try:
            policy_file.save({"default": "auto"})
        except policy_file.Problem:
            pass
        else:
            raise AssertionError("overwrote a damaged policy without replace")
        policy_file.save({"default": "auto"}, replace=True)
        assert json.loads(target.read_text())["default"] == "auto"
    finally:
        policy_file.path = original
        shutil.rmtree(tmp)


def test_a_failed_policy_save_leaves_the_old_file():
    from homelab import policy as policy_file
    tmp = _policy_tmpdir()
    target = tmp / policy_file.FILENAME
    original_path, original_replace = policy_file.path, policy_file.os.replace
    policy_file.path = lambda: target
    try:
        target.write_text('{"default": "announce"}')
        policy_file.os.replace = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
        try:
            policy_file.save({"default": "auto"})
        except OSError:
            pass
        else:
            raise AssertionError("save should have failed")
        assert json.loads(target.read_text())["default"] == "announce"
        assert not list(tmp.glob(".*.tmp"))
    finally:
        policy_file.path, policy_file.os.replace = original_path, original_replace
        shutil.rmtree(tmp)


def test_the_policy_tool_cli_keeps_unknown_keys_and_validates():
    from homelab import policy as policy_file
    tmp = _policy_tmpdir()
    target = tmp / policy_file.FILENAME
    original = policy_file.path
    policy_file.path = lambda: target
    out = io.StringIO()
    try:
        target.write_text(json.dumps({"default": "auto", "future": {"keep": 1},
                                      "database_names": ["custom-db"]}))
        with contextlib.redirect_stdout(out):
            assert policy_file.main(["--set-default", "announce",
                                     "--add-watchtower", "other-panel"]) == 0
        stored = json.loads(target.read_text())
        assert stored["default"] == "announce"
        assert stored["future"] == {"keep": 1}
        assert "other-panel" in stored["watchtower_managed"]

        with contextlib.redirect_stdout(out):
            assert policy_file.main(["--remove-database-name", "custom-db"]) == 0
        assert "custom-db" not in json.loads(target.read_text())["database_names"]

        target.write_text("{bad")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            assert policy_file.main(["--set-default", "auto"]) == 2
        assert target.read_text() == "{bad"
        with contextlib.redirect_stdout(out):
            assert policy_file.main(["--set-default", "auto", "--replace"]) == 0
        assert json.loads(target.read_text())["default"] == "auto"
    finally:
        policy_file.path = original
        shutil.rmtree(tmp)

if __name__ == "__main__":
	failed = 0
	for name, fn in sorted(globals().items()):
		if name.startswith("test_") and callable(fn):
			try:
				fn()
				print(f"ok    {name}")
			except Exception as ex:
				failed += 1
				print(f"FAIL  {name}: {type(ex).__name__}: {ex}")
	sys.exit(1 if failed else 0)
