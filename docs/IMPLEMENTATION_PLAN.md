# Implementation plan

**Status:** v1.1, 2026-11-11 (interfaces frozen in PR-0: `src/pmarb/models.py`, `src/pmarb/protocols.py`, `src/pmarb/storage/migrations/0001_initial.sql`). Live status: `docs/PROGRESS.md`. Supersedes the build sections of `plan.md`; `plan.md` remains the strategy and evidence rationale.
**Inputs:** `plan.md`, `docs/research/*` (fact-checked), decision log in §12. v1.1 applies an adversarial review and a cross-check against an independent draft (changes listed in §13).
**Workflow:** agents build on `avyukth-dev`, one pull request per slice (§10), Rishi reviews and merges into `main`.

---

## 1. Goal, scope, non-goals

**Goal.** Find out, with trustworthy evidence, whether equal-quantity cross-venue arbitrage between US-regulated prediction markets is real after fees, depth, latency and settlement risk — and only then consider trading it.

**The trade.** For one real-world outcome listed on two venues, buy YES on one venue and NO on the other, the same number of contracts on both legs. If both venues settle the same way, exactly one leg pays $1 per contract. Profit = `q × $1 − (both legs' costs + both fees + extra costs)`.

**What M1 delivers (no API keys needed).** A local tool that discovers matching sports game-winner markets on Kalshi and Polymarket US, lets a human approve pairs after a side-by-side rules review, polls both public order books, computes executable both-direction profit at a chosen size, records everything, and shows a ranked live table plus history. It must run 24 hours unattended and recover from disconnects, sleep and restarts.

