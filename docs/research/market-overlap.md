# Kalshi × Polymarket US market overlap (public summary)

> The full study, which contains live prices and book sizes, is kept privately in the gitignored `data/research/market-overlap.md`. This summary contains no live market data.

**Method.** Pulled all open markets on both venues resolving within ~7 days (public endpoints), joined on league, both participants, and event start time, then read both venues' rules for the top candidates.

**Findings**
- The overlap is **sports game-winner markets**: a single week produced several hundred joinable pairs across NFL, NBA, NHL, college football, and men's college basketball. Elections/economics barely overlap on this pair of venues.
- **Rules for a played game mostly match** (overtime included for NFL/NBA/NHL; NFL tie pays $0.50 on both). **Cancellation/postponement does not** produce a guaranteed $1 pair: each venue settles at its own "fair price", and the windows differ ("48 hours" vs "two days").
- **Pregame prices summed to about $1.00 or slightly more** before fees, i.e. no pregame edge observed; fees make it negative. Larger displayed gaps appeared only on live (in-game) books fetched non-simultaneously — not evidence of a real opportunity.
- **Matching traps:** Polymarket US `endDate` is ~2 weeks after the game (cannot be used as the event date); Kalshi expected-expiration timestamps sat a constant +3 h after Polymarket start times; the same line (e.g. "21.5") can mean "wins by" on one venue and "covers +" on the other; similar-looking weather and crypto markets were different contracts (station/source, touch vs averaging window).
- **Polarity:** Polymarket game markets are one YES instrument (the `marketSides[].long` team); the opponent is the short. Kalshi lists one binary per team.

**Implications** — matching keys on league, normalized participants, date from rules/question, market type, line, and YES polarity; title similarity only ranks candidates; human approval bound to rules hashes; exception payouts (tie, postponement, cancellation) must be reviewed per pair; books fetched non-simultaneously must be flagged.
