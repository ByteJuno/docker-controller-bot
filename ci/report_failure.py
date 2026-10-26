"""Summarise every failed CI stage in one upstream-sync issue.

Runs in a separate, issue-only job; no deploy key or production credentials.
The bot's existing upstream-sync issue poller delivers the notice to Telegram.
"""
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

LABEL = "upstream-sync"
MARKER = "<!-- homelab-ci-failure -->"


def failure_details(jobs):
	failures = []
	for job in jobs:
		if job.get("conclusion") not in ("failure", "timed_out", "startup_failure", "action_required"):
			continue
		steps = [step["name"] for step in job.get("steps", [])
				 if step.get("conclusion") in ("failure", "timed_out")]
		failures.append({"job": job["name"], "steps": steps or [job.get("conclusion", "failure")],
						 "url": job.get("html_url", "")})
	return failures


def format_report(repository, run_id, attempt, jobs, results):
	failures = failure_details(jobs)
	if not failures:
		failures = [{"job": name, "steps": [status], "url": ""}
					for name, status in results.items() if status == "failure"]
	stages = "; ".join(f"{item['job']}: {', '.join(item['steps'])}" for item in failures)
	title = ("upstream-sync: CI failed — " + (stages or "see Actions"))[:240]
	url = f"https://github.com/{repository}/actions/runs/{run_id}/attempts/{attempt}"
	body = [MARKER, "## Homelab CI failed", "", f"Actions: {url}", "",
			"| Job | Failed step | Log |", "| :--- | :--- | :--- |"]
	for item in failures:
		job = item["job"].replace("|", "\\|")
		steps = ", ".join(item["steps"]).replace("|", "\\|")
		link = f"[Open job]({item['url']})" if item["url"] else f"[Open run]({url})"
		body.append(f"| {job} | {steps} | {link} |")
	body += ["", "No automatic production deployment was requested by this workflow.",
			 "A publish failure can occur before or after a remote write; inspect the refs before retrying.",
			 "Deployment continues to require the existing Telegram confirmation."]
	return title, "\n".join(body) + "\n"


def api(path, method="GET", data=None):
	token = os.environ["GH_TOKEN"]
	headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
			   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "homelab-ci-reporter"}
	payload = None if data is None else json.dumps(data).encode()
	request = urllib.request.Request("https://api.github.com/" + path, data=payload, headers=headers, method=method)
	with urllib.request.urlopen(request, timeout=30) as response:
		return json.load(response)


def collect_jobs(repository, run_id, attempt, request=api):
	jobs = []
	for page in range(1, 21):
		response = request(f"repos/{repository}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
		batch = response.get("jobs", [])
		jobs.extend(batch)
		if len(batch) < 100:
			return jobs
	raise RuntimeError("Job list exceeded the bounded pagination limit")


def upsert_issue(repository, title, body, request=api):
	# Keep one active pipeline alert; repeat failures update it instead of spamming.
	existing = None
	for page in range(1, 11):
		issues = request(f"repos/{repository}/issues?labels={LABEL}&state=open&per_page=100&page={page}")
		for issue in issues:
			if MARKER in (issue.get("body") or "") and not issue.get("pull_request"):
				existing = issue
				break
		if existing or len(issues) < 100:
			break
	else:
		raise RuntimeError("Issue list exceeded the bounded pagination limit")
	if existing:
		return request(f"repos/{repository}/issues/{existing['number']}", "PATCH", {"title": title, "body": body})
	try:
		request(f"repos/{repository}/labels/{LABEL}")
	except urllib.error.HTTPError as error:
		if error.code != 404:
			raise
		request(f"repos/{repository}/labels", "POST", {"name": LABEL, "color": "D93F0B"})
	return request(f"repos/{repository}/issues", "POST", {"title": title, "body": body, "labels": [LABEL]})


def main():
	repository = os.environ["GITHUB_REPOSITORY"]
	run_id = int(os.environ["GITHUB_RUN_ID"])
	attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
	needs = json.loads(os.environ.get("JOB_RESULTS", "{}"))
	results = {name: value.get("result") if isinstance(value, dict) else value
			   for name, value in needs.items()}
	title, body = format_report(repository, run_id, attempt, collect_jobs(repository, run_id, attempt), results)
	if os.environ.get("GITHUB_STEP_SUMMARY"):
		with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as stream:
			stream.write(body)
	# Preserve an independent copy before attempting issue creation/update.
	if os.environ.get("RUNNER_TEMP"):
		Path(os.environ["RUNNER_TEMP"], "homelab-failure-report.md").write_text(body)
	issue = upsert_issue(repository, title, body)
	print(f"Failure reported: {issue['html_url']}")


if __name__ == "__main__":
	try:
		main()
	except urllib.error.HTTPError as error:
		# Never dump request headers or credentials into an Actions log.
		if error.code == 410:
			raise SystemExit("Repository Issues are disabled. Enable Issues; the report remains in the job summary/artifact.") from None
		raise SystemExit(f"Failure reporting API returned HTTP {error.code}; see preserved report artifact") from None
