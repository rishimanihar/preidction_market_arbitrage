# Progress log

This is the build's living status: what's done, what we've learned that isn't in the plan yet, and what comes next. **Update it at every milestone step.** Read it alongside `docs/IMPLEMENTATION_PLAN.md` (what we're building) and `docs/research/` (why).

_Last updated: 2026-11-11 22:35 CST_

## Status

| Item | State | Where |
|---|---|---|
| Research (5 reports, fact-checked) | done | `docs/research/` |
| Implementation plan v1.1 (adversarially reviewed) | done | `docs/IMPLEMENTATION_PLAN.md` |
| Security guardrails (gitignore, gitleaks pre-commit + CI, policy) | done | `SECURITY.md`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml` |
| Phase 1 core (Decimal models, ladder, fees, 2-venue calculator; 39 tests) | done | `src/pmarb/` |
| **PR #1** foundation → `main` | open, awaiting Rishi | GitHub PR #1 |
| PR-0 contracts (models, protocols, DDL; 64 tests) | done, PR open | branch `m1/pr0-contracts` |
| PR-2a config / secrets / redacting logs / observation HTTP client | built, in review | agent worktree → PR-2 |
| PR-2b crash-safe raw log | built, in review | agent worktree → PR-2 |
| Kalshi fee-rounding algorithm (feeds PR-1) | built, in review | agent worktree |
| Synthetic API fixtures, both venues (feeds PR-3/4) | built, in review | agent worktree |
| PR-4 Polymarket US REST adapter | built; 175 total tests pass; ready for orchestrator review | branch `m1/pr4-polymarket-us` |
| Team alias tables + resolver, 6 leagues (feeds PR-5b) | built, in review | agent worktree |
| PR-1, PR-3, PR-5 … PR-8 | not started | plan §10 |
| Venue accounts + API keys (users) | not started; gates M2 | plan §10 "Your task" |

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
7. **PR-4 keeps the adapter contract and raw JSON together.** `get_books()` returns `list[Book]`, with each complete venue response in `Book.raw_native` for the collector's raw log. Discovery honors the supplied inclusive horizon; the caller supplies the standard 12-hour lookback. The fee-model object remains a PR-1 integration dependency on this branch.

## Engineering conventions learned the hard way

- **Never commit in a checkout where an agent is still editing.** The pre-commit hook stashes unstaged changes while it runs, which briefly reverts the agent's in-progress files. Wait for the agent, or commit from another worktree.
- **Agent worktrees start from `main`**, which lags the dev branch. Workers must first fast-forward to the slice branch, or write only new files and be cherry-picked.
- **Run `gitleaks` on any commit made outside the hooks** (for example in worktrees without the config) before pushing. CI also scans the full history.
- **Build fake secrets in tests at runtime** (PEM blocks, key-like strings) so the secret scanners can stay strict.
- **Tests that use real data** read from `PMARB_DATA_DIR` (default `./data`) and are marked `localdata`. They never point at temporary or session paths.
- **Public repo.** No live market values in docs. Real data stays only in the gitignored `data/`, and private analyses go in `data/research/`.

## Next steps

2. Merge PR-2a and PR-2b into `m1/pr2-foundations` (on PR-0), add `storage/db.py`, review, and push.
3. PR-1 (economics + fees), using the fee-rounding module and finding 1.
4. PR-3 and PR-4 in parallel, using the synthetic fixtures. PR-5a/5b using the team tables.
5. Users: open Kalshi and Polymarket US accounts, create read keys, and record the Kalshi member type and the Polymarket US Θ tier.
