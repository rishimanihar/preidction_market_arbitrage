# Test fixtures

All values in these fixtures are synthetic: team names, tickers, slugs, prices, sizes, cursors, and timestamps are invented, and every date is in 2030. The field names and value formats mirror the documented and observed wire formats in `docs/research/` (Kalshi uses fixed-point dollar strings and count strings, Polymarket US uses `px`/`qty` string objects and nanosecond `transactTime`), so parsers can be tested against realistic shapes without any live data. Never add real captured responses to this directory; if a format needs checking against the live API, capture it outside the repo and substitute synthetic values before committing.

## Caveats

- `kalshi/orderbooks_batch.json` is a **best guess**. The research docs confirm `GET /markets/orderbooks?tickers=…` exists (1–100 tickers) but do not document its response body. The `{"orderbooks": [{"ticker", "orderbook_fp"}]}` wrapper must be verified in PR-3.
- `kalshi/orderbook_empty_side.json` shows an empty REST side as `[]`. The schema marks both arrays as required, but an empty REST array has not been observed live; the WebSocket schema omits the key instead. Verify in PR-3.
- League and event pagination responses are synthesized in `tests/test_polyus_rest.py`. The official [league catalog](https://docs.polymarket.us/api-reference/sports/get-all-leagues.md) and [league events](https://docs.polymarket.us/api-reference/sports/get-events-by-league-slug.md) docs confirm the `leagues` and `events` wrappers and offset pagination. The docs do not enumerate college league slugs; configured candidates are checked against the catalog at runtime.
- `kalshi/markets_page_*.json` cover three games with both sides of each game present: page 1 holds game A (both sides) plus one side of game B, and page 2 holds the other side of game B plus game C (both sides). Team names are fictional rather than real league teams.
- The Polymarket `outcomes` and `outcomePrices` strings in `market_by_slug.json` are intentionally inconsistent with the book. Code must ignore them for pricing.
- The supplied market fixtures predate the documented `gameStartTime` and `sportsMarketTypeV2` fields. Parser tests add synthetic values for these fields and event context explicitly. Market-level `startTime` in the old fixture is not treated as event time.
