"""Container health based on the heartbeat queued through the bot's workers.

This deliberately does not import core or connect to Telegram/Docker. An alive
Python interpreter alone is not evidence that the bot can process commands.
"""
import os
import time

HEARTBEAT_PATH = "/tmp/dcb-heartbeat"
MAX_AGE = 120


def healthy(path=HEARTBEAT_PATH, now=None):
	try:
		modified = os.stat(path).st_mtime
	except OSError:
		return False
	age = (time.time() if now is None else now) - modified
	# A small amount of clock skew is harmless; a far-future timestamp is not.
	return -5 <= age < MAX_AGE


if __name__ == "__main__":
	raise SystemExit(0 if healthy() else 1)
