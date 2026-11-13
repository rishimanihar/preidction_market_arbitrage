# Security and secrets policy

This repository is **public**. Anything committed, even briefly, should be treated as leaked forever.

## Never commit
- API key ids, private keys (`.pem`, Ed25519/RSA), signatures, session tokens, `.env` files.
- Recorded market data, raw API dumps, SQLite databases, logs. These live in the gitignored `data/` folder inside your checkout (`PMARB_DATA_DIR`, default `./data`) and never leave the team's machines. Venue terms restrict publishing API data; keep it private.
- Live API values pasted into docs, issues, or PRs — use synthetic examples instead.
- Account identifiers, balances, fills, or screenshots of either.

## Where credentials live
- Private keys: files under `~/.config/pmarb/` with mode `600`. Config refers to them by path (see `.env.example`).
- On Windows, mode bits do not apply. Key files and the settings file live under `%USERPROFILE%\.config\pmarb\`. Their ACLs are restricted to the owner with `icacls <file> /inheritance:r /grant:r "*<your SID>:(R)"`. The checks refuse a file that Everyone, Users, BUILTIN\Users or Authenticated Users can read, read the ACL through `icacls`, and warn loudly when `icacls` cannot run (see `docs/RUN_ON_WINDOWS.md`).
- The application must refuse to start if a key file is inside the repository, is group/world readable (on Windows: readable by a broad group), or if a secret value appears in a config file tracked by git.
- Each person uses their own venue account and their own API key. Never share keys between people.
- Use the narrowest scope each venue offers (Kalshi `read`; Polymarket US read-only if available). Observation and replay never need trading permission. Trading keys are out of scope until the live-execution milestone is explicitly approved.
- Never paste keys into chats, issues, PRs, commit messages, or AI agent prompts.

## Code rules
- Signing headers (`KALSHI-ACCESS-SIGNATURE`, `X-PM-*`) and key material are never logged; the logging filter redacts them and a test enforces it.
- Observation and replay commands must not construct an order-submission client (enforced by test).
- Dependencies are pinned; CI actions are pinned to commit SHAs.

## Guardrails in place
- `.gitignore` blocks keys, env files, data, databases, and logs.
- `pre-commit` runs gitleaks, private-key detection, large-file checks, and blocks direct commits to `main`.
- CI runs gitleaks over full history on every push and PR.

## If a secret leaks
1. **Revoke/rotate the key at the venue immediately** — deleting the commit is not enough; forks, clones, and caches keep it.
2. Tell the other maintainer.
3. Then clean history (`git filter-repo`) and force-push only after coordinating.

## Recommended GitHub settings (repo owner)
Enable secret scanning + push protection, and a branch protection rule on `main` (PR required, CI must pass, no force-push).
