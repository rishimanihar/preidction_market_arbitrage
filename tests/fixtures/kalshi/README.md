# Kalshi fixtures

All files in this directory are synthetic test data. They contain no live
market recordings or account data.

`orderbooks_batch.json` follows the verified batch response shape: an
`orderbooks` array of objects with `ticker` and `orderbook_fp`, whose
`yes_dollars` and `no_dollars` fields contain bid levels.
