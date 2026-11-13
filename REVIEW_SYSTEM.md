# PR assessment workflow

## Local reviews and publication

Follow AGENTS.md: inspect unique stacked-PR changes and their integration against
the plan, run independent tests, and distinguish defects from unverified or
environment-limited results. Save raw logs and environments under ignored `.review/`.
Write the publishable assessment under `reviews/YYYY-MM-DD.md`; use another
filename suffix for multiple sessions on the same day. Include exact commits,
commands, dependencies, findings with stable IDs, and retest status.

Preview and publish with Python 3.12 or later:

```powershell
python tools/publish_review.py --report reviews/2026-11-12.md
python tools/publish_review.py --report reviews/2026-11-12.md --publish
```

The publisher uses Git's existing GitHub credential helper, or GH_TOKEN /
GITHUB_TOKEN from the environment. Never put tokens in files or command arguments.
It finds or creates the tracking issue, mentions @avyukthNarra in a dated report,
and skips an identical report using a normalized content hash. It does not post
inline PR comments, approve PRs, merge changes, or generate an AI assessment.

Avyukth replies with finding IDs and fix commits or explanations. A subsequent
review tests those exact fixes and publishes updated open/resolved/deferred status.

## GitHub Actions

After `.github/workflows/publish-review.yml` reaches the default branch, run
"Publish PR assessment" manually with a report path. Committing reviewed reports
under `reviews/*.md` to main also triggers publication. The job tests the publisher
and uses GitHub's job token with contents:read and issues:write. A shared concurrency
group serializes workflow runs; content hashes avoid repeat report notifications.
Do not run privileged publishing jobs on untrusted PR code.

This workflow automates delivery of prepared reports, not unattended AI review or
fresh testing of arbitrary PRs. That later stage needs a chosen AI service, scoped
credentials/cost limits, isolated unprivileged test jobs, and a versioned evidence
bundle. It must never describe an unexecuted test as passing. No background
schedule or PR-update-triggered AI job is enabled yet.

Tests for the publisher use a fake GitHub API and never send messages:

```text
python -m unittest discover -s tools -p test_publish_review.py -v
```