**What M1 can and cannot tell us.** Polled books on two venues are never fetched at the same instant, so M1 results are **indicative, never qualified** (§6.2). M1's real question is a cheap one: during live games, are cross-venue gaps larger than the 50–300 ms polling skew, how often, and how deep at the touch? If no, we stop cheaply. If yes, M2 streaming measures whether they are tradable.

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
| F3 | Kalshi taker fee = `round up(M × 0.07 × C × P × (1−P))` (fee schedule PDF, "last updated and effective July 7, 2026", read 2026-11-11 in a browser because scripted fetches are bot-blocked). Exact rounding is the algorithm on docs.kalshi.com `getting_started/fee_rounding`: the model fee is ceiled to 6 dp, `revenue − fee` is aligned to member precision ($0.0001 direct, $0.01 non-direct), and a per-order accumulator rebates the excess. Sports game series list maker 1 / taker 1. The series `fee_type` and `fee_multiplier` come from the API. | Implement the documented algorithm, tested against its official examples. Until then, use a conservative upper bound (ceil each order to member precision). A series with no known multiplier → `fee_unknown`. |
| F4 | Polymarket US taker fee = `Θ × C × p × (1−p)`. Each fill is rounded half-even to the cent, and the order's cumulative charge is capped at `round_half_even(Σ exact)`. So the order total is **≤** that cap and can be lower (a 1-lot fill can round to $0.00). **Θ is per account**: 0.095 / 0.085 / 0.075 / 0.0695, and 0.0695 for ET days 1–7 after the first fill. The market field `feeCoefficient` is always 0.095 and is not your Θ. | Assessments use the cap (`round_half_even(Σ exact)`) as a **conservative bound**, because an aggregated book hides how the order would be split into fills. Θ is user config; if unknown, the result is scenario only. |
| F5 | Polymarket US has **one instrument per market**. Buying NO means shorting YES at the YES bid `p`, which consumes `q × (1−p)` of buying power. The YES side is `marketSides[].long == true`. | NO acquisition ladder = `1 − YES bid`, with the bid's size. Never use `outcomePrices`; it was observed mislabeled. |
| F6 | Kalshi REST books are **bids only** (YES bids and NO bids) as `[dollar_string, fp_size]`. The docs say "best to worst", but live data was ascending. Sizes are fractional (`*_fp`). | YES ask = `1 − NO bid`. **Always sort explicitly**; never trust the array order. Decimal everywhere. |
| F7 | Kalshi WS `orderbook_delta` carries `sid`/`seq`. On a gap, send `get_snapshot` or unsubscribe and resubscribe; a second subscribe does **not** resync. With `use_yes_price=true`, NO levels arrive already on the YES scale, so never complement them twice. The lifecycle channel is `market_lifecycle_v2`. | Gap handling is part of the M2 adapter contract and its tests. |
| F8 | Polymarket US WS market data sends **multi-level snapshots** (depth configurable). Each message is complete and there are no sequence numbers. The public REST book needs no auth. | M1 polls REST. M2 replaces the whole book on every WS message. |
| F9 | Kalshi public listing responses are cached (`max-age=15`, `x-kalshi-cache-hits`), and market top-of-book sizes disagreed with the order book. | Use only order-book endpoints for prices. Record cache headers; a cached body is never "fresh". |
| F10 | Polymarket US `endDate` is ≈2 weeks after the game. Kalshi `close_time` ≠ expected resolution. Kalshi `occurrence_datetime` / `expected_expiration_time` sat **+3 h** after the Polymarket start time during EST; that is an observation, not a documented game length. Once a game goes live, `expected_expiration_time` can pass while the market is still `active`. | Join on league + teams + the **America/New_York date** written in the rules. Never filter by `endDate` or `close_time`. The horizon includes games that already started (§11). |
| F11 | Cancellation or postponement settles at **each venue's own "fair price"** (or $0.50 on a tie), and the postponement windows differ (48 h vs "two days"). | A pair is "equivalent for a played game" plus an explicit **exception-risk** flag shown in the UI. Never silently treated as a guaranteed $1. |
| F12 | Polymarket US has weekly maintenance **Thursday 06:00–08:00 ET**: 503 errors, resting orders cancelled, books reopen empty. There is no retail sandbox. | Treat it as an expected gap; books stay invalid until a fresh snapshot. Everything is read-only against production. |
| F13 | Rate limits: Kalshi Basic read is 200 tokens/s at 10 tokens per request (≈20 req/s), and `GET /markets/orderbooks` returns 1–100 books in one call. Polymarket US is 20–25 req/s per IP (the docs disagree), with one book per request. A full-catalog crawl is huge: about 79k open Polymarket US markets and over 180k open Kalshi markets. | Discover by sports series and league only. Use separate rate budgets for discovery and books. Batch Kalshi books. Honour 429s with jittered backoff. |
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
- The **review app** opens SQLite **read-only** (`mode=ro`) and keeps each read short. It writes each review decision as one JSON file in `data/spool/reviews/`. The collector picks these up, applies them, and moves them to `applied/`. The collector is the only SQLite writer, which avoids `SQLITE_BUSY` errors and WAL growth from long-held readers.
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
    async def list_markets(self, *, horizon_start: datetime, horizon_end: datetime) -> AsyncIterator[Market]: ...
    async def get_market(self, market_id: str) -> Market: ...           # includes rules text + raw
    async def get_books(self, market_ids: Sequence[str]) -> list[Book]: ...  # REST snapshots, batched where supported
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
- `EquivalenceGroup`: group_id, `outcome` (the single real-world outcome every member's YES represents, e.g. "NE beats DET, OT included"), members (≥2 markets on **different venues**, each with `native_side_for_outcome` = YES or NO), approved, reviewer, approved_utc, `member_rules_hashes`, `hash_version`, exception_table, `exception_risk: bool`, notes.
- `Assessment`: group_id, yes_leg, no_leg, requested_qty, per-leg fill (levels, cost, avg, fee quote), capital, gross_edge, net_profit, roc, max_profitable_qty, book ages, skew, `books_atomic`, qualified, reasons, model_version, computed_utc.
- `Episode`: group_id, yes_leg, no_leg, qty, model_version, first_seen, last_seen, observations, peak_net, size range, `sampled: bool`, termination_reason.

### 3.4 Generalizing the phase-1 calculator

Today `calculator.assess(direction="A"|"B", kalshi_book, poly_book)` is hard-wired to the two venues. The replacement:

```python
def assess(group_id, yes_leg: LegInput, no_leg: LegInput, qty, *, now_monotonic, limits) -> Assessment
# LegInput = (Leg, Book, FeeModel). YES leg walks book.yes_asks, NO leg walks book.no_asks.
def assess_group(group, books, fee_models, qty, ...) -> list[Assessment]
# for every ordered pair of members (i, j) on different venues: buy "outcome happens" on i and "outcome does not happen" on j
```

**A group holds exactly one outcome.** Every member expresses the same outcome, so a YES on one member and a NO on another always pay exactly $1 between them. Kalshi lists one binary per team, so "A wins" and "B wins" are **different outcomes** and never share a group: YES on "A wins" plus NO on "B wins" is the same bet twice (it pays $2 or $0), and an NFL tie would hide that. With one Polymarket US instrument per game (YES = `long` team), each game normally produces one group: Kalshi "long team wins" + the Polymarket US market. Where a member's native YES is the opposite outcome, `native_side_for_outcome = NO` flips which ladder is walked. That is valid only when the two outcomes are exact complements on that venue (no tie or void path); otherwise the member is rejected.

Carried over unchanged:
- Decimal-only arithmetic and per-fill fees.
- `fee_unknown`, now reported per leg as `fee_unknown_<venue>`.
- Staleness and skew checks, `insufficient_depth_<venue>`, and `invalid_capital`.
- A requested size fails even when a smaller size would work.

**`max_profitable_qty`** = the largest whole number of contracts q ≤ min(scan limit, depth) that passes **the same test as `qualified`**: net > 0, net ≥ the minimum, ROC ≥ the minimum, and fresh books. It checks **every** q and recomputes each order-level fee over the whole walk. Cent and centicent rounding make profit non-monotone in q, so early stopping or binary search would give wrong answers. Cost is ≤ 1,000 walks, run only when a book changes.

## 4. Venue adapters

### 4.1 Kalshi

| Item | Spec |
|---|---|
| REST base | `https://external-api.kalshi.com/trade-api/v2`. The elections host also serves every market. Keep all constants in one module. |
| Discovery | **By series only:** `GET /markets?series_ticker=<S>&status=open&limit=1000&cursor=…` for S in `KXNFLGAME, KXNBAGAME, KXNHLGAME, KXNCAAFGAME, KXNCAAMBGAME, KXNCAAWBGAME`. Stop when the cursor is missing, null, or `""`. Never send `status=all` (it returns 400). Join `/events/{event_ticker}` and `/series/{series_ticker}` by field, never by splitting the ticker. A watched market stays watched while `status == "active"`, even after its `expected_expiration_time` has passed. |
| Rules captured | `rules_primary`, `rules_secondary`, `settlement_sources`, strike fields, `notional_value_dollars`, `settlement_bounds_type`, `can_close_early`, `settlement_timer_seconds`, and series `contract_terms_url`. **The terms PDF is linked and shown to the reviewer but not hashed in M1**: extracted PDF text depends on the extractor, so a re-extract would revoke every group. If it is fetched, use an allowlist (`https://assets.kalshi.com` only), refuse redirects to other hosts, and cap the size at 5 MB. |
| Qualification gate | `market_type == "binary"`, `Decimal(notional_value_dollars) == 1` (the wire value is `"1.0000"`), `status == "active"`, `settlement_bounds_type == "default"`, no `mve_collection_ticker`, approved rules hash. |
| Book (REST) | **Batch:** `GET /markets/orderbooks?tickers=A&tickers=B…` (1–100 tickers, **repeated** `tickers` params; no auth; default 10 tokens; VERIFIED). **Trap:** a comma-joined `tickers=A,B` returns HTTP 200 with one empty book whose ticker is the literal `"A,B"`. The adapter must send repeated params and reject any response whose tickers differ from those requested, and a test guards this. Single: `GET /markets/{ticker}/orderbook`. Each book is `orderbook_fp.{yes_dollars,no_dollars}` = bids as `[price, size]`. Parse money **from the wire strings** with `Decimal(str)` (never from JSON floats) and sort explicitly. YES acquisition = each NO bid → `(1 − price, size)`. NO acquisition = each YES bid → `(1 − price, size)`. Record `Cache-Control` and `x-kalshi-cache-hits`; a reported cache hit marks the snapshot `cached`, which is never fresh. |
| Book (WS, M2) | `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. Headers: `KALSHI-ACCESS-KEY`, `-TIMESTAMP` (ms), `-SIGNATURE` = sign(`ts + "GET" + "/trade-api/ws/v2"`) with RSA-PSS or Ed25519. Subscribe to `orderbook_delta` with `use_yes_price: true` and `update_interval_ms: 0`, plus `market_lifecycle_v2`. Apply the snapshot, then deltas in `seq` order (`seq` is per `sid`). A delta whose resulting size is ≤ 0 removes the level. A negative result, an unknown level, or a crossed book invalidates the book. On a gap, parse error, or disconnect: mark affected books invalid, `get_snapshot` (or unsubscribe + resubscribe), and replace the book before applying any further delta. Never merge a REST book into a `seq` stream. The 10 s heartbeat does not refresh book age. |
| Fees | `KalshiFeeModel(coefficient=0.07 [PDF 2026-07-07], multiplier, precision)`. The multiplier comes from the series `fee_multiplier` / `fee_type` plus any scheduled series or event fee changes and `fee_waiver_expiration_time`, cross-checked against the PDF's series table. If none is known → `fee_unknown`. Precision is `0.0001` (direct member) or `0.01` (non-direct); the per-order accumulator applies to **both**. **Which precision a retail account gets is UNVERIFIED**, so it is config; unset → scenario-only. The maker formula (0.0175 × M) is stored but not used for taker assessments. |
| Rate | Separate token buckets: **books 3 req/s** (batched, so one call covers every watched Kalshi market) and **discovery/metadata 1 req/s**. On 429 or 5xx, jittered exponential backoff from 1 s up to a 30 s cap, as the developer agreement §4.3 also requires. HTTP timeouts: connect 3 s, read 5 s. |

### 4.2 Polymarket US

| Item | Spec |
|---|---|
| Hosts | Public: `https://gateway.polymarket.us` (no auth). Authenticated: `https://api.polymarket.us`. WS: `wss://api.polymarket.us/v1/ws/markets`. |
| Discovery | **By league only:** `GET /v2/leagues/{slug}/events` (documented, VERIFIED live). League slugs come from `GET /v2/leagues` (paginated with `limit` ≤ 50 and `offset`; `nba` is not on page 1). Each event carries `startTime` and its markets. Fallback: `GET /v1/markets?categories=sports&limit=100&offset=…&active=true&closed=false`, filtered client-side. `sportsMarketTypes` takes the `SPORTS_MARKET_TYPE_*` enum but is not strictly exclusive, so filter again client-side. In both cases keep only `status == MARKET_STATUS_OPEN`, `comboEnabled == false`, a single instrument, and the winner market type. (`active=true` alone returned resolved markets.) |
| Market detail | `GET /v1/market/slug/{slug}`. Not `/v1/markets/{slug}`, which returns 404. Rules = `description`. YES = `marketSides[].long == true`. Market type: winner = `sportsMarketTypeV2 == SPORTS_MARKET_TYPE_MONEYLINE` and/or `sportsMarketType` ending `_full_game_winner`. Store both and flag any disagreement. Start time: event `startTime`, falling back to market `gameStartTime` (both VERIFIED present live; any deprecation notice is unconfirmed). Tick: `orderPriceMinTickSize` is per market (0.01, 0.005, 0.0025, 0.001 all seen), so treat it as data and parse it with `Decimal(str(...))` because it arrives as a JSON number. `endDate` is expiry only. **Never read** `outcomePrices` or `feeCoefficient` for economics. PR-4 pins every one of these field names against synthetic fixtures built from the verified formats. |
| Book (REST) | `GET /v1/markets/{slug}/book` → `marketData.{bids, offers}` with `px.value` and `qty` strings, plus `state` and `transactTime`. Sort explicitly. YES acquisition = offers. NO acquisition = each bid → `(1 − px, qty)`. Valid only when `state == MARKET_STATE_OPEN`. Age comes from **local receipt time**; store `transactTime` but never use it for freshness, since quiet books keep an old value. |
| Book (WS, M2) | Headers `X-PM-*`, with an Ed25519 signature over `ts + method + path`. Subscribe to `SUBSCRIPTION_TYPE_MARKET_DATA` (not lite) and replace the book on every message. Connection generations ensure levels from an old connection are never shown after a reconnect. Pin the field casing: frames that don't match the schema count as schema failures and are never merged. Measure the actual depth N on the first captured frames. |
| Fees | `PolymarketUSFeeModel(theta=config)`. Assessment fee = `round_half_even_to_cent(Σ Θ·qᵢ·pᵢ·(1−pᵢ))`. That is the documented **per-order cap**: actual per-fill half-even charges can only total **≤** it, so it is a conservative bound, and assessments say so. NO acquisition prices are fine because `p(1−p)` is symmetric. Θ unset → unknown. Combos are rejected (they use a different formula since 2026-11-11). |
| Capital | YES: `Σ q·p`. NO: `Σ q·(1 − p_bid)`, which equals the NO acquisition ladder cost. Released margin is not added again. |
| Rate / gaps | Separate buckets: **books 4 req/s** (one request per market) and **discovery/metadata 1 req/s**. The poll cadence adapts to `n_watched / 4` seconds, with a 1 s minimum. Thursday 06:00–08:00 **America/New_York** (computed with `ZoneInfo`, DST-correct) is a scheduled gap; books stay invalid until the first complete snapshot after it. Timeouts: connect 3 s, read 5 s. |

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

1. **Normalize** each market to `(league, {participant ids}, yes_participant, event_date_ny, market_type, line)`:
   - **League:** from the Kalshi series (`KXNFLGAME`, `KXNBAGAME`, `KXNHLGAME`, `KXNCAAFGAME`, `KXNCAAMBGAME`, `KXNCAAWBGAME`) and the Polymarket league slug used for discovery. Not `eventSubcategory`, which was `"SOCCER"` on a futures contract.
   - **Participants:** one alias table per league (`matching/data/<league>.csv`: canonical id, names, nicknames, city labels, Kalshi code, Polymarket slug code). The tables must handle the observed traps: "New York G/J", "(W)" women's markers, and Polymarket team labels like "Jets" (Winnipeg, NHL).
   - **Event date:** the **America/New_York calendar date** written in each venue's rules/question text, plus Polymarket `startTime`. Kalshi `occurrence_datetime` is supporting evidence only (F10).
2. **Join** on league + participant set + equal NY date. If a team plays twice on one date (a doubleheader), require start times within 3 h and otherwise send the pair to manual review.
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

**Rules hash (`hash_version = 1`):** sha256 of a canonical JSON object `{venue, market_id, rules_primary, rules_secondary | description, settlement_sources (sorted), strike fields, notional}`. Text is whitespace-collapsed and NFC-normalized; nothing else. Any later hash change **revokes** the approval automatically and puts the group back in the review queue. Bumping `hash_version` revokes every group by design and requires a new review.

**Stale-review guard:** a review decision carries the hashes the reviewer was shown. The collector applies it only if those still match the current hashes; otherwise the decision is rejected as `stale_review`.

**Exception risk does not disqualify; it labels.** Every sports pair has some cancellation/fair-price exposure (F11), so blocking on it would block everything. The reviewer must acknowledge the exception table to approve. The group then carries `exception_risk = true`, and every assessment and episode shows it, so the M3 evidence can be split by it. Pairs where a **normal** outcome breaks $1 (different overtime/shootout treatment, a different stat source, different tie handling) are **rejected**, not flagged. Kalshi's short `rules_secondary` can omit what the series terms PDF says (e.g. NHL shootout), so the reviewer checklist links the PDF and requires an explicit tick.

Every decision records the reviewer and a timestamp.

## 6. Collector runtime

### 6.1 Loops (asyncio supervisor; each loop restarts with backoff on crash)

| Loop | M1 | M2 |
|---|---|---|
| Catalog refresh | every 15 min, series/league-scoped (§4), on the discovery budget → candidates | same |
| Metadata/rules refresh | every 2 min for watched markets: detect rule changes → revoke | + `market_lifecycle_v2` events |
| Books | **only approved groups whose game is live or starts within 30 min** (`M1 cap: 8 groups`). One batched Kalshi call per cycle plus one Polymarket US call per market, all issued **concurrently**. Cadence = `max(1 s, n_poly / 4)`. Pre-game approved groups are sampled every 60 s, and **discovery samples never open episodes**. | WS streams; REST only as a labelled fallback |
| Freshness timer | every 250 ms: expire stale books even when nothing arrives | same |
| Evaluator | on each book update for a group member, plus every timer tick: both directions × size views (1, 10, 100) | same |
| Recorder | single-writer queue: SQLite batch commit every 500 ms with `PRAGMA wal_checkpoint(PASSIVE)` every 60 s, plus raw-log append | same |
| Health | heartbeat row every 10 s: queue depth, last book per venue, error/429 counts, disk free | + per-connection state |

**Backpressure:** if the recorder queue exceeds its limit, mark all books invalid (`recording_backpressure`), close open episodes, and drop to the slowest cadence until the queue drains. A gap is never silently skipped. If disk free < 2 GB: stop raw logging, keep summaries, raise a health alarm.

**Shutdown and cancellation:** SIGINT/SIGTERM → stop the pollers → drain the writer queue → close episodes with `collector_shutdown` → flush and fsync → exit. `asyncio.CancelledError` is always re-raised, never swallowed by a broad `except`. Each loop's supervisor restarts it with backoff on any `Exception`.

**Why M1 results are indicative only.** With REST polling, the two legs are fetched roughly 50–300 ms apart, so in-game books can disagree just because they were sampled at different moments. Polled assessments therefore carry `books_atomic = false` and are **never `qualified`**. They are stored and shown as **indicative**, with skew in ms, both directions side by side, and the size at the touch. Episodes built from them are **indicative episodes** with sampled durations. Qualified results start in M2, from streamed books with both legs fresh and skew under 250 ms.

### 6.2 Freshness rules (defaults)

- A book is fresh only if all of these hold:
  - both clocks agree it is recent: `now_mono − received_mono ≤ max_age` **and** `now_wall − received_wall ≤ max_age`, where `max_age` is 2.0 s for streams and `2 × cadence` for polling. Checking both clocks catches cases where `time.monotonic()` is paused during sleep.
  - it came from a complete snapshot on the current connection generation
  - it is not `cached`
- **Clock jump guard:** if `wall − mono` moves by more than 1 s between timer ticks (sleep/wake, NTP step), invalidate every book and close episodes with `clock_jump`.
- Skew between legs ≤ 1.0 s for an indicative result, and ≤ 250 ms (both streamed) for a qualified one.
- The book goes invalid immediately on any of: a disconnect, a parse failure, an impossible book (crossed or negative), a sequence gap, the maintenance window, or backpressure.
- Heartbeats never refresh book age.
- **Dust rule:** a leg with less than `min_touch_qty = 10` contracts at the best level is marked `dust`. It is shown but never ranked.

### 6.3 Episodes

There is one state machine per `(group, yes_leg, no_leg, qty, model_version)`:
- **`CLOSED → OPEN`** on the first qualified assessment.
- **Stays `OPEN`** while every evaluation is qualified and no gap exceeds 2× the cadence.
- **`OPEN → CLOSED`** with one of these reasons: `disqualified:<reason>`, `data_gap`, `collector_restart`, `collector_shutdown`, `clock_jump`, `maintenance`, `rules_changed`, `market_closed`.
- Indicative (polled) and qualified (streamed) episodes are tracked separately and never merged.

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
review_log(id INTEGER PRIMARY KEY, group_id TEXT, action TEXT, reviewer TEXT, shown_hashes_json TEXT,
           result TEXT CHECK(result IN ('applied','stale_review','invalid')), spool_file TEXT, applied_utc TEXT);
assessments(id INTEGER PRIMARY KEY, group_id TEXT, yes_market_key TEXT, no_market_key TEXT, qty TEXT,
            qualified INTEGER, reasons_json TEXT, gross_edge TEXT, net_profit TEXT, roc TEXT, capital TEXT,
            fee_yes TEXT, fee_no TEXT, fee_provenance_json TEXT, avg_yes TEXT, avg_no TEXT,
            max_profitable_qty TEXT, age_yes_ms INTEGER, age_no_ms INTEGER, skew_ms INTEGER,
            books_atomic INTEGER, class TEXT CHECK(class IN ('qualified','indicative','rejected')),
            exception_risk INTEGER, model_version TEXT, computed_utc TEXT);
episodes(id INTEGER PRIMARY KEY, group_id TEXT, yes_market_key TEXT, no_market_key TEXT, qty TEXT,
         class TEXT, model_version TEXT, first_seen_utc TEXT, last_seen_utc TEXT, observations INTEGER,
         peak_net TEXT, min_size TEXT, max_size TEXT, termination_reason TEXT);
collector_events(id INTEGER PRIMARY KEY, utc TEXT, venue TEXT, kind TEXT, detail_json TEXT);
```

Money and size columns are TEXT, holding exact Decimal strings. To bound volume, an assessment row is stored only when the **class or the reason set changes**, or when net profit crosses a ±$0.05 band, plus one heartbeat sample per group/direction per minute. Raw books stay in the raw log, not SQLite.

**Raw log:** `data/raw/<venue>/<YYYY-MM-DD>/<HH>.jsonl.gz`, written as **one gzip member per record** with flush + fsync per batch (≤ 500 ms). On startup, a torn tail (bad CRC) is truncated to the last good member and logged as a `collector_events` gap. Each envelope is `{recv_utc, recv_mono_ns, venue, kind (rest_book|ws_msg|market|conn|checkpoint), conn_gen, endpoint, status, cache_headers, body}`. Every received book and WS message goes into it, so it can be replayed. Checkpoints (the full normalized book for each watched market every 60 s) go into a separate `checkpoints/` stream. Retention: raw data 30 days **and** a size cap (default 20 GB, oldest hour deleted first), summaries indefinitely. Expected volume for M1 (8 groups): roughly 1–2 GB/day compressed. PR-8 measures it.

## 7. Review app (Streamlit, bound to 127.0.0.1)

1. **Candidates:** the generator's queue, with the match keys and why each pair was proposed.
2. **Review:** side-by-side rules and the exception table. Actions: approve, reject, or mark exception-risk, with notes.
3. **Live:** results ranked by net $ at the requested size, then ROC, in separate tabs for **Qualified** (M2+) and **Indicative** (polled). Each row shows both legs, both directions' sums, prices, fees with provenance (marked "conservative bound" where applicable), book ages, skew in ms, size at the touch, and the `exception_risk` and `dust` flags. A banner states that indicative results are not tradable evidence. A further filterable tab lists rejected results with all their reasons.
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
- **No order path.** A test walks the import graph of `pmarb.cli scan|review|replay` and fails on any module under `pmarb.execution`. It also scans every collector module's source and bytecode constants for order endpoints: `/portfolio/events/orders`, `/portfolio/orders`, `/v1/orders`, `orders.create`, `ORDER_INTENT_`. At runtime, the HTTP client used by observation commands refuses any non-GET request except the documented WS subscribe frames.
- **Keys.** One account and one key per person. Kalshi offers a `read` scope, so use it. **Polymarket US documents a single key that can also trade** (whether a read-only key exists is UNVERIFIED). Treat that key as a trading credential: keep it in its own file, give it its own Pane secret, and never mount it on a shared machine. Keys are collected through Pane's secrets prompt, never pasted into chat, prompts, or files.
- **Outbound fetch allowlist.** The collector only contacts the venue API hosts (plus `assets.kalshi.com` for terms PDFs). Redirects to other hosts are refused and response bodies are capped.
- **Public repo.** Code and synthetic fixtures only. Real data stays in gitignored `data/`. No live values in docs. No Kalshi or Polymarket branding or marketing claims. gitleaks runs in pre-commit and in CI over the full history.
- **Network hygiene.** HTTPS/WSS only, with TLS verification on. A descriptive User-Agent with no personal data.

## 9. Testing

| Tier | Where | Runs in CI | What |
|---|---|---|---|
| Unit | `tests/` | yes | arithmetic, fees, ladders, normalization, parsing, state machines |
| Fixture | `tests/fixtures/` (synthetic, committed) | yes | adapter parsing against hand-built payloads in the verified formats |
| Simulated network | `tests/` with `httpx.MockTransport` (M1) and a fake WS server (M2) | yes | 429/503 backoff, timeouts, maintenance window, schema drift, clock jump; seq gaps and reconnect generations in M2 |
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
- A **normal-outcome** mismatch (OT/shootout/source) is rejected.
- Θ = `None` keeps results scenario-only.
- Kalshi fees match docs.kalshi.com `fee_rounding`'s official examples (including the non-direct $0.00363825 case). Until that algorithm lands, the conservative bound is ≥ the documented fee.
- The Polymarket US assessment fee equals the documented order cap. A two-fill example shows the actual charge ≤ the cap.
- `max_profitable_qty` finds a profitable q that lies past an unprofitable smaller q (the rounding non-monotonicity case).
- A group never pairs YES "A wins" with NO "B wins".
- Polled assessments are never `qualified`.
- `Decimal(str)` parsing of JSON-number fields (`orderPriceMinTickSize`) yields exact values.
- Clock jump → all books invalid.
- Maintenance computed in America/New_York across a DST change.
- Torn gzip tail recovery.
- A stale review decision is rejected.
- The maintenance window makes the book invalid with reason `maintenance`.
- A restart closes episodes with `collector_restart`.
- The order-path guard, including a synthetic module that posts to `/v1/orders`.

## 10. Milestones and pull requests

Each task is built by a Grok worker in its own git worktree, so parallel tasks don't collide. The orchestrator reviews it (reads the diff and runs the tests), Haiku spot-checks where useful, and then it is pushed as a PR for Rishi to review. Sizes are in agent time: S < ½ day, M ≈ 1 day, L ≈ 2 days.

### M1 — keyless observer (target: 24 h clean run)

| PR | Tasks | Size | Depends on | Parallel with |
|---|---|---|---|---|
| **PR-0 Contracts** | Freeze `models.py` (Market, Leg, EquivalenceGroup, Assessment with `class`, Episode) and the §6.4 DDL as a migration file, plus the `FeeModel`/`VenueAdapter` protocols. No behaviour. Small, so everything after it can run in parallel without colliding. | S | — | — |
| **PR-1 Economics + fees** | `economics.py` (one-outcome groups, both directions, full-predicate `max_profitable_qty`); `fees/kalshi.py` (fee_rounding doc algorithm + official examples, multiplier from series, precision config); `fees/polymarket_us.py` (cap as conservative bound); migrate the 39 tests | M | PR-0 | PR-2 |
| **PR-2 Foundations** | `config.py`, `secrets.py`, `logging.py` (redaction), `storage/db.py` (single-writer queue, WAL checkpoints), `storage/rawlog.py` (per-record gzip members, fsync, torn-tail repair, size cap), **shared `RateLimiter` + backoff + HTTP client with host allowlist and GET-only guard** | M | PR-0 | PR-1 |
| **PR-3 Kalshi REST adapter** | series-scoped discovery, market + rules capture, batched books with cache flags; synthetic fixtures; measure the batch call's token cost | M | PR-1, PR-2 | PR-4 |
| **PR-4 Polymarket US REST adapter** | league-scoped discovery (confirm `/v2/leagues/{slug}/events` in docs), slug detail, field pinning, book parse, DST-correct maintenance calendar; synthetic fixtures | M | PR-1, PR-2 | PR-3 |
| **PR-5a Rules + review store** | rules hash v1, revocation, stale-review guard, exception table, spool-dir review intake | M | PR-0, PR-2 | PR-3/4 |
| **PR-5b Matching (NFL first)** | NFL alias table + candidate generator + polarity; then NBA/NHL/NCAA tables as small follow-up PRs | M | PR-3, PR-4, PR-5a | PR-6a |
| **PR-6a Poller + freshness** | supervisor, live-game polling scheduler, BookStore, dual-clock freshness, clock-jump guard, maintenance gaps, backpressure, shutdown | M | PR-2, PR-3, PR-4 | PR-5b |
| **PR-6b Evaluator + episodes** | evaluator wiring, indicative/qualified classes, dust rule, episode state machine, health rows | M | PR-1, PR-6a | — |
| **PR-7 Review app + CLI** | Streamlit pages §7 (read-only DB, spool writes), `pmarb scan`, `pmarb review`, `pmarb doctor` (config/keys/data-dir/disk checks) | M | PR-5b, PR-6b | — |
| **PR-8 Soak report** | 24 h run during a live slate: uptime, gaps, 429s, kill/restart and sleep/wake tests, data volume, indicative gap statistics during live games (size vs skew); fixes | S–M | PR-7 | — |

**Your task, in parallel (gates M2): accounts.** Each of you opens your own Kalshi and Polymarket US account and creates a read-scoped API key.
- **Polymarket US:** install the iOS app, complete identity verification, then create the key at polymarket.us/developer.
- **Kalshi:** create the key in account settings with the `read` scope.
- **Record two things:** whether your Kalshi account is a direct member (this sets fee precision), and your Polymarket US Θ tier.

### M2 — streaming (needs keys)

- **Auth:** signing modules for both venues, tested against known vectors.
- **Kalshi WS:** snapshot, delta, `seq`, `get_snapshot` recovery, lifecycle events.
- **Polymarket US WS:** snapshot replace, connection generations, schema pinning.
- **Verification:** capture real frames to confirm depth N and field casing. **Polymarket US stream results stay non-qualified until depth N is recorded.** Add forced-disconnect integration tests, then a 7-day stability run.
- **Qualified class:** turned on only here, from streamed books on both legs with skew under 250 ms.
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
| Horizon | game start in [now − 12 h, now + 7 d]; watched while still `active`/open |
| Polled groups (M1) | ≤ 8, live or starting within 30 min; others sampled every 60 s |
| Poll cadence | `max(1 s, n_poly / 4)`, all legs concurrent |
| Freshness | streams 2.0 s; polling 2 × cadence; both monotonic and wall clock |
| Skew | indicative ≤ 1.0 s; qualified ≤ 250 ms (streamed) |
| Size views | 1, 10, 100 contracts (requested = 100) |
| Dust | best level < 10 contracts → shown, not ranked |
| Qualification (M2+) | streamed, all checks pass, net > 0, net ≥ $0.50, ROC ≥ 0.5% at the requested size |
| Kalshi fee | 0.07 × series multiplier (PDF 2026-07-07), fee_rounding algorithm; precision config (unset → scenario) |
| Polymarket US fee | Θ config (unset → scenario); order cap used as a conservative bound |
| Rate budgets | Kalshi books 3 req/s (batched) + discovery 1 req/s; Polymarket US books 4 req/s + discovery 1 req/s |
| HTTP timeouts | connect 3 s, read 5 s |
| Raw log cap | 30 days and 20 GB, oldest first |
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
| Q6 | ~~Kalshi batch books~~ **Resolved:** `GET /markets/orderbooks`, repeated `tickers`, max 100, no auth, default 10 tokens. | — | done |
| Q7 | Is every Polymarket US team market "first team = long"? | Always use `marketSides[].long`; log any disagreement with slug order | PR-4 |
| Q10 | Does a Polymarket US read-only key exist? Does the Participant Agreement §I.19 "written production approval" apply to retail API automation? Does §I.22 limit data redistribution? | Read the agreement; ask support before M5 | before M5 (data stays private regardless) |
| Q11 | ~~League events endpoint~~ **Resolved:** documented and live; `/v2/leagues` is paginated. | — | done |
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
| D25 | Storage stays local (SQLite + compressed files) through M1. Cloud sync is deferred to M2/M3: summary tables to a managed Postgres, raw logs to private object storage. All storage access goes through `storage/` interfaces so the switch is cheap. | active |

## 13. Changes in v1.1 (from review)

An adversarial review (Grok) and a cross-check against an independent draft (Haiku diff) found these problems in v1. All were fixed above:

1. **The rate budget was exhausted by polling, and discovery crawled whole catalogs.** Discovery is now series/league-scoped, Kalshi books are batched, and discovery and books have separate budgets.
2. **Non-atomic polled books could qualify, and the horizon excluded in-game markets.** Polled results are now indicative only, the horizon includes games that started up to 12 h ago, and M1 focuses on live games.
3. **The fee rounding was oversimplified.** Kalshi now uses the documented fee_rounding algorithm with its official examples. For Polymarket US, the order cap is used as a conservative bound.
4. **`max_profitable_qty` assumed monotone profit.** It now checks every quantity against the full qualification test.
5. **Cartesian pairing could buy the same bet twice.** Groups now hold exactly one outcome.
6. **Matching used incorrect fields.** It now uses both market-type fields, event `startTime` falling back to `gameStartTime`, and the NY rules date, and it parses notional and tick as Decimal. (A follow-up check found the reviewer's claim that `gameStartTime` and `sportsMarketTypeV2` are deprecated to be unconfirmed: both are present live.)
7. **Collector failure modes were missing.** Added dual-clock freshness, a clock-jump guard, DST-correct maintenance, crash-safe gzip, write volume and disk caps, cancellation handling, and timeouts.
8. **Two processes wrote to SQLite.** The review app is now read-only and writes decisions to a spool directory.
9. **The PR plan was too coarse.** Added PR-0 contracts and split PR-5 and PR-6. The RateLimiter now has an owner (PR-2). The terms PDF is no longer hashed.
10. **The order-path guard missed `/v1/orders`.** It now covers it, plus a GET-only client and a host allowlist.
11. **Other additions:** a dust rule, the stale-review guard, the meaning of exception risk (labels, does not block), a rules-hash version, the Polymarket US key treated as a trading credential, and Kalshi delta-to-zero handling.

Reviewer claim not adopted: "the Kalshi 0.07 coefficient is unverified". The fee schedule PDF (effective 2026-07-07) was read directly in a browser on 2026-11-11. The coefficient stays, with that provenance recorded.
