"""Pure candidate proposals for cross-venue game-winner markets.

``normalize.normalize_market(market, diagnostics=issues)`` returns canonical
participants, YES polarity, and a New York event date with its source. Rejected
metadata returns ``None`` and appends a diagnostic to the caller-owned list.

``candidates.generate_candidates(markets, diagnostics=issues)`` joins exact
league, participant-set, and date keys. Each proposal pairs the Polymarket US
long team with only that team's Kalshi YES contract. Similarity ranks proposals;
it never establishes equivalence. Groups remain candidates for human review,
including manual date checks and the proposed settlement exception table.

Team aliases live in ``matching/data/<league>.csv``. Neither normalization nor
candidate generation makes network requests, writes to a database, or approves
a group.
"""
