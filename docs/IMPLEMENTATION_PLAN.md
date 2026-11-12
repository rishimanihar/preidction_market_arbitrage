# Implementation plan

**Status:** v1, 2026-11-11. Supersedes the build sections of `plan.md`; `plan.md` remains the strategy and evidence rationale.
**Inputs:** `plan.md`, `docs/research/*` (fact-checked), decision log in §12.
**Workflow:** agents build on `avyukth-dev`, one pull request per slice (§10), Rishi reviews and merges into `main`.

---

## 1. Goal, scope, non-goals

**Goal.** Find out, with trustworthy evidence, whether equal-quantity cross-venue arbitrage between US-regulated prediction markets is real after fees, depth, latency and settlement risk — and only then consider trading it.

**The trade.** For one real-world outcome listed on two venues, buy YES on one venue and NO on the other, the same number of contracts on both legs. If both venues settle the same way, exactly one leg pays $1 per contract. Profit = `q × $1 − (both legs' costs + both fees + extra costs)`.

**What M1 delivers (no API keys needed).** A local tool that discovers matching sports markets on Kalshi and Polymarket US, lets a human approve pairs after a side-by-side rules review, polls both public order books, computes executable both-direction profit at a chosen size, records everything, and shows a ranked live table plus history. It must run 24 hours unattended and recover from disconnects and restarts.

**Non-goals until explicitly approved.**
- No order submission of any kind (enforced by test, §8).
- No global Polymarket (offshore; restricts US persons). No VPNs, no offshore venues.
- No sportsbooks (no $1 contracts, no trading API, they limit arbitrage accounts).
- No combos/parlays, scalar, or multi-outcome structures. Standard $1/$0 binaries only.

## 2. Facts that shape the design

Each fact below was verified in primary docs or live responses; the high-impact ones were re-checked by an independent fact-check pass. Sources are in `docs/research/`.

