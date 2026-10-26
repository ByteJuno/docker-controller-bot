# Homelab CI release gate

`homelab.yml` prepares one immutable Git bundle, then tests the **same commit**
in Python 3.14, the actual `Dockerfile.homelab` Alpine image, and real Docker on
Ubuntu 24.04/26.04. The plugin suite and standalone health/CI regression suites
run before publication. The image probe is checked with a missing, fresh and
stale heartbeat in a disposable container without network, config or sockets.

## Upstream releases

New upstream releases are no longer merged on a schedule. `upstream_merge.py`
on the lab has gpt-6.1-sol review each new upstream release, merge it on an
`upstream-sync/<tag>` branch (resolving conflicts if needed) and open a pull
request into `homelab`. Pull requests run every test job but skip publish and
the failure report; the script reads the checks, merges the pull request when
they pass, and reports to the owner on Telegram. That merge is a normal push to
`homelab`, which publishes the deployable tag. A manual dispatch still merges
the newest upstream release directly.

## Publish boundary

The publish job runs on a fresh hosted runner and only downloads the exact
artifact ID exported by the prepare job. It never runs candidate scripts,
installs its requirements, or invokes its Git hooks. Only its final push step
receives the repository-scoped `DCB_SYNC_SSH_KEY`. Its temporary key is removed
by an EXIT trap and an always-run cleanup step. The default GitHub token is
read-only; only the independent failure reporter has issue-write permission.

The candidate SHA, version tag and current remote branch are checked before
push. `homelab`, its upstream version tag and its deployable tag are pushed
atomically, without force. Existing tags may only resolve to the same commit.
If someone advances `homelab` while tests run, publication fails instead of
releasing the stale candidate. Rerun from the current branch; do not force-push.

Upstream test scripts run without repository write credentials. The bundle
artifact expires after three days; an older rerun may need a new workflow run.

## Failure notifications

The terminal report job runs after **any failed prepare, test, build, integration
or publish job**. It queries this run's jobs and records exact failed step names
and links in one open `upstream-sync` issue. Existing failures update that issue
instead of opening duplicates. The existing bot issue poller carries this into
Telegram on its normal polling interval; this is not an instant webhook.

`tests/test_ci_pipeline.py` exercises merge/build/test/publish failures, timeouts,
pagination and duplicate suppression offline, without creating GitHub issues or
sending Telegram messages. The `report` job is skipped after a successful run.

This pipeline **never deploys the production container**. New versions continue
to require the existing Telegram Deploy confirmation. The new heartbeat check
only takes effect when an image containing it is deployed.

## Failure delivery prerequisite and OOM regression

Repository **Issues must remain enabled**: `upstream-sync` is the bot's existing
failure-delivery channel. Preparation now verifies this prerequisite before
running expensive tests. Reporting preserves the Markdown summary as a retained
artifact even if an issue API call fails. A disabled-Issues response gives an
explicit diagnostic rather than a bare HTTP error.

For Docker exit 137, event monitoring falls back to inspecting the exact
container ID and requires `OOMKilled=true`, `Running=false`, and exit code 137.
SIGKILL alone is never called an OOM; explicit requested stops still win. The
normal `oom`-before-`die` fast path remains unchanged. Eight isolated regression
tests cover missing/late OOM events, ordinary SIGKILL, requested stops, missing
containers and restart-state races. The original real-Docker event test remains
strict and is additionally repeated three times on **each** Docker runner.
