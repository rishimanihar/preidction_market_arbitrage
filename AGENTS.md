# PR review workflow

When reviewing avyukthNarra's PRs in this workspace:

- Compare the actual implementation against the implementation plan and active decisions, including dependencies between stacked PRs.
- Perform independent testing locally when practical. Do not treat GitHub CI or the author's reported test counts as locally verified results.
- Keep the user's checkout and working changes intact; use an isolated source copy or worktree for testing.
- Run the relevant existing tests and add focused checks for meaningful integration gaps or suspected defects. Do not change production code during an assessment unless asked to fix it.
- Save an assessment report with exact commit IDs, environment and dependency versions, reproducible commands, test counts, failure evidence, limitations, and prioritized next steps.
- Distinguish baseline suite failures, independently reproduced defects, environment limitations, and unverified hypotheses. Do not count missing credentials or private datasets as application failures.
- Include these independent test results in the report prepared for Avyukth. Follow the user's delivery authorization; do not claim that a report was sent unless publication actually succeeded.
- Live authenticated tests and long soak runs must be identified separately from local synthetic tests. Never submit orders as part of a review.
