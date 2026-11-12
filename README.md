# pmarb

Local research toolkit for comparing Kalshi and Polymarket US binary contracts. Phase 1 is a pure calculation core: domain models, order-book walking, fee models, and a two-direction arbitrage assessment. It does not open network connections or submit orders.

## Development

Python 3.12:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e '.[dev]' pre-commit
.venv/bin/pre-commit install
.venv/bin/pytest -q
.venv/bin/ruff check src tests
```

See `CONTRIBUTING.md` for the git workflow and `SECURITY.md` for credential and data rules. Research notes are in `docs/research/`.

`pmarb scan`, `pmarb review`, and `pmarb replay` are reserved for later phases.
