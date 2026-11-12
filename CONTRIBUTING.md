# Contributing

## Setup
```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]' pre-commit
.venv/bin/pre-commit install
```

## Git workflow
- `main` is protected: no direct commits. Each contributor works on their own branch (e.g. `avyukth-dev`) and merges via pull request.
- Small, focused commits with imperative messages (`Add Kalshi book normalizer`). Push often so teammates can follow.
- Before every commit: `pre-commit` (gitleaks, ruff) runs automatically. `ruff check src tests` and `pytest -q` must pass.
- Never `--no-verify`, never force-push `main`, never rewrite shared history without coordinating.

## Engineering rules
- `Decimal` for all prices, sizes, fees, and money. No floats in calculations.
- Every venue-specific behavior (price convention, fee rounding, book semantics) gets a fixture-based test citing its doc source.
- Unknown inputs (fees, settlement timing, rules) must surface as "unknown/unqualified", never default to zero.
- No network calls in unit tests; use recorded fixtures (synthetic or redacted, never committed raw dumps).
- See `SECURITY.md` for credential handling.
