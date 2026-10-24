"""Offline regression tests for stage-aware CI failure reporting."""
import importlib.util
from pathlib import Path
import sys
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ci_report_failure", ROOT / "ci/report_failure.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_each_failed_stage_includes_its_step_and_run_link():
	for name in ("Merge newest upstream", "Unit and plugin tests", "Build homelab image",
				 "Docker integration tests", "Push tested refs and deployable tag"):
		jobs = [{"name": "pipeline", "conclusion": "failure", "html_url": "https://example.invalid/job/1",
				 "steps": [{"name": name, "conclusion": "failure"}]}]
		title, body = report.format_report("owner/repo", 123, 2, jobs, {})
		assert name in title and name in body
		assert "/actions/runs/123/attempts/2" in body
		assert report.MARKER in body
		assert "No automatic production deployment" in body


def test_timeouts_and_missing_step_data_are_reported():
	jobs = [{"name": "image", "conclusion": "timed_out", "steps": []},
			{"name": "good", "conclusion": "success", "steps": []}]
	failures = report.failure_details(jobs)
	assert failures == [{"job": "image", "steps": ["timed_out"], "url": ""}]
	title, body = report.format_report("owner/repo", 123, 1, [], {"publish": "failure", "unit": "success"})
	assert "publish" in title and "publish" in body


def test_job_pagination_does_not_drop_a_failed_publish():
	calls = []
	def request(path):
		calls.append(path)
		if path.endswith("&page=1"):
			return {"jobs": [{"name": "unit", "conclusion": "success"}] * 100}
		return {"jobs": [{"name": "publish", "conclusion": "failure", "steps": []}]}
	jobs = report.collect_jobs("owner/repo", 1, 2, request)
	assert len(jobs) == 101 and len(calls) == 2
	assert report.failure_details(jobs)[0]["job"] == "publish"


def test_repeat_failure_updates_one_existing_alert():
	calls = []
	def request(path, method="GET", data=None):
		calls.append((path, method, data))
		if method == "GET":
			return [{"number": 17, "body": report.MARKER}]
		return {"html_url": "https://example.invalid/issues/17"}
	report.upsert_issue("owner/repo", "title", "body", request)
	assert calls[-1] == ("repos/owner/repo/issues/17", "PATCH", {"title": "title", "body": "body"})
	assert not any(method == "POST" for _, method, _ in calls)


def test_missing_label_is_created_before_first_alert():
	calls = []
	def request(path, method="GET", data=None):
		calls.append((path, method, data))
		if "/issues?" in path:
			return []
		if path.endswith("/labels/upstream-sync"):
			raise urllib.error.HTTPError(path, 404, "missing", {}, None)
		return {"html_url": "https://example.invalid/issues/1"}
	report.upsert_issue("owner/repo", "title", "body", request)
	posts = [(path, data) for path, method, data in calls if method == "POST"]
	assert posts[0][0].endswith("/labels")
	assert posts[1][1]["labels"] == ["upstream-sync"]


def test_main_normalises_needs_results_without_live_api():
	import json
	import os
	from unittest.mock import patch
	captured = {}
	def save(repository, title, body):
		captured.update(repository=repository, title=title, body=body)
		return {"html_url": "https://example.invalid/issues/1"}
	with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "1",
			"GITHUB_RUN_ATTEMPT": "2", "JOB_RESULTS": json.dumps({"publish": {"result": "failure"}}),
			"GITHUB_STEP_SUMMARY": ""}), patch.object(report, "collect_jobs", return_value=[]), \
			patch.object(report, "upsert_issue", side_effect=save):
		report.main()
	assert "publish" in captured["title"] and "publish" in captured["body"]
	assert "/attempts/2" in captured["body"]


if __name__ == "__main__":
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
