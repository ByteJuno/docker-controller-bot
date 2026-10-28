"""Standalone regression tests; no bot, network, Docker socket or credentials."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from homelab.healthcheck import MAX_AGE, healthy  # noqa: E402


def test_missing_heartbeat_fails():
	with tempfile.TemporaryDirectory() as directory:
		assert not healthy(Path(directory) / "missing", now=1000)


def test_recent_heartbeat_succeeds():
	with tempfile.TemporaryDirectory() as directory:
		path = Path(directory) / "beat"
		path.touch()
		os.utime(path, (1000, 1000))
		assert healthy(path, now=1001)
		assert healthy(path, now=1000 + MAX_AGE - 0.1)


def test_expired_and_future_heartbeat_fail():
	with tempfile.TemporaryDirectory() as directory:
		path = Path(directory) / "beat"
		path.touch()
		os.utime(path, (1000, 1000))
		assert not healthy(path, now=1000 + MAX_AGE)
		assert not healthy(path, now=1000 + MAX_AGE + 1)
		assert not healthy(path, now=990)


def test_dockerfile_uses_the_same_healthcheck():
	text = (REPO / "Dockerfile.homelab").read_text()
	assert 'CMD ["python3", "homelab/healthcheck.py"]' in text
	assert 'sys.exit(0)' not in text
	assert 'HEARTBEAT_PATH = "/tmp/dcb-heartbeat"' in (REPO / "src" / "core.py").read_text()


def image_smoke(image):
	"""Exercise Docker's configured probe in an isolated, network-less container.

	Never start the bot: override ENTRYPOINT, use no socket/config/secrets, and
	require missing/stale beats to fail, plus a fresh beat to succeed.
	"""
	probe = json.loads(subprocess.check_output([
		"docker", "image", "inspect", image, "--format", "{{json .Config.Healthcheck.Test}}"
	], text=True))
	assert probe == ["CMD", "python3", "homelab/healthcheck.py"], probe
	source = """
import os, subprocess, time
path = '/tmp/dcb-heartbeat'
command = ['python3', 'homelab/healthcheck.py']
assert subprocess.run(command).returncode == 1, 'missing beat accepted'
with open(path, 'w'):
    pass
assert subprocess.run(command).returncode == 0, 'fresh beat rejected'
old = time.time() - 121
os.utime(path, (old, old))
assert subprocess.run(command).returncode == 1, 'stale beat accepted'
print('image probe: missing=fail, fresh=pass, stale=fail')
"""
	subprocess.run([
		"docker", "run", "--rm", "--network", "none", "--read-only", "--tmpfs", "/tmp",
		"--entrypoint", "python3", image, "-c", source
	], check=True, timeout=60)


if __name__ == "__main__":
	if sys.argv[1:2] == ["--image"]:
		image_smoke(sys.argv[2])
	else:
		failed = 0
		for name, function in sorted(list(globals().items())):
			if name.startswith("test_") and callable(function):
				try:
					function()
					print(f"ok    {name}")
				except Exception as error:
					failed += 1
					print(f"FAIL  {name}: {type(error).__name__}: {error}")
		raise SystemExit(1 if failed else 0)
