"""Pure candidate proposals for cross-venue game-winner markets.

``normalize.normalize_market(market, diagnostics=issues)`` returns canonical
participants, YES polarity, and a New York event date with its source. Rejected
metadata returns ``None`` and appends a diagnostic to the caller-owned list.
Policy v2 prefers venue-scoped structured team codes; labels only corroborate
those identities. Without a structured side identity, labels must be unique.
Kalshi rules/title and Polymarket slug dates are authoritative; timestamp hints
cannot substitute for them and disagreements are retained in flags and evidence.

``candidates.generate_candidates(markets, diagnostics=issues)`` joins exact
league, participant-set, and date keys. Each proposal pairs the Polymarket US
long team with only that team's Kalshi YES contract. Similarity ranks proposals;
it never establishes equivalence. Groups remain candidates for human review,
including manual date checks and the proposed settlement exception table.
Every candidate carries ``policy_version="v2"``, evidence, and any
``date_hint_conflict`` flag. Matching never approves a group; approval is
the collector's job (``review_mode="auto"`` during testing per plan D27, or a
human decision via the review spool).

Rules hashing and revocation live in ``matching.rules``. Team aliases live in ``matching/data/<league>.csv``. Neither normalization nor
candidate generation makes network requests, writes to a database, or approves
a group.
"""