| # | Fact | Design consequence |
|---|---|---|
| F1 | The Kalshi × Polymarket US overlap is **sports game winners**: several hundred joinable pairs per week (NFL, NBA, NHL, NCAAF, NCAAMB). Elections and economics barely overlap. | v1 universe = game-winner markets. |
| F2 | **Pregame prices summed to ≈$1.00–1.015** before fees, so there is no pregame edge. Bigger gaps appeared only on live (in-game) books. | Real opportunities, if any, are **in-game and short-lived**, so speed and simultaneity matter. M1 polling proves correctness; M2 streaming is needed to measure in-game edges honestly. |
| F3 | Kalshi taker fee = `round_up(M × 0.07 × C × P × (1−P))` to $0.0001 (fee schedule PDF, effective 2026-07-07). Per-series multipliers apply: sports game series list maker 1 / taker 1, combos taker 2. | Fee model reads the series multiplier as data, not code. |
| F4 | Polymarket US taker fee = `Θ × C × p × (1−p)`. The order total is `round_half_even_to_cent(Σ exact per-fill fees)`. **Θ is per account**: 0.095 / 0.085 / 0.075 / 0.0695, and 0.0695 for ET days 1–7 after the first fill. The market field `feeCoefficient` is always 0.095 and is not your Θ. | Θ is user config. If unknown, the result is unqualified (scenario only). |
| F5 | Polymarket US has **one instrument per market**. Buying NO means shorting YES at the YES bid `p`, which consumes `q × (1−p)` of buying power. The YES side is `marketSides[].long == true`. | NO acquisition ladder = `1 − YES bid`, with the bid's size. Never use `outcomePrices`; it was observed mislabeled. |
| F6 | Kalshi REST books are **bids only** (YES bids and NO bids) as `[dollar_string, fp_size]`. The docs say "best to worst", but live data was ascending. Sizes are fractional (`*_fp`). | YES ask = `1 − NO bid`. **Always sort explicitly**; never trust the array order. Decimal everywhere. |
| F7 | Kalshi WS `orderbook_delta` carries `sid`/`seq`. On a gap, send `get_snapshot` or unsubscribe and resubscribe; a second subscribe does **not** resync. With `use_yes_price=true`, NO levels arrive already on the YES scale, so never complement them twice. The lifecycle channel is `market_lifecycle_v2`. | Gap handling is part of the M2 adapter contract and its tests. |
| F8 | Polymarket US WS market data sends **multi-level snapshots** (depth configurable). Each message is complete and there are no sequence numbers. The public REST book needs no auth. | M1 polls REST. M2 replaces the whole book on every WS message. |
| F9 | Kalshi public listing responses are cached (`max-age=15`, `x-kalshi-cache-hits`), and market top-of-book sizes disagreed with the order book. | Use only order-book endpoints for prices. Record cache headers; a cached body is never "fresh". |
| F10 | Polymarket US `endDate` is ≈2 weeks after the game. Kalshi `close_time` ≠ expected resolution. Kalshi expected expiration sat a constant **+3 h** after the Polymarket start time (it estimates the game's end). | Join on teams + date with a tolerance window. Never filter by `endDate` or `close_time`. |
| F11 | Cancellation or postponement settles at **each venue's own "fair price"** (or $0.50 on a tie), and the postponement windows differ (48 h vs "two days"). | A pair is "equivalent for a played game" plus an explicit **exception-risk** flag shown in the UI. Never silently treated as a guaranteed $1. |
| F12 | Polymarket US has weekly maintenance **Thursday 06:00–08:00 ET**: 503 errors, resting orders cancelled, books reopen empty. There is no retail sandbox. | Treat it as an expected gap; books stay invalid until a fresh snapshot. Everything is read-only against production. |
| F13 | Rate limits: Kalshi Basic read is 200 tokens/s at 10 tokens per request (≈20 req/s). Polymarket US is 20–25 req/s per IP (the docs disagree). | Start at ≤5 req/s per venue. Honour 429s with jittered backoff. |
| F14 | Third venues: **Gemini Titan** (CFTC DCM, public unauthenticated book with sizes) is the best next venue. Coinbase, Webull and IBKR-Kalshi route to Kalshi's book and are **not** separate venues. | Gemini Titan is the M4 adapter. |
| F15 | Kalshi Developer Agreement: store and use API data for your own trading, and do not publish it. One account and one key per person. | Data stays local in gitignored `data/`. One key per person. No Kalshi branding in the public repo. |

## 3. Architecture

### 3.1 Shape

```
                 ┌────────────── collector process (asyncio, single writer) ──────────────┐
 venue REST/WS → │ VenueAdapter ─► BookStore ─► Evaluator ─► EpisodeTracker ─► Recorder   │ → data/
                 │      │              ▲            ▲                             (SQLite  │
                 │      └─ metadata ─► Catalog ─► Groups (approved, hash-bound)    + raw) │
                 └────────────────────────────────────────────────────────────────────────┘
 review app (Streamlit, localhost, read-mostly) ── reads SQLite; writes only review decisions via a small API table
```

- One **collector** process owns all network I/O and is the only SQLite writer. Closing the review app never stops collection.
- The **review app** only reads SQLite. Approvals go into a `review_requests` table, and the collector applies them, so there is still a single writer.
- Neither process imports an order client.

### 3.2 Package layout (target after M1)

```
src/pmarb/
  models.py            # Level, Book, Market, Leg, EquivalenceGroup, Assessment, Episode (Decimal, Pydantic)
  ladder.py            # walk(), available_size()  (exists)
  normalize.py         # bids→acquisition ladders, explicit sort  (exists, generalized)
  economics.py         # N-venue calculator: assess(group, yes_leg, no_leg, qty, ...)  (replaces calculator.py)
  fees/
    base.py            # FeeModel protocol, FeeQuote(amount|None, provenance)
    kalshi.py          # 0.07 × M ceil-to-centicent, series multiplier table, member precision
    polymarket_us.py   # Θ per account, half-even once per order
  venues/
    base.py            # VenueAdapter protocol, RateLimiter, BackoffPolicy
    kalshi/{rest.py, ws.py, auth.py, parse.py}
    polymarket_us/{rest.py, ws.py, auth.py, parse.py}
    gemini/            # M4
  matching/
    teams.py           # league → canonical team ids + alias tables (data files under matching/data/)
    candidates.py      # deterministic candidate generator
    rules.py           # rules text capture, normalization, sha256 hashing, exception table
  collector/
    runtime.py         # task supervisor, schedules, freshness timer, shutdown
    books.py           # BookStore: per (venue, market) latest valid book + connection generation
    evaluator.py       # runs economics on approved groups, both directions, size views
    episodes.py        # episode state machine
  storage/
    db.py              # SQLite (WAL), migrations, single-writer queue
    rawlog.py          # append-only gzip JSONL, rotation, checkpoints
  app/                 # Streamlit pages (review, live, history, health)
  config.py            # typed settings (pydantic-settings), defaults in §11
  secrets.py           # key loading with path/permission checks
  logging.py           # structured logging with redaction filter
  safety.py            # observation-mode guards (exists)
  cli.py               # pmarb scan | review | replay | doctor
```

### 3.3 Core interfaces

```python
class VenueAdapter(Protocol):
    venue: Venue
    async def list_markets(self, *, horizon: timedelta) -> AsyncIterator[Market]: ...
    async def get_market(self, market_id: str) -> Market: ...           # includes rules text + raw
    async def get_book(self, market_id: str) -> Book: ...               # REST snapshot, normalized
    def book_stream(self, market_ids: Sequence[str]) -> AsyncIterator[BookEvent] | None: ...  # None without keys
    def fee_model(self, market: Market) -> FeeModel: ...

class FeeModel(Protocol):
    def taker_fee(self, levels: Sequence[Level], side: Side) -> FeeQuote: ...   # amount None = unknown

class FeeQuote(BaseModel):
    amount: Decimal | None
    provenance: str           # e.g. "kalshi-fee-pdf-2026-07-07; series KXNFLGAME taker M=1; precision=0.0001"
```

**Models (new or changed).**
- `Market`: venue, market_id, title, league, participants (canonical ids), yes_participant, market_type, line, event_start_utc, expected_resolution_utc (+ provenance), expected_payout_utc (`None` = unknown), status, rules_text, rules_hash, fee_meta, url, raw.
- `Leg`: venue, market_id, side (YES/NO). The instrument we would buy.
- `EquivalenceGroup`: group_id, members (≥2 markets with YES mapping), approved, reviewer, approved_utc, `member_rules_hashes`, exception_table, `exception_risk: bool`, notes.
- `Assessment`: group_id, yes_leg, no_leg, requested_qty, per-leg fill (levels, cost, avg, fee quote), capital, gross_edge, net_profit, roc, max_profitable_qty, book ages, skew, `books_atomic`, qualified, reasons, model_version, computed_utc.
- `Episode`: group_id, yes_leg, no_leg, qty, model_version, first_seen, last_seen, observations, peak_net, size range, `sampled: bool`, termination_reason.

### 3.4 Generalizing the phase-1 calculator

Today `calculator.assess(direction="A"|"B", kalshi_book, poly_book)` is hard-wired to the two venues. The replacement:

```python
def assess(group_id, yes_leg: LegInput, no_leg: LegInput, qty, *, now_monotonic, limits) -> Assessment
# LegInput = (Leg, Book, FeeModel). YES leg walks book.yes_asks, NO leg walks book.no_asks.
def assess_group(group, books, fee_models, qty, ...) -> list[Assessment]
# every ordered pair (i, j), i != j, of group members: YES on i, NO on j
```

Carried over unchanged:
- Decimal-only arithmetic and per-fill fees.
- `fee_unknown`, now reported per leg as `fee_unknown_<venue>`.
- Staleness and skew checks, `insufficient_depth_<venue>`, and `invalid_capital`.
- A requested size fails even when a smaller size would work.

One improvement: `max_profitable_qty` becomes an incremental scan. It walks both ladders once (marginal cost per contract never decreases) instead of re-walking for every quantity.

## 4. Venue adapters

### 4.1 Kalshi

| Item | Spec |
|---|---|
| REST base | `https://external-api.kalshi.com/trade-api/v2`. The elections host also serves every market. Keep all constants in one module. |
| Discovery | `GET /markets?status=open&mve_filter=exclude&limit=1000&cursor=…`; stop when the cursor is missing, null, or `""`. Filter **client-side** on `expected_expiration_time` within the horizon. Never send `status=all` (it returns 400). Join `/events/{event_ticker}` and `/series/{series_ticker}` by field, never by splitting the ticker. |
| Rules captured | `rules_primary`, `rules_secondary`, series `contract_terms_url` (fetch the PDF text and hash it), `settlement_sources`, strike fields, `notional_value_dollars`, `settlement_bounds_type`, `can_close_early`, `settlement_timer_seconds`. |
| Qualification gate | `market_type == "binary"`, `notional_value_dollars == 1`, `status == "active"`, no `mve_collection_ticker`, approved rules hash. |
| Book (REST) | `GET /markets/{ticker}/orderbook` → `orderbook_fp.{yes_dollars,no_dollars}` = bids as `[price, size]`. Parse with `Decimal(str)` and sort explicitly. YES acquisition = each NO bid → `(1 − price, size)`. NO acquisition = each YES bid → `(1 − price, size)`. Record `Cache-Control` and `x-kalshi-cache-hits`; a reported cache hit marks the snapshot `cached`, which is never fresh. |
| Book (WS, M2) | `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. Headers: `KALSHI-ACCESS-KEY`, `-TIMESTAMP` (ms), `-SIGNATURE` = sign(`ts + "GET" + "/trade-api/ws/v2"`) with RSA-PSS or Ed25519. Subscribe to `orderbook_delta` with `use_yes_price: true` and `update_interval_ms: 0`, plus `market_lifecycle_v2`. Apply the snapshot, then deltas in `seq` order. On a gap, parse error, or disconnect: mark affected books invalid, `get_snapshot` (or unsubscribe + resubscribe), and replace the book before applying any further delta. The 10 s heartbeat does not refresh book age. |
| Fees | `KalshiFeeModel(coefficient=0.07, multiplier=series taker multiplier, precision)`. Precision is `0.0001` for a direct member, or `0.01` plus the per-order accumulator for a non-direct member. **Which one a retail account gets is UNVERIFIED**, so it is config; unset means scenario-only. Read `fee_type`, `fee_multiplier`, and scheduled fee changes from the API, and cross-check against the PDF's series table. |
| Rate | Token bucket at 5 req/s (25% of the Basic budget). On 429, jittered exponential backoff from 1 s up to a 30 s cap, as the developer agreement §4.3 also requires. |

### 4.2 Polymarket US

| Item | Spec |
|---|---|
| Hosts | Public: `https://gateway.polymarket.us` (no auth). Authenticated: `https://api.polymarket.us`. WS: `wss://api.polymarket.us/v1/ws/markets`. |
| Discovery | `GET /v1/markets?limit=100&offset=…&active=true&closed=false` until a page comes back short. Then filter **client-side**: `status == MARKET_STATUS_OPEN`, `comboEnabled == false`, single instrument. (`active=true` alone returned resolved markets.) |
| Market detail | `GET /v1/market/slug/{slug}`. Not `/v1/markets/{slug}`, which returns 404. Rules = `description`. YES = `marketSides[].long == true`. Also read `gameStartTime`, `endDate` (expiry only), `orderPriceMinTickSize` (0.001), `minimumTradeQty`, `eventSubcategory`, `sportsMarketTypeV2`. **Never read** `outcomePrices` or `feeCoefficient` for economics. |
| Book (REST) | `GET /v1/markets/{slug}/book` → `marketData.{bids, offers}` with `px.value` and `qty` strings, plus `state` and `transactTime`. Sort explicitly. YES acquisition = offers. NO acquisition = each bid → `(1 − px, qty)`. Valid only when `state == MARKET_STATE_OPEN`. Age comes from **local receipt time**; store `transactTime` but never use it for freshness, since quiet books keep an old value. |
| Book (WS, M2) | Headers `X-PM-*`, with an Ed25519 signature over `ts + method + path`. Subscribe to `SUBSCRIPTION_TYPE_MARKET_DATA` (not lite) and replace the book on every message. Connection generations ensure levels from an old connection are never shown after a reconnect. Pin the field casing: frames that don't match the schema count as schema failures and are never merged. Measure the actual depth N on the first captured frames. |
| Fees | `PolymarketUSFeeModel(theta=config)`. Total = `round_half_even_to_cent(Σ Θ·qᵢ·pᵢ·(1−pᵢ))`. NO acquisition prices are fine because `p(1−p)` is symmetric. Θ unset → unknown. Combos are rejected (they use a different formula since 2026-11-11). |
| Capital | YES: `Σ q·p`. NO: `Σ q·(1 − p_bid)`, which equals the NO acquisition ladder cost. Released margin is not added again. |
| Rate / gaps | 5 req/s. Thursday 06:00–08:00 ET maintenance is a scheduled gap; books stay invalid until the first complete snapshot after it. |

### 4.3 Gemini Titan (M4)

Public endpoints: `GET https://api.gemini.com/v1/book/{symbol}` (bids and asks with `price` and `amount`) and `GET /v1/prediction-markets/events?status=active`. Before an adapter, targeted research must confirm:
- whether `amount` is counted in contracts
- the contract value ($1?)
- the fee schedule
- retail eligibility
- settlement rules

Until each of these is confirmed, it stays UNVERIFIED and blocks qualification.

## 5. Matching and review

### 5.1 Candidate generation (deterministic)

1. **Normalize** each market to `(league, {participant ids}, yes_participant, event_date_local, market_type, line)`:
   - **League:** from the Kalshi series (`KXNFLGAME`, `KXNBAGAME`, `KXNHLGAME`, `KXNCAAFGAME`, `KXNCAAMBGAME`, `KXNCAAWBGAME`) and the Polymarket `eventSubcategory` or slug prefix (`aec-nfl-…`).
   - **Participants:** one alias table per league (`matching/data/<league>.csv`: canonical id, names, nicknames, city labels, Kalshi code, Polymarket slug code). The tables must handle the observed traps: "New York G/J", "(W)" women's markers, and Polymarket team labels like "Jets" (Winnipeg, NHL).
   - **Event time:** Polymarket `gameStartTime` and the time named in the description. For Kalshi, the date in the title and rules, with `expected_expiration_time − 3 h` used only as a *hint* (F10).
2. **Join** on league + participant set + event time within ±6 h.
3. **Polarity:** Kalshi YES for team T pairs with Polymarket YES when `long == T`, and with Polymarket NO otherwise. Either polarity works; it only decides which legs form each direction.
4. **Rank** by fuzzy title similarity, used only as a tie-breaker. Nothing is ever auto-approved.
5. **v1 market types: winner only.** Spreads and totals stay disabled until each has its own equivalence rules ("21.5" means "wins by" on one venue and "covers +" on the other).

### 5.2 Review (human)

The side-by-side page shows, for each member market: the full rules text, the contract-terms PDF text (Kalshi), the settlement source, the timing fields, and an exception table:

| Outcome | Venue A pays YES | Venue B pays YES | $1 pair holds? |
|---|---|---|---|
| Played, regulation | 1/0 | 1/0 | yes |
| Overtime / shootout | per rules | per rules | must match |
| Tie | e.g. 0.50 | e.g. 0.50 | yes if both 0.50 |
| Postponed within window | rescheduled result | rescheduled result | windows differ (48 h vs 2 days) → flag |
| Cancelled | fair price | fair price | **no** → `exception_risk = true` |

Approval stores each member's `rules_hash`: a sha256 over the normalized rules text, the terms PDF text, and the settlement-source fields. Any later hash change **revokes** the approval automatically and puts the group back in the review queue. Every approval records the reviewer and a timestamp.

## 6. Collector runtime

### 6.1 Loops (asyncio supervisor; each loop restarts with backoff on crash)

| Loop | M1 | M2 |
|---|---|---|
| Catalog refresh | every 10 min: discovery on both venues → candidates | same |
| Metadata/rules refresh | every 2 min for watched markets: detect rule changes → revoke | + `market_lifecycle_v2` events |
| Books | poll the REST book for each watched market, both legs of a group fetched **concurrently**; cadence 2 s per group, ≤10 groups (M1 cap) | WS streams; REST only as a labelled fallback |
| Freshness timer | every 250 ms: expire stale books even when nothing arrives | same |
| Evaluator | on each book update for a group member, plus every timer tick: all directions × size views (1, 10, 100) | same |
| Recorder | single writer queue: SQLite batch commit every 500 ms, plus raw-log append | same |
| Health | heartbeat row every 10 s: queue depth, last book per venue, error counts | + per-connection state |

**Backpressure:** if the recorder queue exceeds its limit, mark all books invalid (`recording_backpressure`), close open episodes, and drop to the slowest cadence until the queue drains. A gap is never silently skipped.

**Polling caveat:** with REST polling, the two legs are fetched roughly 50–300 ms apart. Polled assessments are therefore marked `books_atomic=false` and `sampled=true`, and episodes report sampled durations. This is why M1 proves correctness, not edge (F2).

### 6.2 Freshness rules (defaults)

- A book is fresh only if all of these hold:
  - `now_mono − received_mono ≤ 2.0 s`
  - it came from a complete snapshot on the current connection generation
  - it is not `cached`
- The skew between legs must be ≤ 1.0 s.
- The book goes invalid immediately on any of: a disconnect, a parse failure, an impossible book (crossed or negative), a sequence gap, the maintenance window, or backpressure.
- Heartbeats never refresh book age.

### 6.3 Episodes

There is one state machine per `(group, yes_leg, no_leg, qty, model_version)`:
- **`CLOSED → OPEN`** on the first qualified assessment.
- **Stays `OPEN`** while every evaluation is qualified and no gap exceeds 2× the cadence.
- **`OPEN → CLOSED`** with one of these reasons: `disqualified:<reason>`, `data_gap`, `collector_restart`, `rules_changed`, `market_closed`.

Downtime never counts as persistence.

### 6.4 Storage (under `PMARB_DATA_DIR`, default `./data`, gitignored)

**SQLite** (WAL, `synchronous=NORMAL`) tables:

```sql
markets(market_key TEXT PRIMARY KEY, venue TEXT, venue_market_id TEXT, title TEXT, league TEXT,
        participants_json TEXT, yes_participant TEXT, market_type TEXT, line TEXT,
        event_start_utc TEXT, expected_resolution_utc TEXT, expected_resolution_src TEXT,
        expected_payout_utc TEXT, status TEXT, rules_hash TEXT, fee_meta_json TEXT, url TEXT,
        first_seen_utc TEXT, last_seen_utc TEXT);
rules_versions(market_key TEXT, rules_hash TEXT, rules_text TEXT, terms_text TEXT, captured_utc TEXT,
               PRIMARY KEY (market_key, rules_hash));
groups(group_id TEXT PRIMARY KEY, status TEXT CHECK(status IN ('candidate','approved','revoked','rejected')),
       exception_risk INTEGER, exception_table_json TEXT, reviewer TEXT, decided_utc TEXT, notes TEXT);
group_members(group_id TEXT, market_key TEXT, yes_maps_to TEXT, approved_rules_hash TEXT,
              PRIMARY KEY (group_id, market_key));
review_requests(id INTEGER PRIMARY KEY, group_id TEXT, action TEXT, reviewer TEXT, payload_json TEXT,
                created_utc TEXT, applied_utc TEXT);
assessments(id INTEGER PRIMARY KEY, group_id TEXT, yes_market_key TEXT, no_market_key TEXT, qty TEXT,
            qualified INTEGER, reasons_json TEXT, gross_edge TEXT, net_profit TEXT, roc TEXT, capital TEXT,
            fee_yes TEXT, fee_no TEXT, fee_provenance_json TEXT, avg_yes TEXT, avg_no TEXT,
            max_profitable_qty TEXT, age_yes_ms INTEGER, age_no_ms INTEGER, skew_ms INTEGER,
            books_atomic INTEGER, model_version TEXT, computed_utc TEXT);
episodes(id INTEGER PRIMARY KEY, group_id TEXT, yes_market_key TEXT, no_market_key TEXT, qty TEXT,
         model_version TEXT, first_seen_utc TEXT, last_seen_utc TEXT, observations INTEGER,
         peak_net TEXT, min_size TEXT, max_size TEXT, sampled INTEGER, termination_reason TEXT);
collector_events(id INTEGER PRIMARY KEY, utc TEXT, venue TEXT, kind TEXT, detail_json TEXT);
```

Money and size columns are TEXT, holding exact Decimal strings. To bound volume, an assessment row is stored only when something changes (qualification, reasons, or net profit at 4 dp), plus one heartbeat sample per minute.

**Raw log:** `data/raw/<venue>/<YYYY-MM-DD>/<HH>.jsonl.gz`. Each envelope is `{recv_utc, recv_mono_ns, venue, kind (rest_book|ws_msg|market|conn), conn_gen, endpoint, status, cache_headers, body}`. Every received book and WS message goes into it, so it can be replayed. A checkpoint (the full normalized book for each watched market) is written every 60 s. Retention: raw data 30 days, summaries indefinitely (the `plan.md` default).

## 7. Review app (Streamlit, bound to 127.0.0.1)

1. **Candidates:** the generator's queue, with the match keys and why each pair was proposed.
2. **Review:** side-by-side rules and the exception table. Actions: approve, reject, or mark exception-risk, with notes.
3. **Live:** qualified opportunities ranked by net $ at the requested size, then ROC. Each row shows both legs, prices, fees with provenance, book ages, skew, and the atomic/sampled flags. A separate filterable tab lists unqualified results with all their reasons.
4. **Episodes:** searchable history with links to both markets.
5. **Health:** per-venue connection state, last book age, error and 429 counts, recorder lag, and gaps (including scheduled maintenance).

Times are stored in UTC and displayed in America/Chicago with zone labels.

## 8. Security and compliance

- **Secrets.** `secrets.py` loads keys from `KALSHI_PRIVATE_KEY_PATH` / `POLYUS_PRIVATE_KEY_PATH`. It refuses to start if:
  - a key file is inside the repo,
  - a key file's permissions are looser than 0600, or
  - a secret value appears in any git-tracked file.

  Keys are never logged, stored in SQLite, or written to the raw log. Request envelopes store paths, not headers.
- **Redaction.** A logging filter masks `KALSHI-ACCESS-*`, `X-PM-*`, `Authorization`, PEM blocks, and base64 strings longer than 40 characters. A test feeds it each of these.
- **No order path.** A test walks the import graph of `pmarb.cli scan|review|replay` and fails on any module under `pmarb.execution` or any order-create endpoint string (`/portfolio/events/orders`, `/portfolio/orders`, `orders.create`).
- **Keys.** One account and one key per person, using the `read` scope where offered. Keys are collected through Pane's secrets prompt, never pasted into chat, prompts, or files.
- **Public repo.** Code and synthetic fixtures only. Real data stays in gitignored `data/`. No live values in docs. No Kalshi or Polymarket branding or marketing claims. gitleaks runs in pre-commit and in CI over the full history.
- **Network hygiene.** HTTPS/WSS only, with TLS verification on. A descriptive User-Agent with no personal data.

## 9. Testing

| Tier | Where | Runs in CI | What |
|---|---|---|---|
| Unit | `tests/` | yes | arithmetic, fees, ladders, normalization, parsing, state machines |
| Fixture | `tests/fixtures/` (synthetic, committed) | yes | adapter parsing against hand-built payloads in the verified formats |
| Property | `tests/` (hypothesis) | yes | ladder walking (monotone cost, no level reused), fees ≥ 0, complement applied exactly once |
| Simulated network | `tests/` with fake HTTP/WS servers | yes | seq gaps, reconnect generations, 429/503 backoff, maintenance window, schema drift |
| Local data | `@pytest.mark.localdata`, reads `data/` | **no** (local only) | replay real recordings through parsers and the evaluator; regression snapshots |
| Soak | manual, documented | no | 24 h run, forced kill and restart, network drop |

Must-have test cases, beyond the 39 existing tests:
- Kalshi REST complement works for both array orders.
- WS `use_yes_price` never double-complements.
- A sequence gap blocks qualification until the snapshot is replaced.
- A cached Kalshi body is never fresh.
- The Polymarket US NO ladder is built from bids (fixed numbers).
- A market with `active=true` that is already resolved is excluded.
- A quiet book with an old `transactTime` stays fresh based on receipt time.
- A rules change revokes approval.
- A Cardi B-style exception (venues settle differently) sets `exception_risk`.
- Θ = `None` keeps results unqualified.
- Kalshi fees round up to the centicent.
- Polymarket US fees round half-even once per order.
- The maintenance window makes the book invalid with reason `maintenance`.
- A restart closes episodes with `collector_restart`.
- The order-path import guard.

## 10. Milestones and pull requests

Each task is built by a Grok worker in its own git worktree, so parallel tasks don't collide. The orchestrator reviews it (reads the diff and runs the tests), Haiku spot-checks where useful, and then it is pushed as a PR for Rishi to review. Sizes are in agent time: S < ½ day, M ≈ 1 day, L ≈ 2 days.

### M1 — keyless observer (target: 24 h clean run)

| PR | Tasks | Size | Depends on | Parallel with |
|---|---|---|---|---|
| **PR-1 Core generalization** | `economics.py` N-venue assess; `fees/` protocol + Kalshi/Polymarket US models (series multiplier, precision config); incremental max-size scan; migrate the 39 tests | M | — | PR-2 |
| **PR-2 Foundations** | `config.py`, `secrets.py`, `logging.py` (redaction), `storage/db.py` (schema §6.4, migrations, single-writer queue), `storage/rawlog.py`; tests | M | — | PR-1 |
| **PR-3 Kalshi REST adapter** | discovery, market + rules + terms PDF text, book parse with cache flags, rate limiter; synthetic fixtures | M | PR-1 | PR-4 |
| **PR-4 Polymarket US REST adapter** | discovery with client-side filters, slug detail, book parse, maintenance calendar; synthetic fixtures | M | PR-1 | PR-3 |
| **PR-5 Matching + review store** | team alias tables (NFL, NBA, NHL, NCAAF, NCAAMB, NCAAWB), candidate generator, rules hashing, exception table, revocation | L | PR-3, PR-4 | PR-6 (partly) |
| **PR-6 Collector** | supervisor, polling loop, BookStore, freshness timer, evaluator, episodes, health, backpressure; simulated-network tests | L | PR-2, PR-3, PR-4 | — |
| **PR-7 Review app + CLI** | Streamlit pages §7, `pmarb scan`, `pmarb review`, `pmarb doctor` (config/keys/data-dir checks) | M | PR-5, PR-6 | — |
| **PR-8 Soak report** | 24 h run log: uptime, gaps, 429s, restart test, sampled episodes; fixes found | S–M | PR-7 | — |

**Your task, in parallel (gates M2): accounts.** Each of you opens your own Kalshi and Polymarket US account and creates a read-scoped API key.
- **Polymarket US:** install the iOS app, complete identity verification, then create the key at polymarket.us/developer.
- **Kalshi:** create the key in account settings with the `read` scope.
- **Record two things:** whether your Kalshi account is a direct member (this sets fee precision), and your Polymarket US Θ tier.

### M2 — streaming (needs keys)

- **Auth:** signing modules for both venues, tested against known vectors.
- **Kalshi WS:** snapshot, delta, `seq`, `get_snapshot` recovery, lifecycle events.
- **Polymarket US WS:** snapshot replace, connection generations, schema pinning.
- **Verification:** capture real frames to confirm depth N and field casing. Add forced-disconnect integration tests, then a 7-day stability run.
- **Scope:** raise the group cap beyond 10 and add an in-game focus (F2).
- **Portable deployment** to your always-on machine: a launch script and service file, `.env` kept outside the repo, a clock-sync check, the data dir, and log rotation.

### M3 — replay and evidence

- **Replay engine:** feeds the raw log through the same parsers, BookStore, and evaluator, using only information that existed at decision time.
- **Latency and fill simulator:**
  - per-leg delays of 100/250/500/1000/2000 ms, asymmetric delays, and jitter
  - FOK and IOC orders, partial fills, and one-sided fills
  - depth haircuts of 25% and 50%
  - separate cash ledgers per venue, with no reuse of the same liquidity
- **30-day evidence report** using event-level statistics (`plan.md` §4). The go/no-go criteria are the ones in `plan.md` §4.

### M4 — third venue

Targeted Gemini Titan research (§4.3), then its adapter and fee model. The matching tables extend unchanged because groups are already N-venue.

### M5 — execution (gated)

Starts only after the M3 evidence review **and** your explicit approval of a separate risk budget. It is a separate package and process: risk limits default to zero, order intents are durable, positions are reconciled, there is a kill switch, and one-sided fills are handled as specified in `plan.md` §4.

## 11. Defaults

| Setting | Default |
|---|---|
| Venues (M1) | Kalshi, Polymarket US |
| Universe | sports game winners: NFL, NBA, NHL, NCAAF, NCAAMB, NCAAWB |
| Horizon | game start within 7 days |
| Watched groups (M1) | ≤ 10 approved |
| Poll cadence | 2 s per group, both legs concurrent |
| Freshness / skew | 2.0 s / 1.0 s |
| Size views | 1, 10, 100 contracts (requested = 100) |
| Qualification | all checks pass, net > 0, net ≥ $0.50 and ROC ≥ 0.5% at requested size |
| Kalshi fee | 0.07 × series multiplier, ceil to centicent; precision config (unset → scenario) |
| Polymarket US Θ | config (unset → scenario) |
| Rate budgets | 5 req/s per venue |
| Data dir | `./data` (gitignored) |
| Retention | raw 30 days; summaries indefinitely |
| Display time zone | America/Chicago |

## 12. Risks, open questions, decision log

### Open questions (each with how and when it gets resolved)

| # | Question | Resolve by | When |
|---|---|---|---|
| Q1 | Is a retail Kalshi account a direct member (fee precision $0.0001 vs $0.01)? | Check account docs/settings; reconcile one small fill later | when accounts open |
| Q2 | Can a Kalshi `read` key open the WS and subscribe to `orderbook_delta`? | First key: subscribe test | M2 start |
| Q3 | Polymarket US WS depth N and field casing | Capture the first frames | M2 start |
| Q4 | Your Polymarket US Θ tier and onboarding window | Account settings / support | when accounts open |
| Q5 | Polymarket US rate limit as actually enforced | Observe 429s while ramping slowly | M1 soak |
| Q6 | Is there a Kalshi batch order-book endpoint (to cut request count)? | Docs check during PR-3 | PR-3 |
| Q7 | Is every Polymarket US team market "first team = long"? | Always use `marketSides[].long`; log any disagreement with slug order | PR-4 |
| Q8 | When do payouts release on each venue? | Observe settlements on watched markets | M2–M3 |
| Q9 | Are in-game gaps real or artifacts of non-atomic fetches? | M2 streaming + M3 replay | M3 |

### Top risks

1. **No real edge.** Pregame pricing is efficient, and in-game gaps may be latency artifacts. *Mitigation:* the plan answers this cheaply before any capital is at risk, and "no opportunities" is a valid result.
2. **Settlement divergence** (the Cardi B type). *Mitigation:* rules hashing, the exception table, and the exception-risk flag; nothing qualifies without human review.
3. **One-sided fills** once trading starts, because there is no cross-venue atomic order. *Mitigation:* the M3 simulation quantifies it; M5 handles it as specified.
4. **Account or terms risk.** *Mitigation:* official APIs only, conservative rates, one account per person, and no data publication.

### Decision log (summary)

| ID | Decision | Status |
|---|---|---|
| D1 | Observation-only until the evidence review; no order client in scan/review/replay | active |
| D2 | US-regulated venues only; global Polymarket is never an execution venue | active (user) |
| D3 | N-venue architecture: adapters, equivalence groups, any (YES venue, NO venue) pair | active (user) |
| D4 | v1 universe: sports game winners | active |
| D5 | Deterministic matching plus human approval bound to rules hashes; exception-risk flag | active |
| D6 | M1 public REST polling; M2 streaming (Kalshi WS deltas, Polymarket US WS snapshots) | active |
| D7 | Freshness from local monotonic receipt time; 2 s / 1 s defaults; cached bodies never fresh | active |
| D8 | Fees carry provenance; unknown fees → unqualified; Polymarket US Θ is per account | active |
| D9 | Equal integer contract quantities; Polymarket US NO consumes `q(1−p)`; margin never double-counted | active |
| D10 | SQLite WAL single writer + gzip JSONL raw log + checkpoints | active |
| D11 | Rate budgets 5 req/s per venue; Polymarket US Thursday maintenance is a scheduled gap | active |
| D12 | Keys by path outside the repo, 0600, read scope, redaction, Pane secrets prompt | active |
| D13 | Python 3.12, Decimal, Pydantic v2, httpx, websockets, Streamlit (localhost) | active |
| D14 | Milestones M1–M5 as in §10 | active |
| D16 | Never trust venue level ordering; always sort explicitly | active |
| D19 | Kalshi fee formula verified against the 2026-07-07 schedule | active |
| D20 | Gemini Titan is the M4 candidate; broker brands routing to Kalshi are not venues | active |
| D22 | Data in gitignored `./data`, private use only, never public; cross-venue analytics is not treated as benchmarking | active (user) |
| D23 | Two test tiers: synthetic in CI, `localdata` local only | active (user) |
| D24 | No accounts yet (M1 keyless); long runs later on the user's own machine; agents build, Rishi reviews PRs | active (user) |
