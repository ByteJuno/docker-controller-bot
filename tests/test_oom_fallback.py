"""Regression coverage for OOM detection when Docker's oom event is absent/late."""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness
core, store, _root = harness.load_bot()


def monitor(state=None):
	store.set('hosts', [{'id': 'h_local', 'alias': 'local', 'url': 'unix:///var/run/docker.sock', 'local': True}])
	result = core.DockerEventMonitor('h_local')
	messages = []
	result._announce = lambda text, detail=None: messages.append((text, detail))
	result._last_logs = lambda container_id: None
	result._inspect = lambda container_id: SimpleNamespace(attrs={'State': state}) if state is not None else None
	return result, messages


def event(action, **attrs):
	return {'Type': 'container', 'Action': action, 'Actor': {'ID': 'a'*64, 'Attributes': dict(attrs, name='app')}}


def test_oom_state_recovers_a_missing_oom_event():
	m, messages = monitor({'OOMKilled': True, 'Running': False, 'ExitCode': 137})
	m._handle_event(event('die', exitCode='137'))
	assert messages == [(core.get_text('container_oom', 'app'), None)], messages


def test_late_oom_event_does_not_send_a_second_notification():
	m, messages = monitor({'OOMKilled': True, 'Running': False, 'ExitCode': 137})
	m._handle_event(event('die', exitCode='137'))
	m._handle_event(event('oom'))
	assert messages == [(core.get_text('container_oom', 'app'), None)], messages


def test_plain_sigkill_is_not_reported_as_oom():
	m, messages = monitor({'OOMKilled': False, 'Running': False, 'ExitCode': 137})
	m._handle_event(event('die', exitCode='137'))
	assert messages == [(core.get_text('container_failed', 'app', '137 (SIGKILL)'), None)], messages


def test_requested_stop_takes_precedence_over_inspect_fallback():
	m, messages = monitor({'OOMKilled': True, 'Running': False, 'ExitCode': 137})
	m._handle_event(event('kill', signal='9'))
	m._inspect = lambda _: (_ for _ in ()).throw(AssertionError('explicit stop must not inspect'))
	m._handle_event(event('die', exitCode='137'))
	assert messages == [(core.get_text('stopped_container', 'app'), None)], messages


def test_inspect_unavailable_keeps_generic_exit_reason():
	m, messages = monitor()
	m._handle_event(event('die', exitCode='137'))
	assert messages == [(core.get_text('container_failed', 'app', '137 (SIGKILL)'), None)], messages


def test_running_new_instance_is_not_attributed_to_previous_oom():
	m, messages = monitor({'OOMKilled': True, 'Running': True, 'ExitCode': 137})
	m._handle_event(event('die', exitCode='137'))
	assert messages == [(core.get_text('container_failed', 'app', '137 (SIGKILL)'), None)], messages


def test_non_sigkill_exit_never_fetches_oom_state():
	m, messages = monitor()
	m._inspect = lambda _: (_ for _ in ()).throw(AssertionError('unnecessary inspect'))
	m._handle_event(event('die', exitCode='3'))
	assert messages == [(core.get_text('container_failed', 'app', '3'), None)], messages


def test_oom_flag_without_matching_exit_code_is_not_enough():
	m, messages = monitor({'OOMKilled': True, 'Running': False, 'ExitCode': 0})
	m._handle_event(event('die', exitCode='137'))
	assert messages == [(core.get_text('container_failed', 'app', '137 (SIGKILL)'), None)], messages


if __name__ == '__main__':
	failed = 0
	for name, test in sorted(list(globals().items())):
		if name.startswith('test_') and callable(test):
			try:
				test()
				print('ok   ' + name)
			except Exception:
				import traceback
				traceback.print_exc()
				failed += 1
	raise SystemExit(bool(failed))
