# Progress log

This is the build's living status: what's done, what we've learned that isn't in the plan yet, and what comes next. **Update it at every milestone step.** Read it alongside `docs/IMPLEMENTATION_PLAN.md` (what we're building) and `docs/research/` (why).

_Last updated: 2026-11-12 14:45 CST_

## Status (2026-11-12 20:15 CST)

**On `m1/integration` (725 tests):** M1, plus #19 (Windows), #20 (retry fix) and #21 (security hardening). The Grok orchestrator merged #19–#21 after a fresh-worktree review. **#22 (M3 replay)** is open; it is stacked on `m1/integration` and passes 739 tests.

**CI:**
- **Linux** passes on #22.
- **Windows** fails 6 tests on #22. One is a real bug: `secrets.py` uses `os.O_NOFOLLOW` unconditionally, so loading a key on Windows would crash.
- **Fix:** a Grok worker is on branch `m1/windows-ci-fix`.

**Evening soak (`data/soak-2026-11-12-evening`, private):**
- **First start (17:30):** died at 17:32. It was a child of the hand-off orchestrator's shell and was killed when that shell exited. `nohup` does not protect against this.
- **Restart (20:04):** the collector now runs in its own session (`start_new_session`), under `caffeinate -is`, with 8 groups.
- **Health:** most evaluations are rejected as `books_not_fresh`. Kalshi polls take about 3.4 s against a 4 s age limit and a 3 s budget, and there are ReadTimeout bursts while the network is flaky. `pmarb report` will show how much valid coverage this run produced.
- **Power:** the laptop is on battery, and `-s` holds only on AC power.

**Overnight 2026-11-13:**
- **Evening soak stopped** cleanly at 02:35 CST after about 9 h. About 35 min was lost to laptop sleep. `pmarb report` and `pmarb sim` outputs are private under `data/`.
- **Review #25:** findings answered on the issue. PR #27 contains the fixes, and Rishi's 7 acceptance checks pass.
- **PR #26 (simulator):** audited and fixed. Haircut monotonicity, per-episode deduplication, arrival pricing, fees on both legs, and fresh-books-only fills are now enforced.
- **Early signal:** the simulated edge stays positive from 100 to 2000 ms of delay. This is not evidence yet: it is one evening, with scenario fees and auto-reviewed groups.
- **Gap:** `game_state` is empty during `pmarb scan`, because the recorder is not wired into the collector.

**Earlier (13:30):**

| PR | Item | State |
|---|---|---|
| #1–#10, #12 | Foundation, contracts, foundations, adapters, matching v1/v2, review store, poller | open |
| #7 | Economics + fees | open |
| #8 | Review app + CLI | open |
| #13 | Game-state recorder (Kalshi `/live_data`) | open |
| #14 / #15 | M2: Polymarket US / Kalshi WebSocket streams | open |
| #11 | M2: request signing | open |
| #16 | `pmarb report` (M1 analysis) | open |
| #17 / #18 | Deploy templates / M2-D stream integration | open |
| #19 / #20 / #21 | Windows / retry fix / security hardening | open; merged into `m1/integration` |
| #22 | M3 replay (`pmarb replay run/verify`) | open; stacked on `m1/integration` |
| `m1/windows-ci-fix` | Windows-safe key loading + Windows-aware tests (Grok) | in progress |
| `m1/pr6b-evaluator` | PR-6b: evaluator, episodes, discovery, auto-review, scenario fees | pushed |
| `m1/integration` | Everything merged and reconciled (report ported to the PR-1 fee models) | pushed |
| `m1/livefix` | Profile and fix discovery slowness and backpressure (Codex) | in progress |
| `m1/deploy` | launchd/systemd templates, install script, clock check (Codex) | in progress |

## Learned since plan v1.1

1. **The conservative bound for Kalshi fees needs a per-fill margin.** The documented algorithm (`docs.kalshi.com/getting_started/fee_rounding`) works in three steps:
   - ceil the model fee to 6 dp;
   - floor `revenue − fee` to the member precision;
   - carry the difference in a per-order accumulator that is rebated in precision-sized steps.

   The naive bound `ceil(Σ exact, precision)` understated the true fee in about 0.5% of random orders: by up to $0.0001 for direct members, and by up to $0.025 for non-direct members with fractional counts. → **PR-1:** use the algorithm, treat each walked level as at least one fill, and add a one-step margin labelled "conservative".
2. **Kalshi batch books trap.** `GET /markets/orderbooks` takes repeated `tickers=` params. A comma-joined value returns HTTP 200 with one empty book whose ticker is the literal `"A,B"`. → **PR-3:** send repeated params and assert that the returned tickers equal the requested ones. The endpoint needs no auth and costs 10 tokens. _(in plan v1.1)_
3. **Polymarket US fields, verified live.**
   - `gameStartTime` is on market objects; `startTime` exists only at event level.
   - `sportsMarketType` (a string) and `sportsMarketTypeV2` (`SPORTS_MARKET_TYPE_MONEYLINE`) are both present.
   - `/v2/leagues` is paginated, and `nba` is not on page 1.

   _(in plan v1.1)_
4. **The Polymarket US order fee is a cap, not a single rounded sum.** Each fill is rounded half-even, and the cumulative charge is capped at `round_half_even(Σ exact)`, so the cap is a safe upper bound for assessments. _(in plan v1.1)_
5. **Scripts cannot fetch the Kalshi fee schedule PDF** (Vercel bot check, 429). It was read in a browser: taker 0.07, maker 0.0175, effective 2026-07-07. Recheck the effective date periodically.
6. **Team tables cover 100% of both venues' game listings in a one-week snapshot** (NFL, NBA and NHL complete; NCAA tables built from the same snapshot, so that 100% is not a held-out measure). Kalshi and Polymarket codes differ (`JAC`/`jax`, `GSW`/`gs`, `VGK`/`veg`). The resolver raises on ambiguous labels (`New York G/J`, bare `Los Angeles`, `Wildcats`) instead of guessing. New NCAA schools will show up as unresolved and need adding.

7. **First live run (`pmarb scan --once`, 2026-11-12).**
   - Discovery found 1,186 Kalshi and 959 Polymarket US markets and auto-approved 570 groups. Concurrent fetch skew was 153–211 ms.
   - The run took 15.6 min.
   - The discovery burst (~1,000 raw envelopes) tripped `recording_backpressure`, which invalidated every book (ages ~141 s), so all 24 assessments were rejected.
   - Pregame pairs were `not_profitable` anyway.
   - Fix in progress on `m1/livefix`.
8. **Polymarket US `comboEnabled` means "usable as a combo leg".** Excluding it found 0 live markets (D29, fixed).
9. **Scenario fees (D30) are needed while nobody has accounts.** Without them every assessment is `fee_unknown`, so no episode can open.
10. **The `RetriesExhaustedError` book-poll failures (`m1/retry-fix`).** A 12-minute collector reproduction in the same afternoon window showed transient network trouble hitting **both venues at once**: Kalshi gave 5 consecutive `ConnectTimeout`s, Polymarket US gave `ssl.SSLError` (`SSLV3_ALERT_BAD_RECORD_MAC`), and there were multi-second stalls on both. The event loop and the writers were not blocked. The retry path turned each blip into a long outage:
    - Retries ran up to about 30 s per poll (27.4 s observed) against a 2 s freshness window, and `gather` held the healthy venue's books until the failing venue finished.
    - A raw `ssl.SSLError` was not retried at all.
    - One failed Polymarket US market failed the whole venue and left the other requests retrying in the background, using the next cycle's book tokens.
    - The log printed only the exception class.

    Fixes:
    - A per-fetch budget of 0.75 × the freshness window.
    - A shared per-`(venue, purpose)` backoff that honours `Retry-After` across cycles.
    - Partial results.
    - Books accepted per venue as soon as that venue's fetch returns.
    - Fixed-rate cycles.
    - Errors that carry cause, attempts, elapsed time, and endpoint, and health counters by cause.

    The old "sleep the cadence after the work" loop also gave a 2.8 s period against a 2 s window: 1,413 `books_not_fresh` in 12 min, down to 183 after the fix. Timeouts were not the problem: a 100-ticker Kalshi batch took 247 ms median, 560 ms max. The httpx pool limits were not the problem either.
11. **Polymarket US public books are served from Cloudflare's cache.** The responses carry `cache-control: public, max-age=30`. In a 5-minute probe, 1,190 of 1,200 book responses were `cf-cache-status: HIT`, with `Age` up to 29 s (median 15 s). Book age measured from local receipt therefore understates true age by up to 30 s. **Undecided:** record `Age`/`cf-cache-status` and treat them in freshness, mark such books cached, or rely on the M2 stream.
12. **Evaluation runs on the event loop**: about 0.25 s per call for 4 groups, plus a re-evaluation on every 250 ms freshness tick. Health `loop_lag_max_s` reaches about 1.2 s, and that lag is what leaves the remaining `books_not_fresh`.
13. **`pmarb doctor` rejected every fresh database.** Its schema check was pinned to version 1, but migration 0002 (game state) makes a fresh database version 2. The check now derives the expected version from the packaged migrations (fixed on `m1/windows`).

## Engineering conventions learned the hard way

- **Never commit in a checkout where an agent is still editing.** The pre-commit hook stashes unstaged changes while it runs, which briefly reverts the agent's in-progress files. Wait for the agent, or commit from another worktree.
- **Agent worktrees start from `main`**, which lags the dev branch. Workers must first fast-forward to the slice branch, or write only new files and be cherry-picked.
- **Run `gitleaks` on any commit made outside the hooks** (for example in worktrees without the config) before pushing. CI also scans the full history.
- **Build fake secrets in tests at runtime** (PEM blocks, key-like strings) so the secret scanners can stay strict.
- **Tests that use real data** read from `PMARB_DATA_DIR` (default `./data`) and are marked `localdata`. They never point at temporary or session paths.
- **Public repo.** No live market values in docs. Real data stays only in the gitignored `data/`, and private analyses go in `data/research/`.

## Next steps (start of next session)

1. **`RetriesExhaustedError` is fixed on `m1/integration`** (learned item 10). On the next soak:
   - read `adapter_error` events: `cause`, `history`, `attempts`, `elapsed_s`, `endpoint`;
   - read health: `poll_failures_by_cause`, `http`, `loop_lag_max_s`.

   Follow-ups: decide on the Polymarket US CDN cache (item 11) and take evaluation off the event loop (item 12).
2. **Windows support is on `m1/integration`.** It adds `tzdata`, Windows signal handling and `pmarb stop`, `icacls` checks, a skip of directory fsync on Windows, a `w32tm` clock check, `deploy/windows/` (Task Scheduler), `docs/RUN_ON_WINDOWS.md`, and a `windows-latest` CI job. Next: confirm that CI job is green, then do a first real-PC run by following the runbook.
3. **The evening soak is running** (restarted 20:04). Next: stop it with `pmarb stop` and run `pmarb report` on it, with the output under `data/reports/`. Then try `pmarb replay verify` on the same raw log and database (#22). Long term, it runs on Rishi's Windows desktop via `docs/RUN_ON_WINDOWS.md` once Windows CI is green. Start long runs in their own session (`start_new_session` or `deploy/` templates), never as a child of an agent's shell.
4. **Open the integration PR** (`m1/integration` → `main`) once Rishi has reviewed the stacked PRs, or let him merge the stack in order.
5. **M2:** wire the streams into the collector. Change `depth_unconfirmed` to valid + non-atomic. This needs API keys.
6. **Users:** create venue accounts and read keys. Rishi turns on secret scanning and protects `main`.
7. **Codex is off for the rest of the week** (user request). Use Grok and Claude subagents only.

## Next steps (historical, before 2026-11-12)


2. Merge PR-2a and PR-2b into `m1/pr2-foundations` (on PR-0), add `storage/db.py`, review, and push.
3. PR-1 (economics + fees), using the fee-rounding module and finding 1.
4. PR-3 and PR-4 in parallel, using the synthetic fixtures. PR-5a/5b using the team tables.
5. Users: open Kalshi and Polymarket US accounts, create read keys, and record the Kalshi member type and the Polymarket US Θ tier.
