"""Homelab plugins: /status, the Portainer update flow and the bot's own updates.

Imported by homelab/main.py once the upstream modules have registered their
commands and callbacks. The core registers a catch-all message handler and a
catch-all callback handler, and Telegram dispatches to the first handler that
matches, so the plugin handlers are moved in front of the core's ones. No
upstream file is edited.
"""
import telebot

import core
import host_registry
from logger import error
from homelab import docker_updates, n8n_status, self_update

# Cross-border Tailscale SSH (home -> osaka-lab) can hit 300ms+ RTT and packet
# loss during evening peak, making a cold SSH handshake exceed the upstream 5s
# probe deadline and triggering drop() in a loop. Raise probe deadline to 15s
# without touching upstream src/host_registry.py.
CROSS_BORDER_PROBE_SECONDS = 15


def tune_host_probes():
	host_registry.PROBE_TIMEOUT_SECONDS = CROSS_BORDER_PROBE_SECONDS
	host_registry.reachable_hosts.__defaults__ = (CROSS_BORDER_PROBE_SECONDS,)
	host_registry.host_status.__defaults__ = (CROSS_BORDER_PROBE_SECONDS,)
	host_registry.status_snapshot.__defaults__ = (CROSS_BORDER_PROBE_SECONDS, None)

COMMANDS = [
	telebot.types.BotCommand("/status", "查询 Home Lab 三台主机实时状态"),
]


class Menu:
	"""What the plugins need from the bot: who may use them, and how to answer."""

	def __init__(self, bot):
		self.bot = bot

	def allowed(self, message, uid):
		return core.is_admin(uid) and core.is_allowed_origin(message.chat, uid)

	def send(self, message, text, markup=None):
		kwargs = {"reply_markup": markup, "parse_mode": "HTML"}
		if getattr(message.chat, "is_forum", False):
			kwargs["message_thread_id"] = getattr(message, "message_thread_id", None)
		return self.bot.send_message(message.chat.id, text, **kwargs)


def _to_front(handlers, count):
	"""Moves the last `count` handlers, in order, to the front of the list."""
	if count:
		added = handlers[-count:]
		del handlers[-count:]
		handlers[0:0] = added


def install(bot):
	tune_host_probes()
	before = len(bot.message_handlers), len(bot.callback_query_handlers)
	menu = Menu(bot)
	for plugin in (docker_updates, n8n_status, self_update):
		# A broken plugin must not take the bot down with it.
		try:
			plugin.install(bot, menu)
		except Exception as ex:
			error(f"homelab plugin {plugin.__name__} not installed: {type(ex).__name__}: {ex}")
	_to_front(bot.message_handlers, len(bot.message_handlers) - before[0])
	_to_front(bot.callback_query_handlers, len(bot.callback_query_handlers) - before[1])

	original = bot.set_my_commands
	bot.set_my_commands = lambda commands, *args, **kwargs: original(merge_commands(commands), *args, **kwargs)
	return menu


def merge_commands(commands):
	"""The plugin commands first, then the core's, without duplicates."""
	ours = {c.command.lstrip("/") for c in COMMANDS}
	return COMMANDS + [c for c in commands if c.command.lstrip("/") not in ours]


menu = install(core.bot)
