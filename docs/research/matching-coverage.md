# PR-5c: matching policy v2 and coverage

Policy v2 matches **343/343 research games (100%)**, up from 110/343 under v1.
The broader captured universe produces **645 outcome groups**, up from 170 at
PR-5b and 171 after the initial PR-5c spelling/parser fixes. Every group remains
a candidate requiring human review; matching does not approve settlement equivalence.

Only aggregate results and team labels/codes are recorded here. Private market
payloads, IDs, timestamps, rules, and per-pair source evidence remain in the local
research directory. The full before/v1/v2 ledgers are `audit_pr5c_before.json`,
`audit_pr5c_after.json`, and `audit_pr5c_v2.json` there.

## Policy v2

Structured team identities take precedence over text. Slug codes resolve in the
Polymarket code namespace; Kalshi ticker codes resolve in the Kalshi namespace.
A code is not resolved through the other venue's aliases. Polymarket sides map
bijectively to slug participants using nested team abbreviations, direct team
references, or identifier/teamId references whose canonical team is known. Opaque
IDs may be linked through a supplied team registry. A shared instrument identifier
alone never establishes either team's identity, and side ordering never determines YES.

Labels corroborate an established identity. A uniquely resolved conflicting label
or an ambiguous label whose candidate set excludes the established team rejects
the market. A compatible ambiguous nickname or unknown label cannot replace a
structured identity; it is ignored with explicit evidence. Without structured
side identity, label fallback must resolve uniquely and consistently; ambiguity
rejects the market. Conflicting structured references and invalid explicit team
codes also reject the market. A women's marker cannot corroborate a men's league.

The date written in Kalshi's rules/title and the Polymarket slug date are the
authoritative New York calendar dates. Both must exist and be exactly equal;
there is no tolerance. Conflicting explicit Kalshi dates reject the market.
Occurrence/start timestamps are hints. A differing hint date retains the candidate
with `date_hint_conflict` and both values in evidence. Neither an occurrence hint
nor expected expiration supplies a missing authoritative date, and no three-hour
subtraction is used to create an authoritative date.

Every candidate includes `policy_version="v2"`, the source identifiers and their
canonical mappings, authoritative date sources, corroboration decisions, and any
hint-conflict evidence. Both members remain YES contracts for the same single team.
Groups remain `CANDIDATE`, with no reviewer, decision timestamp, or approved rules
hashes. Doubleheader/manual-date checks and settlement-exception review remain active.

## Coverage

The research reference contains one row per game, including both teams' contracts.
A matcher group represents one Polymarket YES outcome. Some captured games have
multiple outcome groups, and the broader universe includes games outside the
reference. Coverage therefore counts actual generated member pairs with the
same Kalshi event and Polymarket market as a reference row, once per game.

| League | PR-5b groups | Initial PR-5c groups | V2 groups | Reference games | V1 matched | V2 matched | V2 coverage |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| NFL | 14 | 14 | 14 | 14 | 14 | 14 | 100% |
| NBA | 30 | 30 | 37 | 37 | 30 | 37 | 100% |
| NHL | 25 | 25 | 38 | 38 | 25 | 38 | 100% |
| NCAAF | 35 | 35 | 131 | 104 | 25 | 104 | 100% |
| NCAAMB | 25 | 25 | 205 | 142 | 13 | 142 | 100% |
| NCAAWB | 41 | 42 | 220 | 8 | 3 | 8 | 100% |
| Total | 170 | 171 | 645 | 343 | 110 | 343 | 100% |

All 343 reference rows have corroborating full team/school labels, matching
Kalshi rules and Polymarket slug dates, a winner market, and a recorded Kalshi
contract for the Polymarket long team. None is excluded from the denominator.

## Misses by reason

Each primary miss is counted once, inspecting Kalshi normalization before
Polymarket normalization. The initial PR-5c parser fix changed one NCAAMB primary
reason from a matchup parsing failure to an ambiguous nickname, without adding
a reference match. The new NCAAWB v1 group was outside the reference set.

| Primary reason | NFL v1 | NBA v1 | NHL v1 | NCAAF v1 | NCAAMB v1 | NCAAWB v1 | V1 total | V2 remaining |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Ambiguous side nickname | 0 | 0 | 0 | 71 | 105 | 4 | 180 | 0 |
| Occurrence NY date disagrees with rules | 0 | 7 | 13 | 8 | 24 | 1 | 53 | 0 |
| Missing alias or school/code mapping | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Missing matching Kalshi YES contract | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Non-winner or opposite YES outcome | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Doubleheader | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Total | 0 | 7 | 13 | 79 | 129 | 5 | 233 | 0 |

Counting overlaps, 205 reference rows had ambiguous Polymarket text; 25 of them
also had date-hint disagreements. V2 resolves these cases using the explicitly
authorized precedence rules. Hint disagreements remain visible for review.

| League | Generated groups with date_hint_conflict | Reference games with date_hint_conflict |
| --- | ---: | ---: |
| NFL | 0 | 0 |
| NBA | 7 | 7 |
| NHL | 13 | 13 |
| NCAAF | 8 | 8 |
| NCAAMB | 31 | 24 |
| NCAAWB | 10 | 1 |
| Total | 69 | 53 |

Outside the reference set, 14 captured Kalshi game keys have no matching
Polymarket league/team/date key (13 NHL, 1 NCAAWB). There are also 414 supported
Polymarket keys without a Kalshi counterpart (25 NBA, 1 NCAAF, 214 NCAAMB,
174 NCAAWB). These absent counterpart listings remain unmatched. Every shared
key has a matching YES contract. All 1,318 captured Kalshi markets and all 1,059
supported-league Polymarket markets normalize; 557 other Polymarket entries
remain unsupported or lack a supported league.

## Alias and parser decisions

No new school/code mapping was required: the observed team tables already covered
the snapshot. Existing rows retain their `source=observed` provenance. The initial
fix normalized leading `St.` to `Saint`, retained trailing `St.` as `State`, and
normalized `Univ.` to `University`. Ampersand, accent, case, whitespace, and
punctuation rules have focused tests.

The matchup parser preserves `University at Albany` as a complete team name and
accepts `at` wording only when a single supported split exists. Redundant explicit
NCAAMB `MSU`/`KSU` alias assignments were removed without changing standalone
ambiguity. V2 additionally uses venue-scoped code lookups so cross-venue code/alias
collisions cannot silently select another school.

## Verification and reproduction

```sh
python -m pytest -q -o pythonpath=src
PMARB_DATA_DIR=/path/to/private/data python -m pytest -q -s -o pythonpath=src -m localdata
ruff check src tests
ruff format --check src tests
```

The local tests report both total groups and reference coverage and validate each
generated candidate's league, team set, exact authoritative date, and same-YES
alignment. Unit tests cover structured and label fallback branches, contradictory
identity evidence, strict code namespaces, missing/conflicting authoritative dates,
hint conflicts, policy/evidence propagation, and the human review gate. Alias
integrity checks enforce unique venue codes and explicit alias assignments while
preserving ambiguous standalone label rejection.
