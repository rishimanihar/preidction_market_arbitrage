# Progress log

This is the build's living status: what's done, what we've learned that isn't in the plan yet, and what comes next. **Update it at every milestone step.** Read it alongside `docs/IMPLEMENTATION_PLAN.md` (what we're building) and `docs/research/` (why).

_Last updated: 2026-11-12 13:30 CST_

## Status (2026-11-12 13:30 CST)

**All of M1 is built and integrated** on `m1/integration` (548 tests). The first live end-to-end run has happened. Fixes it surfaced are in progress on `m1/livefix`.

| PR | Item | State |
|---|---|---|
| #1–#10, #12 | Foundation, contracts, foundations, adapters, matching v1/v2, review store, poller | open |
| #7 | Economics + fees | open |
| #8 | Review app + CLI | open |
| #13 | Game-state recorder (Kalshi `/live_data`) | open |
| #14 / #15 | M2: Polymarket US / Kalshi WebSocket streams | open |
| #11 | M2: request signing | open |
| #16 | `pmarb report` (M1 analysis) | open |
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

## Engineering conventions learned the hard way

- **Never commit in a checkout where an agent is still editing.** The pre-commit hook stashes unstaged changes while it runs, which briefly reverts the agent's in-progress files. Wait for the agent, or commit from another worktree.
- **Agent worktrees start from `main`**, which lags the dev branch. Workers must first fast-forward to the slice branch, or write only new files and be cherry-picked.
- **Run `gitleaks` on any commit made outside the hooks** (for example in worktrees without the config) before pushing. CI also scans the full history.
- **Build fake secrets in tests at runtime** (PEM blocks, key-like strings) so the secret scanners can stay strict.
- **Tests that use real data** read from `PMARB_DATA_DIR` (default `./data`) and are marked `localdata`. They never point at temporary or session paths.
- **Public repo.** No live market values in docs. Real data stays only in the gitignored `data/`, and private analyses go in `data/research/`.

## Next steps (start of next session)

1. **PR-6b** (base: merge `m1/pr6a-poller` + `m1/pr1-economics` + `m1/pr5a-review` + `m1/pr5c-coverage`):
   - evaluator hook calling `economics.assess_group` for every polled group;
   - writes assessments through `DbWriterTask`;
   - episode state machine (indicative vs qualified kept separate);
   - review intake loop (`review.spool.apply_pending`) plus `review_mode="auto"` (D27);
   - the DB file is `data/pmarb.sqlite` (the app expects this);
   - replace the adapters' `fee_model()` stubs with `pmarb.fees` models (Kalshi multiplier from series; precision and Θ from config, unset → unknown).
2. **Integration branch** `m1/integration`: merge every M1 branch, run the full suite, implement `pmarb scan`, then do a first live run against the public APIs (discover → auto-approve → poll a live game).
3. **PR-8 soak:** 24 h unattended across an evening slate; report uptime, gaps, 429s, data volume, and indicative gap statistics (size vs skew).
4. **M2 prep:** a Kalshi WS client using PR #11 signing (needs keys); a game-state recorder from Kalshi `/live_data` (D28).
5. **Users:** venue accounts + read keys; Rishi turns on secret scanning and protects `main`.

## Next steps (historical, before 2026-11-12)


2. Merge PR-2a and PR-2b into `m1/pr2-foundations` (on PR-0), add `storage/db.py`, review, and push.
3. PR-1 (economics + fees), using the fee-rounding module and finding 1.
4. PR-3 and PR-4 in parallel, using the synthetic fixtures. PR-5a/5b using the team tables.
5. Users: open Kalshi and Polymarket US accounts, create read keys, and record the Kalshi member type and the Polymarket US Θ tier.
