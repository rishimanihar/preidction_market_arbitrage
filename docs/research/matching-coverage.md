# PR-5c coverage audit

The strict matcher generates 171 outcome groups after this change, up from 170.
It covers 110 of the 343 research games before and after (32.1%). The requested
90% research coverage target is **not met**. Shared nickname ambiguity and
contradictory date evidence remain rejected.

Only aggregate results and team labels/codes are recorded here. The exhaustive
343-row audits and their source evidence stay in the private research directory
as `audit_pr5c_before.json` and `audit_pr5c_after.json`.

## Comparable denominators

The reference file contains one row per game, including both teams' contracts.
A matcher group represents one Polymarket YES outcome. Some games produce two
groups, and the captured matcher universe includes games outside the reference.
Therefore total groups divided by reference rows is not a coverage percentage.
Reference coverage counts an actual generated member pair with the same Kalshi
event and Polymarket market as the reference row, once per game.

| League | Groups before | Groups after | Research games | Research matched before | Research matched after | Research coverage after |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| NFL | 14 | 14 | 14 | 14 | 14 | 100.0% |
| NBA | 30 | 30 | 37 | 30 | 30 | 81.1% |
| NHL | 25 | 25 | 38 | 25 | 25 | 65.8% |
| NCAAF | 35 | 35 | 104 | 25 | 25 | 24.0% |
| NCAAMB | 25 | 25 | 142 | 13 | 13 | 9.2% |
| NCAAWB | 41 | 42 | 8 | 3 | 3 | 37.5% |
| Total | 170 | 171 | 343 | 110 | 110 | 32.1% |

All 343 reference rows have corroborating full team/school labels, matching
Kalshi rules and Polymarket slug dates, a winner market, and a recorded Kalshi
contract for the Polymarket long team. These are plausible equivalent outcomes,
not approved settlement-equivalence groups: conflicting occurrence dates and
ambiguous labels still prevent strict normalization. None is excluded from the
coverage denominator to improve the percentage.

## Primary rejection reasons

Each missed reference row is counted once, inspecting Kalshi normalization
before Polymarket normalization. This priority makes the reason counts additive;
it does not imply that a row has only one problem.

| Primary reason after change | NFL | NBA | NHL | NCAAF | NCAAMB | NCAAWB | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Ambiguous Polymarket side nickname | 0 | 0 | 0 | 71 | 105 | 4 | 180 |
| Kalshi occurrence NY date conflicts with rules | 0 | 7 | 13 | 8 | 24 | 1 | 53 |
| Missing alias or school/code mapping | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Missing matching Kalshi YES contract | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Non-winner market or opposite YES outcome | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Doubleheader | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Total remaining misses | 0 | 7 | 13 | 79 | 129 | 5 | 233 |

Before the fix, NCAAMB had 104 primary nickname rejections and one matchup-parser
rejection. Parsing that matchup now succeeds, exposing its ambiguous Polymarket
nickname; its reference game still does not match. The one new NCAAWB group is
outside the reference set.

Counting overlapping blockers, 205 reference rows have ambiguous Polymarket
labels (75 NCAAF, 125 NCAAMB, 5 NCAAWB). Of the 53 date-conflict rows, 25 also
have nickname ambiguity (4 NCAAF, 20 NCAAMB, 1 NCAAWB). The union accounts for
all 233 remaining misses. Even resolving every date conflict would leave at
most 138/343 games matchable while rejecting those ambiguous labels.

## Decisions and evidence

- Bare `Tigers`, `Bulldogs`, and similar shared nicknames remain ambiguous.
  Nested full school names and codes do not override an ambiguous supplied label.
  Adding a nickname alias to one school would silently misassign other schools.
- All captured school names and venue codes already have table entries. No new
  team alias or code mapping was justified. Three research abbreviation checks
  (`CHAR`, `MSU`, `CSU`) encounter cross-team code/alias collisions; the full
  school labels corroborate identity, while ambiguous labels remain rejected.
- In every date-conflict row, subtracting three hours from expected expiration
  produces the rules/Polymarket date. That pattern is diagnostic evidence, not
  authorization to override an explicit occurrence timestamp. The occurrence
  versus rules conflict guard and exact equal-NY-date join remain intact.
- The matchup parser mistook the internal `at` in `University at Albany` for a
  matchup separator. It now prioritizes `vs`/`versus`, recognizes a complete
  single-team winner title, and accepts `at` wording only when exactly one split
  resolves both full team labels. Unknown, conflicting, or multiply interpretable
  labels remain rejected. Winner-title polarity is still independently checked.
- Leading `St.` normalizes to `Saint`; trailing `St.` continues to mean `State`.
  `Univ.` normalizes to `University`. Existing ampersand, accent, punctuation,
  whitespace, and case normalization has focused regression tests.
- Redundant NCAAMB `MSU`/`KSU` explicit aliases were removed to eliminate repeated
  alias assignments. Venue-code collisions still make these labels ambiguous;
  no team ownership was guessed. All affected rows retain `source=observed`.

## Reproduction

Use the repository virtual environment and point `PMARB_DATA_DIR` at the private
data root. No network calls or private fixture publication are needed.

```sh
python -m pytest -q -o pythonpath=src
PMARB_DATA_DIR=/path/to/private/data python -m pytest -q -s -o pythonpath=src -m localdata
ruff check src tests
ruff format --check src tests
```

`test_localdata_matching_candidate_coverage` reports total groups;
`test_localdata_reference_pair_coverage` reports actual reference-game coverage
and reasons, and verifies all generated candidates' team, league, date, and YES
alignment. Integrity tests enforce unique per-venue codes and unique explicit
alias assignments, and check that shared labels still reject ambiguity.

Further progress toward 90% requires a separately reviewed policy for structured
school-plus-nickname evidence and trustworthy date-source provenance. This
change does not introduce either policy.
