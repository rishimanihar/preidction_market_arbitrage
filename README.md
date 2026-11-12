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

The build plan is `docs/IMPLEMENTATION_PLAN.md` (strategy rationale: `plan.md`); current status is `docs/PROGRESS.md`.

See `CONTRIBUTING.md` for the git workflow and `SECURITY.md` for credential and data rules. Research notes are in `docs/research/`.

## Review app

Set `PMARB_DATA_DIR` to the collector's data directory (default `./data`, gitignored).
The review app reads `pmarb.sqlite` in that directory. Run:

```bash
pmarb doctor
pmarb review
pmarb review --port 8502
```

The app listens on `127.0.0.1`; `PMARB_REVIEW_PORT` sets its default port (8501).
Its pages show candidates, rules review, live assessments, episodes, and collector
health. Times display in America/Chicago, including CST/CDT labels. Indicative
results are not tradable evidence. Missing collector fields remain unavailable.

SQLite is opened read-only. Review actions write atomic decision files to
`<data_dir>/spool/reviews/` for the collector to apply. Approvals require explicit
exception acknowledgment, and the collector must reject decisions whose displayed
rules hashes have changed. The app does not apply its own decisions or initialize
the collector's database. Kalshi terms links are displayed without fetching PDFs.

`pmarb doctor` checks configuration, data directory policy and writability, disk
space, configured key-file permissions/location, database schema, and Streamlit.
`pmarb scan` and `pmarb replay` remain placeholders until the runtime arrives.
