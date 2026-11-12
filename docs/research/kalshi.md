# Kalshi API research (observation, then later execution)

> Live API values in examples were replaced with synthetic values; formats and field names are accurate.

**Fetched:** 2026-11-12 (HTTP `date` on live responses, for example `Thu, 01 Jan 2026 00:00:00 GMT`).
**Spec revision read:** OpenAPI `info.version` **3.36.0** and the matching AsyncAPI download.
**Method:** `llms.txt` / `llms-full.txt`, the Markdown docs, `openapi.yaml`, `asyncapi.yaml`, unauthenticated `GET`s against production and demo, PyPI metadata, and the official sync wheel. User-Agent `pmarb-research/0.1`. No account, no orders.
**Confidence labels:** **VERIFIED** means read in a primary doc downloaded this session or observed on a live response. **INFERRED** means a mechanical reading of those sources. **UNKNOWN** means the source was missing, blocked, or silent.

This report is about the **Predictions / event-contract** Trade API (`/trade-api/v2`). Perps (margin), FIX, and CF Benchmarks / Pyth feeds are separate products. Do not point the collector at `margin-ws`, `/margin/*`, or `/trade-api/v2/external-feeds`.

Raw pages are in `sources/`. Live response excerpts are in `sources/live/`.

---

## Where `plan.md` is wrong, outdated, or missing

| Plan claim | Status | What the current sources say |
|---|---|---|
| REST books are YES/NO bids; YES ask = `1 −` best NO bid | **VERIFIED for REST**, and for WebSocket only when `use_yes_price` is false | e.g. on `KXNBAGAME-26NOV11CLENYK-CLE`: best NO bid `0.6000` and market `yes_ask_dollars` `0.4000`. With `use_yes_price: true`, that same NO level is sent as `0.4000` on the YES scale. Complementing it again yields the NO price, not the YES ask. |
| Prefer `use_yes_price=true` and do not complement twice | **VERIFIED as the right future default; the flag still defaults to false** | Set it explicitly. The docs say the default will flip to `true` later, then the flag will be removed. No date is published. |
| Sequence gap: invalidate, fetch a fresh snapshot, do not treat a repeated `subscribe` as a resync | **VERIFIED**, and more specific than the plan | Recovery command is `update_subscription` action `get_snapshot` for every subscribed ticker, or `unsubscribe`, wait for `unsubscribed`, then `subscribe`. `seq` is per subscription. `clock` is a per-market exchange clock and is not a gap detector. |
| Channel name `market_lifecycle` | **WRONG name** | The channel is `market_lifecycle_v2`. Combos use `multivariate_market_lifecycle`. Neither accepts a market filter. |
| Basic read budget 200 tokens/s, most calls cost 10 | **VERIFIED and incomplete** | Seven prediction tiers. Separate read, write, and balance buckets. Write budgets, per-shard write billing, burst capacity, and WebSocket connection caps are all specified. Authenticated `GET /account/endpoint_costs` is not required to learn non-default costs: that endpoint answered **without** a key. |
| Standard taker fee `0.07 × multiplier × q × p × (1−p)` | **NOT re-verified** | The schedule PDF and `https://kalshi.com/fee-schedule` returned HTTP 429 (Vercel checkpoint), so the coefficient was not read. Series metadata does name the fee *structure* and maker multipliers `0.25` and `0.5`. |
| Worked example fee `$1.7325` under direct-member rounding | **Consistent with the rounding doc if the model fee is exactly `$1.7325`** | Direct-member `$0.0001` alignment adds `$0` of rounding on that input. The same input under non-direct `$0.01` alignment adds `$0.0075` of rounding fee. Account type is still unknown until a key exists. |
| Kalshi may not have a usable read-only key | **OUTDATED** | Scope `read` grants all read endpoints. Broad `write` cannot be granted unless `read` is included. Omitting `scopes` still means full access. Whether a `read` key may open the WebSocket is **UNKNOWN**. |
| Discovery horizon from expected resolution, using close-time filters | **MISSING an API limitation** | There is no `expected_expiration_time` query. `min_close_ts` / `max_close_ts` are a different clock. Live soccer market `KXESOCCERGAME-26NOV112337EDERFEDE-TIE`: e.g. expected expiration `2026-01-02T00:00:00Z`, close `2026-01-16T00:00:00Z`. A seven-day **close** window drops it. |
| Integer cent prices and whole contracts | **OUTDATED** | Live books and trades use fixed-point dollar strings and fixed-point counts. A public trade on `KXNBAGAME-26NOV11CLENYK-CLE` had a fractional `count_fp` such as `"12.34"`. `fractional_trading_enabled` is gone from responses. |
| Official Python SDK as a possible client | **MISSING constraints** | `kalshi_python_sync` / `kalshi_python_async` **3.36.0** (uploaded 2026-11-11) require **Python ≥ 3.13**, are OpenAPI REST clients, and have **no WebSocket client**. License `LicenseRef-Proprietary`. The plan’s runtime is Python 3.12. |
| Public market `GET` is fresh enough to police a two-second book | **MISSING cache behavior** | `GET /markets?status=open` returned `cache-control: public, max-age=15`. Single-market `GET`s returned `x-kalshi-cache-hits: 1` and no `Cache-Control`. Back-to-back with the order book, prices matched and **sizes did not**. |
| `GET /series` cursor pagination at `limit` 100 | **WRONG as observed** | `GET /series?limit=1` returned **16,231** series, no `cursor` key. |
| Quick-start `status=all` | **WRONG** | Live `status=all` → HTTP 400 `invalid status filter`. |
| One shared “order book parser” for REST and WebSocket | **MISSING a field-name split** | REST: `orderbook_fp.yes_dollars` / `no_dollars`. WebSocket snapshot: `msg.yes_dollars_fp` / `no_dollars_fp`. |

The plan’s core shape is still right: public REST for rules and metadata, authenticated WebSocket for the book, complement bids into asks exactly once, `Decimal`, sequence-gap rebuild, series fee metadata, and no order client in the observation process.

---

## 1. Environments, versioning, docs index

**VERIFIED** from [API Environments](https://docs.kalshi.com/getting_started/api_environments) and [Demo](https://docs.kalshi.com/getting_started/demo_env). Live `GET /exchange/status` returned HTTP 200 on all four REST hosts below.

| Surface | Production (use this) | Production, still accepted | Demo (use this) | Demo, still accepted |
|---|---|---|---|---|
| REST | `https://external-api.kalshi.com/trade-api/v2` | `https://api.elections.kalshi.com/trade-api/v2` | `https://external-api.demo.kalshi.co/trade-api/v2` | `https://demo-api.kalshi.co/trade-api/v2` |
| WebSocket | `wss://external-api-ws.kalshi.com/trade-api/ws/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` | `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |

- The `elections` host serves **all** event-contract markets. **VERIFIED** in the environments page and by the live NBA/soccer responses on `external-api.kalshi.com`.
- Demo and production credentials are not interchangeable. Demo markets are not a price source. **VERIFIED.**
- URL version is the path segment `v2`. OpenAPI `3.36.0` is the spec revision, not a second URL version. There is no `/trade-api/v3` in the index. **VERIFIED.**
- Docs index: `https://docs.kalshi.com/llms.txt` (302 links this fetch) and `https://docs.kalshi.com/llms-full.txt` (HTTP 200, 629,251 bytes). Specs linked from the index: `/openapi.yaml`, `/asyncapi.yaml`, plus separate perps specs. **VERIFIED.**
- Signing path does not include the host or the query string. The same path is signed for every host. **VERIFIED.**
- Lower-latency private stream, **only** `fill` and `user_orders`: production `wss://warp-ws.kalshi.com/trade-api/ws/v2`, demo `wss://warp-ws.demo.kalshi.co/trade-api/ws/v2`. Any other channel on that host returns WebSocket error code 8. Sign the same path `/trade-api/ws/v2`. **VERIFIED** in the environments page. Not connected this session.
- External index feeds (CF Benchmarks, Pyth) are moving to `wss://external-feeds.kalshi.com/trade-api/v2/external-feeds`. Out of scope for the book. A weather-probability feed on that family has an explicit license: use only for quoting on Kalshi, do not redistribute or store for another use (`llms-full.txt` weather section). That sentence is **not** a general order-book license.
- PrivateLink for `external-api.kalshi.com` and `external-api-ws.kalshi.com` is Premier and above. VPC peering is Prime and above. **VERIFIED.** Not needed for a local collector.
- Live production exchange status: `trading_active: true`. Shards observed: `0` Default, `1` Combos, `2` Crypto & Commodities, `3` Tennis, Baseball, Basketball. `KXNBAGAME-26NOV11CLENYK-CLE` and the settled college-basketball total were `exchange_index` **3**. The esoccer tie market was `exchange_index` **0**. **VERIFIED** live. Shard membership can change; read `exchange_index` from the market, do not hard-code categories.

---

## 2. Authentication

Sources: [API Keys](https://docs.kalshi.com/getting_started/api_keys), [Quick Start: Authenticated Requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests), OpenAPI `CreateApiKeyRequest` / `ApiKeyScope`.

### Creating a key

- UI: Account & security → API Keys, or `https://kalshi.com/account/profile`. Ed25519 is the default. The private key is shown once and downloaded as a PEM text file. The key id looks like `a952bcbe-ec3b-4b5b-b8f9-11dae589608c`. **VERIFIED** in the docs. UI tier gate: **UNKNOWN** (the UI section does not repeat the API tier rule below).
- `POST /trade-api/v2/api_keys` registers a PEM public key you generate. The description limits this to **Premier or Market Maker** usage levels. RSA-PSS SHA-256 or Ed25519. **VERIFIED.**
- `POST /trade-api/v2/api_keys/generate` mints a key pair. `key_type` omitted means RSA. The private key is returned once. **VERIFIED.**
- Own-key generation commands in the docs: `openssl genpkey -algorithm ed25519` or `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048`. **VERIFIED.**
- PKCS#8 RSA and Ed25519 keys share the same generic PKCS#8 PEM header. Detect type from the parsed key, not the header. **VERIFIED.**

### Signing string and headers

**VERIFIED.** For every REST call and for the WebSocket handshake:

```text
message = timestamp_ms + METHOD + path_without_query
```

No separators. `METHOD` is uppercase (`GET`). `timestamp_ms` is Unix epoch milliseconds as a decimal string. `path_without_query` starts at the API root.

| Call | Path that is signed |
|---|---|
| Balance | `/trade-api/v2/portfolio/balance` |
| Order create v2 | `/trade-api/v2/portfolio/events/orders` |
| WebSocket handshake | `/trade-api/ws/v2` |

Headers, same names for REST and the WebSocket upgrade:

- `KALSHI-ACCESS-KEY`: key id
- `KALSHI-ACCESS-TIMESTAMP`: the same millisecond string
- `KALSHI-ACCESS-SIGNATURE`: base64 of the signature

Ed25519 signs the raw message (RFC 8032). RSA is RSA-PSS, MGF1-SHA-256, salt length equal to the SHA-256 digest length, hash SHA-256. Kalshi picks the verifier from the registered public key. **VERIFIED.**

A live WebSocket upgrade to `wss://external-api-ws.kalshi.com/trade-api/ws/v2` **without** those headers returned HTTP **401** `{"code":"token_authentication_failure","message":"token authentication failure"}`. **VERIFIED.**

### Minimal signer (matches the docs)

```python
import base64
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def load_private_key(path: str):
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_request(private_key, timestamp_ms: str, method: str, path: str) -> str:
    path_without_query = path.split("?", 1)[0]
    message = f"{timestamp_ms}{method}{path_without_query}".encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode("utf-8")


def auth_headers(private_key, key_id: str, method: str, path: str) -> dict[str, str]:
    timestamp_ms = str(int(time.time() * 1000))
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sign_request(private_key, timestamp_ms, method, path),
    }
```

If `BASE_URL` already ends in `/trade-api/v2`, the request path passed to the HTTP client is `/portfolio/balance`, while the signed path is `/trade-api/v2/portfolio/balance`. The docs call out double-prefixing as a 404/signature failure. **VERIFIED.**

### Scopes

**VERIFIED** from `ApiKeyScope`:

| Scope | Meaning |
|---|---|
| `read` | All read endpoints |
| `write` | All write endpoints. If this parent is present, `read` must also be present |
| `read::portfolio_balance` | Balance only |
| `read::block_trade_accept` | View block-trade proposals |
| `write::trade` | Trade writes |
| `write::transfer` | Transfers |
| `write::fcm_risk` | FCM risk controls |
| `write::block_trade_accept` | Accept block trades |

Omitted `scopes` means `read` and `write`. Child scopes can be granted without the parent. A subaccount-restricted key (0–63) cannot see other subaccounts. An FCM subtrader-bound key is denied on every REST endpoint, including key management. **VERIFIED.**

**UNKNOWN:** whether a key whose only scope is `read` can complete the WebSocket handshake and subscribe to `orderbook_delta`. The quick start lists `orderbook_delta` among channels that require channel-level auth, because deltas can carry `client_order_id` when **your** order changed the book. `ticker`, `trade`, and `market_lifecycle_v2` need the authenticated connection and no further channel auth. Error code 9 means the channel was not granted.

Observation keys should be created as `scopes: ["read"]` and tested on demo before production. Do not ship a key that can `POST /portfolio/events/orders`.

---

## 3. REST market data

Sources: OpenAPI `Market`, `GetMarkets`, `GetMarketOrderbook`, `Series`, `EventData`; [Market Lifecycle](https://docs.kalshi.com/getting_started/market_lifecycle); [Orderbook Responses](https://docs.kalshi.com/getting_started/orderbook_responses); [Fixed-Point Representation](https://docs.kalshi.com/getting_started/fixed_point_migration) (page says last updated 2026-11-05); [Pagination](https://docs.kalshi.com/getting_started/pagination); [Kalshi Glossary](https://docs.kalshi.com/getting_started/terms). Live calls as noted.

Public, no key, HTTP 200 this session: `GET /exchange/status`, `GET /markets`, `GET /markets/{ticker}`, `GET /markets/{ticker}/orderbook`, `GET /markets/orderbooks`, `GET /events/{event_ticker}`, `GET /series/{series_ticker}`, `GET /series`, `GET /markets/trades`, `GET /series/fee_changes`, `GET /events/fee_changes`, `GET /historical/cutoff`, `GET /account/endpoint_costs`.

The glossary page is named `terms` in the docs path and is **not** the developer agreement. It says a market is “a single binary market,” which is narrower than the API enum `market_type: binary | scalar`. It also says not to parse ticker strings to infer series or event membership; use `series_ticker` and `event_ticker`. **VERIFIED.**

### Status lifecycle

**VERIFIED** in the lifecycle page and the `Market.status` enum.

| REST `status` | Meaning |
|---|---|
| `initialized` | Created, not yet open. Becomes `active` when `open_time` passes. No WebSocket `activated` for that implicit transition |
| `active` | Trading |
| `inactive` | Exchange pause. Not closed. Reactivation cancels resting orders |
| `closed` | Past `close_time`. No new orders. Awaiting determination |
| `determined` | Result is set. `settlement_timer_seconds` is running. Result can still be disputed |
| `disputed` | Result challenged. May be re-determined |
| `amended` | Re-determined. Timer restarts |
| `finalized` | Positions paid. Terminal. This is the REST name for “settled” |

`GET /markets?status=` filter values are **not** the same strings:

| Filter | Matches |
|---|---|
| `unopened` | `initialized` |
| `open` | `active` |
| `paused` | `inactive` |
| `closed` | Past `close_time` and not yet `finalized` |
| `settled` | `finalized` |

Only one `status` filter. `status=all` is invalid (live HTTP 400). **VERIFIED.**

`GET /events?status=` uses `unopened`, `open`, `closed`, `settled` and matches an event if **any** child market matches. An event has no single status field. **VERIFIED.**

### Time fields

**VERIFIED** in the lifecycle page and on live markets.

| Field | Meaning |
|---|---|
| `open_time` | Trading start |
| `close_time` | Trading stop. Can move earlier when `can_close_early` is true. WebSocket `close_date_updated` |
| `expected_expiration_time` | When the outcome is expected to be known. Nullable. Can be **before** `close_time` |
| `latest_expiration_time` | Latest possible expiration |
| `expiration_time` | Deprecated. Docs say prefer `latest_expiration_time` |
| `occurrence_datetime` | Recorded time the underlying occurred, if any |
| `settlement_timer_seconds` | Seconds **after determination** before payout, not after `close_time` |
| `settlement_ts` | Present once settled |
| `early_close_condition` | Text, when the market can close early |

Live illustrations:

- `KXESOCCERGAME-26NOV112337EDERFEDE-TIE`: e.g. `expected_expiration_time` `2026-01-02T00:00:00Z`, `close_time` `2026-01-16T00:00:00Z`, `can_close_early` true, `settlement_timer_seconds` **180**.
- `KXNBAGAME-26NOV11CLENYK-CLE`: e.g. expected `2026-01-03T00:00:00Z`, close `2026-01-05T00:00:00Z`, timer **45**, early-close text “after a winner is declared”.
- Settled `KXNCAAMBTOTAL-26NOV11COFCTEM-184`: e.g. closed early at `2026-01-01T00:00:03Z`, `settlement_ts` `2026-01-01T00:04:06.20922Z`, while `expected_expiration_time` was still `2026-01-01T04:00:00Z`. Payout was about four minutes after the early close, not at the expected expiration. **VERIFIED.**

### Close-time filters

**VERIFIED** in OpenAPI `GetMarkets` description.

| Timestamp filters | Status values the table allows with them |
|---|---|
| `min_created_ts`, `max_created_ts` | `unopened`, `open`, or empty |
| `min_close_ts`, `max_close_ts` | `closed`, or empty |
| `min_settled_ts`, `max_settled_ts` | `settled`, or empty |
| `min_updated_ts`, `max_updated_ts` | empty only, plus `mve_filter=exclude`. `series_ticker` is allowed and then **requires** `mve_filter=exclude` |

Timestamps are Unix **seconds**. `min_updated_ts` tracks non-trading metadata changes, not trades.

The published table does not list `status=open` with `min_close_ts`/`max_close_ts`. A live request `status=open` plus a seven-day close window returned HTTP 200 and an `active` market inside that window (`KXAUDUSDEVENT-18NOV262030-T07120`, close e.g. `2026-01-06T01:30:00Z`). **VERIFIED** for that one call. Treat the compatibility table as incomplete relative to that observation, and still do not use close time as expected resolution.

There is **no** server filter on `expected_expiration_time`. **VERIFIED** by the parameter list.

Other list filters: `series_ticker`, single `event_ticker`, comma-separated `tickers`, `mve_filter` `only` | `exclude`. On-request `KXRFQ*` markets are omitted from unscoped `GET /markets` unless the request names their series, event, or ticker (changelog 2026-11-05). **VERIFIED** in the changelog.

### Rules and identity fields to hash

On **market** (required in the schema unless noted): `ticker`, `event_ticker`, `market_type` (`binary` | `scalar`), `yes_sub_title`, `no_sub_title` (deprecated; equal to `yes_sub_title` since September 2024), `rules_primary`, `rules_secondary`, `notional_value_dollars`, `price_level_structure`, `price_ranges`, `settlement_bounds_type` (`default` | `floor`), `settlement_floor_dollars` when floor, `can_close_early`, `early_close_condition`, `strike_type` (`greater`, `greater_or_equal`, `less`, `less_or_equal`, `between`, `functional`, `custom`, `structured`), `floor_strike`, `cap_strike`, `functional_strike`, `custom_strike`, `mve_collection_ticker`, `mve_selected_legs`, `is_provisional`, `exchange_index`, `fee_waiver_expiration_time`.

`title` and `subtitle` are deprecated and still appeared on live markets.

On **series**: `fee_type`, `fee_multiplier`, `settlement_sources[]` (`name`, `url`), `contract_url`, `contract_terms_url`, `additional_prohibitions[]`, `category`, `categories`, `tags`, `frequency`. `contract_terms_url` is the current terms; `contract_url` is the original filing. Live `KXESOCCERGAME`: `fee_type` `quadratic`, `fee_multiplier` `1`, contract PDFs under `assets.kalshi.com`, settlement source DRAFTED.GG. **VERIFIED.**

On **event**: `series_ticker`, `title`, `sub_title`, `collateral_return_type`, `mutually_exclusive`, `settlement_sources`, `category`, `strike_period`, `last_updated_ts`, `exchange_index`. Live `KXNBAGAME-26NOV11CLENYK`: `mutually_exclusive: true`, `collateral_return_type: "MECNET"`, sources ESPN and nba.com. **VERIFIED.**

`rules_secondary` on the settled total-points market says a cancellation or a start delayed more than 48 hours resolves “to a fair price.” That is not a clean $1 / $0 complement. **VERIFIED** in `sources/live/settled1.json`. The plan’s exception rule should fail the pair on that sentence.

### Prices and sizes

**VERIFIED** in the fixed-point page, the Market schema, and live bodies.

- Prices are `*_dollars` strings, up to 4 decimal places (`"0.4100"`). Integer cent fields (`yes_bid`, `last_price`, …) were removed from the schema (changelog release date 2026-01-15) and were absent on live markets.
- Counts are `*_fp` strings. Requests accept 0–2 decimal places. Responses always emit 2 (e.g. `"100.50"`). Minimum grain 0.01 contract. If both integer `count` and `count_fp` are sent, they must match.
- `notional_value_dollars` is the contract payout value. Live binaries were `"1.0000"`.
- Top of book on the market object: `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars`, `yes_bid_size_fp`, `yes_ask_size_fp`, `last_price_dollars`, previous-day counterparts, `volume_fp`, `volume_24h_fp`, `open_interest_fp`.
- Valid prices come from `price_ranges[]` of `{start, end, step}` in dollars. `price_level_structure` is a label only. Documented labels include `linear_cent` (whole cent), `deci_cent`, tapered and center/edge structures down to `$0.0001`. Whole cents are valid on every structure. Combos are documented as `center_deci_edge_centi_cent` after the 2026-09-03 migration. Live NBA, esoccer, and the settled total were all `linear_cent` with step `0.0100` from `0.0000` to `1.0000`.
- Changelog 2026-04-17 and 2026-07-09: `fractional_trading_enabled` no longer carries information and is removed from responses. Treat every active market as able to trade fractional contracts. A live trade `count_fp` such as `"12.34"` and open interest such as `"1000.00"` confirm fractional sizes are live. **VERIFIED.**

### Order book

`GET /markets/{ticker}/orderbook` and `GET /markets/orderbooks` (1–100 tickers). No auth required for a successful read (live 200). The OpenAPI response list includes 401; an unauthenticated call did not produce it.

**VERIFIED** shape (synthetic, truncated):

```json
{"orderbook_fp": {"yes_dollars": [["0.4000", "100.00"], ["0.5000", "200.50"]],
                  "no_dollars":  [["0.4000", "150.00"], ["0.4500", "300.25"]]}}
```

Each level is `[price_dollars, count_fp]`, both strings. The prose still says “order counts”; the schema and the live body are a 2-tuple. There is no separate order-count element.

`depth`: integer 0–100, default 0. `0` means every level. `101` returned HTTP 400 (`Depth` failed `max`). **VERIFIED.**

Sort order: the getting-started page says **ascending price, best bid last**. OpenAPI prose says “best to worst.” Live `KXNBAGAME` book (40 YES levels, 59 NO levels) was strictly ascending. `depth=3` kept the **three highest** prices, still ascending, best last (e.g. `yes` tail `0.5000`, `no` tail `0.4500` on the earlier snapshot). **VERIFIED.** Implement “best = last element,” which matches the getting-started page and the live book. The OpenAPI sentence does not describe that array order.

Complement, REST and default WebSocket scale:

- YES ask price = `1 −` best NO bid price, size = that NO bid’s count
- NO ask price = `1 −` best YES bid price, size = that YES bid’s count

On the paired fetch, e.g. `yes_ask_dollars` `0.4000` equaled `1 − 0.6000`. **VERIFIED.**

Sizes did **not** match on that same pair of calls: e.g. market `yes_bid_size_fp` `100.00` versus book `150.50`; market `yes_ask_size_fp` `200.00` versus best NO size `250.25`. The market response had `x-kalshi-cache-hits: 1`. The order book response had no cache-hit header and no `Cache-Control`. **VERIFIED** observation. **INFERRED:** the market object’s size can be a cached quote and is not the book. Qualify from the order book, not from `yes_bid_size_fp`.

`GET /markets` list responses carried `cache-control: public, max-age=15`. The 2026-11-12 changelog says `GET /markets/{ticker}` and `GET /events/{event_ticker}` send `Cache-Control: public, max-age=5`. Those two responses this session did not include `Cache-Control`. **VERIFIED** both the changelog sentence and the headers that were actually returned.

Empty side: the WebSocket schema says `yes_dollars_fp` / `no_dollars_fp` are omitted when that side has no bids. REST schema marks both arrays required. An empty REST array was not observed.

### Pagination

**VERIFIED** in the pagination guide and OpenAPI, with one live contradiction.

- Cursor token plus `limit`. Markets: `limit` default 100, maximum **1000** (`MarketLimitQuery`). `limit=1001` → HTTP 400. **VERIFIED.**
- Guide text still says “typically 1–100.” That sentence is stale relative to the schema.
- Stop condition is inconsistent across docs: the guide says `cursor` is null; the events schema says empty string when finished. Live market pages returned a non-empty cursor string. Treat missing, `null`, and `""` as the end.
- `GET /series?limit=1` returned 16,231 series and **no** `cursor` field (summary in `sources/live/series_limit1_summary.json`). The pagination guide lists `/series` as cursor-paginated. **VERIFIED** that this call ignored `limit`. Re-check before assuming a page size. A full catalog fetch is about 20 MB.

Historical cutoffs, live `GET /historical/cutoff`: markets and trades `2026-09-12T00:00:00Z`, orders `2026-10-28T00:00:00Z`, positions backfill horizon `2026-09-12T00:00:00Z`. Older rows move to `/historical/*`. Events and series stay on the live endpoints. **VERIFIED.**

---

## 4. WebSocket

Sources: [Quick Start: WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets), [Orderbook Updates](https://docs.kalshi.com/websockets/orderbook-updates), [Connection Keep-Alive](https://docs.kalshi.com/websockets/connection-keep-alive), AsyncAPI `subscribeCommandPayload`, `orderbookSnapshotPayload`, `orderbookDeltaPayload`. The handshake itself was probed; an authenticated session was not opened.

### URL and handshake

Connect to the WebSocket URL in section 1. Headers from section 2. Signed method `GET`, signed path `/trade-api/ws/v2`. **VERIFIED.**

Commands are JSON text frames:

```json
{"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_tickers": ["KXNBAGAME-26NOV11CLENYK-CLE"], "use_yes_price": true, "update_interval_ms": 0}}
```

`id` is a client integer, unique per connection. `0` means “no id.” **VERIFIED.**

`cmd`: `subscribe`, `unsubscribe` (`params.sids`), `update_subscription`.

`update_subscription.params.action` for books and similar channels: `add_markets`, `delete_markets`, `get_snapshot`. Exactly one of `sid` or `sids` (array length 1). Error 12 if that is violated.

### Channels the collector cares about

| Channel | What it is | Market filter |
|---|---|---|
| `orderbook_delta` | Snapshot then deltas. One subscription per connection. Auth required at channel level in the quick start | Required. `market_ticker` or `market_tickers`. Not `market_id` |
| `ticker` | Last, bid, ask, sizes, volume, open interest | Optional. Omit = all markets. `send_initial_snapshot` default false |
| `trade` | Public prints | Optional. Omit = all trades |
| `market_lifecycle_v2` | Lifecycle for every non-MVE market, plus `event_lifecycle` and `event_fee_update` | **Not supported.** Cannot narrow to ten tickers |
| `multivariate_market_lifecycle` | `KXMVE*` only | Not supported |
| `fill` | Your fills | Optional |
| `user_orders` | Your order create/update | Optional `market_tickers` |
| `market_positions` | Your positions | `market_ticker(s)` only |

`KXRFQ*` markets are left out of `ticker` / `trade` unless named, and left out of `market_lifecycle_v2` entirely (changelog 2026-11-05). **VERIFIED.**

### Snapshot and delta

Envelope: `type`, `sid`, `seq`, `msg`, plus `sending_ts_ms` (when Kalshi queued the frame). `id` is present when the snapshot answers a command that had an id.

Snapshot `msg`: `market_ticker`, `market_id`, `clock`, optional `yes_dollars_fp`, optional `no_dollars_fp`. Same `[price, count]` string pairs as REST, **different key names** (`yes_dollars_fp`, not `yes_dollars`). Example in the spec:

```json
{"type":"orderbook_snapshot","sid":2,"seq":2,"msg":{
  "market_ticker":"FED-23DEC-T3.00","market_id":"...","clock":4182057,
  "yes_dollars_fp":[["0.0800","300.00"],["0.2200","333.00"]],
  "no_dollars_fp":[["0.5400","20.00"],["0.5600","146.00"]]}}
```

Delta `msg` required: `market_ticker`, `market_id`, `clock`, `price_dollars`, `delta_fp`, `side` (`yes` | `no`). Optional: `client_order_id` and `subaccount` only when **your** order caused the change, `ts_ms`, deprecated `ts`.

`delta_fp` is documented as a “fixed-point contract delta,” and the example is `"-54.00"`. **INFERRED:** add it to the level for `(side, price_dollars)` and delete the level at zero. The docs do not print that sentence. A fixture test should lock it.

`clock`: exchange clock of the newest change included. Same value on every connection for the same real-time change. Several deltas can share a clock. Clocks skip. Do not compare clocks across markets, or across a maintenance window. A snapshot contains every change with `clock` at or below its own. **VERIFIED.**

### `sid` and `seq`

- `sid`: server subscription id. Required on later `update_subscription` / `unsubscribe`. **VERIFIED.**
- `seq`: per subscription, starting at 1, contiguous. It is the gap detector. **VERIFIED.** `ticker`’s required field list does **not** include `seq`. Do not assume every channel is sequenced the same way.
- Apply order-book messages in `seq` order. On a gap: `get_snapshot` for **all** subscribed markets, or unsubscribe and resubscribe. A second `subscribe` adds markets to the existing `sid` and does **not** resend snapshots for markets already subscribed. **VERIFIED.** This matches the plan and names the command the plan left implicit.

### `use_yes_price`

**VERIFIED** in [Order direction](https://docs.kalshi.com/getting_started/order_direction) and the subscribe schema. Default **false**.

| `use_yes_price` | YES levels | NO levels |
|---|---|---|
| `false` (default, same as REST) | YES bid prices | NO bid prices. YES ask = `1 −` NO price |
| `true` | unchanged YES bid prices | The same NO bids, quoted on the YES scale. A NO bid that would have been `0.30` is sent as `0.70` |

Under `use_yes_price: true`, the NO-side price **is** the implied YES ask. The `side` field stays `no`. Do not subtract from 1. The plan’s sentence “never apply the complement twice” is the right warning; the book builder needs two explicit normalizers.

`bid ≡ yes` and `ask ≡ no` on orders, fills, and trades. That vocabulary is **not** the order-book price scale. A public trade uses `taker_outcome_side` and `taker_book_side`. Legacy `taker_side` / `action` / `side` remain until at least **2026-05-28** and are still on the live trade payload (`taker_side: "yes"` together with `taker_outcome_side: "yes"`, `taker_book_side: "bid"`). **VERIFIED.**

### Conflation, heartbeats, caps

- `update_interval_ms`: `0` (default, every change), `100`, or `1000`. Set only on the original `subscribe`. A mismatch on a later `subscribe` is error 11. Changing it requires unsubscribe then subscribe. **VERIFIED.**
- Conflated mode nets changes per price level. Own-order updates skip the wait. The book can look locked or crossed between flushes. Conflated `clock` values can decrease. Do not dedupe conflated streams by `clock`. **VERIFIED.**
- For a two-second freshness rule, use `0`. A 1000 ms window already spends half the budget before network delay.
- Server sends WebSocket Ping (`0x9`) every **10 seconds** with body `heartbeat`. Client replies with Pong (`0xA`). The Python `websockets` library answers control-frame pings automatically. A heartbeat does not refresh a book timestamp. **VERIFIED** in the keep-alive page. The plan already says this.
- Connection caps (predictions and perps **share** the count): Basic/Advanced/Expert **200**, Premier **500**, Paragon **750**, Prime **1000**, Prestige **1000**. Over the cap, the handshake is HTTP 429. **VERIFIED** in the rate-limits page.
- Changelog **2026-06-18**, not restated on the current rate-limits page: max **500,000** market subscriptions per session, and max **10,000** commands per second. **VERIFIED** as that changelog entry. No later reversal was found in the changelog text. Still subscribe only to the watchlist.
- Error 25: subscription buffer overflow. The client was too slow or the subscription was too wide (all-market `ticker` or `trade`). **VERIFIED.**

`market_lifecycle_v2` event types: `created`, `activated`, `deactivated`, `close_date_updated`, `determined`, `settled`, `metadata_updated`, plus price-level-structure updates carried as documented on that channel, plus `event_lifecycle` and `event_fee_update`. REST `finalized` corresponds to WebSocket `settled`. `created` can 404 on REST for a moment; retry. **VERIFIED.**

---

## 5. Rate limits

Source: [Rate Limits and Tiers](https://docs.kalshi.com/getting_started/rate_limits). Live non-default costs: `GET /trade-api/v2/account/endpoint_costs` (no auth, HTTP 200).

Authenticated requests spend tokens. The tier refills a bucket continuously. A request is allowed when the bucket holds its cost. HTTP 429 body is `{"error":"too many requests"}`. No `Retry-After` and no `X-RateLimit-*` headers. No extra penalty. Back off and retry. **VERIFIED.**

Default cost **10**, confirmed by the live `default_cost` field. Read endpoints for markets, events, series, and order books are **not** in the non-default list, so they cost 10 when the call is authenticated. **VERIFIED** that they are absent from the override list. **INFERRED** that an authenticated call therefore costs 10.

### Prediction tiers (tokens per second)

| Tier | Read | Write | Read bucket | Write bucket |
|---|---:|---:|---|---|
| Basic | 200 | 100 | 3 seconds (600) | 1 second (100) |
| Advanced | 300 | 300 | 3 seconds | 3 seconds |
| Expert | 600 | 600 | 1 second | 3 seconds |
| Premier | 1,200 | 1,200 | 1 second | 3 seconds |
| Paragon | 2,400 | 2,400 | 1 second | 3 seconds |
| Prime | 4,800 | 4,800 | 1 second | 3 seconds |
| Prestige | 12,000 | 9,600 | 1 second | 3 seconds |

**VERIFIED** from the tier table and the burst section. Basic is signup. Advanced is self-serve `POST /account/api_usage_level/upgrade` after at least 1 of the last 100 prediction orders was placed via API (that call costs **30** tokens). Higher tiers are volume grants or manual. Volume share = trailing 30-day personal volume (both sides) / (previous calendar month exchange volume × 2). Earn/keep percents are in the rate-limits page (Expert earn 0.075% / keep 0.05%, up to Prestige 1.00% / 0.80%). Grants last 30 days and are visible on `GET /account/limits` (`usage_tier`, `read`, `write`, `grants[]`). That endpoint is authenticated and was not called.

`GET /portfolio/balance` has its **own** bucket and does not spend the read budget. Basic and Advanced: 1/second. Higher: 5/second. No burst. **VERIFIED.** Do not poll it. Use `fill` / settlements.

Write costs that differ from 10, **VERIFIED** in docs and/or the live cost list:

| Operation | Tokens |
|---|---|
| Create order, and each order inside a v2 batch create | 10 |
| Cancel one order, batch cancel **per order**, cancel-all | 2 |
| `GET /portfolio/orders/{order_id}` | 2 |
| Quote confirm (RFQ) | 1 |
| Perps single cancel / decrease | 1 (perps bucket, not the prediction bucket) |

A batch must fit in the bucket entirely or the whole batch is rejected. Batching does not discount the token price.

Per-shard write billing is documented in two places that do not match:

- Rate-limits page: `exchange_index >= 1` bills **that** shard’s write bucket; `0` bills the unscoped write bucket; auto-route (`-1` or omitted with `market_ticker`) bills **every** shard’s write bucket.
- Sharding page: auto-routed single REST writes bill the **unscoped** bucket **and** every nonzero shard.

**UNKNOWN** which sentence is the one the matcher enforces. For a later sender, pass the market’s `exchange_index` explicitly (the NBA game was `3`) so the order is not auto-routed. Read budgets are not sharded.

**UNKNOWN:** any per-IP limit on unauthenticated public REST. The rate-limits page talks about authenticated tokens. Public GETs succeeded with no key. Do not design the collector to need a key for snapshots, and do not assume anonymous traffic is uncapped.

Sustained Basic read capacity is 200/10 = **20 requests/second**. Ten watched markets plus occasional metadata refreshes fit. A full 16k-series download is one large response, not 16k calls, on the behavior observed today.

---

## 6. Fees

Sources: OpenAPI `FeeType` and `Series.fee_type` / `fee_multiplier`; [Fee Rounding](https://docs.kalshi.com/getting_started/fee_rounding); [Get Series Fee Changes](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes); changelog 2026-08-20 through 2026-08-22. The fee schedule PDF was **not** read.

`https://kalshi.com/docs/kalshi-fee-schedule.pdf` and `https://kalshi.com/fee-schedule` returned HTTP 429 Vercel checkpoint (`sources/kalshi.com_fee-schedule.html`). Help article [Fees](https://help.kalshi.com/en/articles/13823805-fees) (dated 2026-04-19) says the math lives in that schedule and that maker fees are charged only when a resting order **executes**, not when it is canceled. It does not print a formula. **VERIFIED** that the help page says this. The coefficient in `plan.md` (`0.07`) stays **UNKNOWN** until the PDF is fetched from a client that can pass the checkpoint.

### What the API does say

`fee_type` enum, **VERIFIED**:

| Value | OpenAPI description |
|---|---|
| `quadratic` | General trading-fee table. No maker-fee section |
| `quadratic_with_maker_fees` | General table, plus maker fees. Maker multiplier **0.25** |
| `quadratic_with_combo_maker_fees` | Same maker structure, maker multiplier **0.5** instead of 0.25 |
| `flat` | Specific trading-fees table |

`fee_multiplier` is “a floating point multiplier applied to the fee calculations.” **VERIFIED** as that sentence. **INFERRED:** `0` zeroes the calculated trading fee and `0.5` halves it. Qualify from the number that was fetched, not from the inference alone.

Live `GET /series?limit=1` (16,231 series, 2026-11-12):

| `fee_type` | `fee_multiplier` | Series |
|---|---:|---:|
| `quadratic` | 1 | 16,036 |
| `quadratic_with_maker_fees` | 1 | 159 |
| `quadratic` | 0.5 | 18 |
| `quadratic` | 0 | 14 |
| `quadratic_with_combo_maker_fees` | 1 | 3 |
| `quadratic_with_maker_fees` | 0.5 | 1 |
| `flat` | any | **0** |

`KXNBAGAME` is `quadratic_with_maker_fees` × 1. `KXESOCCERGAME` is `quadratic` × 1. `KXBTCY` is `quadratic` × 0 (sample printed while scanning). A taker on an NBA game is **not** the same fee object as a taker on a quadratic series, because the series opted into the maker-fee schedule. Whether the **taker** coefficient itself changes is **UNKNOWN** without the PDF. The OpenAPI wording puts maker fees **in addition to** the general table, which is the best reading, and it is still **INFERRED**.

`GET /series/fee_changes` and `GET /series/fee_changes?series_ticker=KXESOCCERGAME&show_historical=true` returned `series_fee_change_arr: []`. `GET /events/fee_changes?limit=3` returned `event_fee_changes: []`. **VERIFIED** empty at that moment. The endpoints exist and are public. Event overrides use `fee_type_override` and `fee_multiplier_override`; null means the override is cleared and the series schedule applies. Scheduled time is `scheduled_ts`. **VERIFIED** in the schema.

Markets also have `fee_waiver_expiration_time` (omitted when null on the samples). **VERIFIED** in the schema.

Changelog 2026-08-22: for combo trades, if a quoter hits an order that has rested **less than five seconds**, the quoter pays the **maker** fee and the resting order pays the **taker** fee. Combo maker multiplier in that note is `0.5`, “rather than the standard `0.25`.” Independent-NFL combos created after 2026-08-19 11:59 PM ET under `KXMVECROSSCATEGORY0-SHARD1` have **no maker fee**. **VERIFIED** as changelog text. The collector’s pairs should be single binaries, so this quoter swap should not apply. It matters if anyone later trades combos.

Rounding, **VERIFIED** in the fee-rounding page:

- Fees are computed at `$0.000001`.
- Direct-member balances sit on `$0.0001`. Non-direct (FCM) balances sit on `$0.01`.
- `trade_fee = ceil_6dp(model_fee)`.
- `aligned_change = floor_precision(revenue − trade_fee)` with `revenue` signed, negative for a buyer.
- `rounding_fee = (revenue − trade_fee) − aligned_change`.
- An accumulator per **order** (across taker and maker fills of that order) rebates rounding in steps of the user’s precision, and the rebate cannot make that fill’s net fee negative.
- Net fee = trade fee + rounding fee − rebate, and is ≥ 0.

`GET /account/limits` does not, in the documented example, say whether the member is direct. **UNKNOWN** how an API caller learns “direct” versus “non-direct” other than by knowing how the account was opened (Kalshi direct versus an FCM). The plan is right that qualification must wait for that fact.

### Three numeric examples

**Example 1. Official non-direct fill. VERIFIED**, copied from the fee-rounding page. Model fee `$0.00363825`, buyer revenue `−$0.055000`, cent precision:

```text
trade fee      = ceil_6dp($0.00363825)           =  $0.003639
aligned change = floor_cent(−$0.055 − $0.003639) = −$0.060000
rounding fee   = (−$0.055 − $0.003639) − (−$0.06) =  $0.001361
```

Balance moves `−$0.06`. Trade fee plus rounding fee is `$0.005`. The model fee alone understates the cash leaving the account by `$0.001361`.

**Example 2. Official accumulator, non-direct. VERIFIED** from the same page. Three fills each add `$0.004` of rounding overpayment. The third fill rebates `$0.01` and carries `$0.002`. Direct members use the same steps in `$0.0001` increments.

| Fill | Added | Before rebate | Rebate | Carried |
|---:|---:|---:|---:|---:|
| 1 | $0.004 | $0.004 | — | $0.004 |
| 2 | $0.004 | $0.008 | — | $0.008 |
| 3 | $0.004 | $0.012 | $0.010 | $0.002 |

A single-fill fee calculator overcharges relative to a one-shot order once the accumulator rebates. For a taker IOC that fills in several prints, replay must run the accumulator in fill order. The accumulator is per order, so two venue legs do not share one.

**Example 3. Same official inputs, direct-member precision. INFERRED** by applying the published floor rule at `$0.0001` instead of `$0.01`. This exact direct-member walk is not printed in the doc.

```text
trade fee      = ceil_6dp($0.00363825)              =  $0.003639
pre-align      = −$0.055000 − $0.003639             = −$0.058639
aligned change = floor to $0.0001 (−$0.058639)      = −$0.058700
rounding fee   = −$0.058639 − (−$0.058700)          =  $0.000061
net fee        = $0.003639 + $0.000061              =  $0.003700
```

Same contracts, same model fee: the non-direct member pays `$0.005` all-in and the direct member pays `$0.003700`.

**Plan’s 100 × $0.45 case, only if the model fee really is `$1.7325`.** The `$1.7325` figure is `0.07 × 100 × 0.45 × 0.55`. The `0.07` was **not** re-read. Given that model fee and buyer revenue `−$45.00`:

- Direct (`$0.0001`): `−45 − 1.7325 = −46.7325`, already on the grid. Rounding fee `$0`. Net fee `$1.7325`. The plan’s Kalshi cell matches this branch.
- Non-direct (`$0.01`): floor of `−46.7325` is `−46.74`. Rounding fee `$0.0075`. Net fee `$1.7400`. Capital in the plan’s table would be `$0.0075` higher and profit `$0.0075` lower.

Maker dollar amounts are not computed here. The maker multipliers `0.25` and `0.5` are verified as labels in the API description and the August 2026 changelog. Multiplying them by an unverified `0.07` would invent a schedule.

---

## 7. Settlement

Sources: [Market Settlement](https://docs.kalshi.com/getting_started/market_settlement), [Settlement Bounds](https://docs.kalshi.com/getting_started/settlement_bounds), lifecycle page, `Market` and `Settlement` schemas, live settled market, help [Timeline and Payout](https://help.kalshi.com/en/articles/13823825-timeline-and-payout).

**VERIFIED** mechanics:

- Yes result: yes holders receive `$1` per contract. No result: no holders receive `$1` per contract. Only **net** positions settle.
- Simple yes/no settlement fees are zero. Sub-cent **scalar** settlement may charge a fee. The payout figure named `CollateralAmountChange` is rounded to whole cents, and `CollateralAmountChange + MiscFeeAmt` equals the pre-rounding settlement value. That wording is on the settlement page and points at FIX for the message layout.
- `result`: `yes`, `no`, `scalar`, or `""` while unresolved.
- `settlement_value_dollars`: YES/LONG payout per contract, filled after determination. Live finalized `result: "no"` market had `settlement_value_dollars: "0.0000"` and `notional_value_dollars: "1.0000"`. **VERIFIED.**
- `expiration_value`: the value used for settlement (e.g. `"100"` against `floor_strike` 150.5, `strike_type` `greater`, result `no`).
- Timer: after determination the market stays `determined` for `settlement_timer_seconds`. Dispute can still change the result. Then REST `finalized` and WebSocket `settled`. Portfolio `Settlement.settled_time` is when payouts were processed. `Settlement.revenue` is integer **cents**. `Settlement.value` is nullable “payout of a single yes contract in cents.” `fee_cost` is a fixed-point dollar string. **VERIFIED** in the schema. Portfolio settlements require auth and were not fetched.
- The settlement page says markets “typically settle shortly after expiration,” and that timing varies with market type, source availability, and manual review. There is **no** guaranteed payout SLA in the API. **VERIFIED.**
- Help “Timeline and Payout” says the UI shows open, close, and expected payout, and that some markets close when the outcome occurs rather than at a listed clock. It does not add a machine-readable payout deadline beyond the API fields. **VERIFIED.**
- Settlement floors (`settlement_bounds_type: floor`) are documented and **“not live yet. The launch timeline is TBA.”** `MECNET` and `DIRECNET` events are always `default`. Live NBA event was `MECNET` and the sampled markets were `default`. **VERIFIED.**
- Early close rejects further order operations with `MARKET_INACTIVE`, including cancels. Resting orders are canceled shortly after close, with updates on the user channels. **VERIFIED** in the lifecycle page.

The live settled total shows why the plan must not recycle capital at `expected_expiration_time` or at `close_time`: those three timestamps differed, and `rules_secondary` allows a non-$1 “fair price” on a postponement.

`collateral_return_type: MECNET` plus `mutually_exclusive: true` means sibling markets in the event are a mutually exclusive group. Help [Collateral return](https://help.kalshi.com/en/articles/13823816-collateral-return) says netting is opt-in, locked per event per subaccount at the first order (`netting_enabled`), and returns cash early on hedged positions inside that event. A single-market YES-versus-the-other-venue-NO hedge is still one Kalshi contract. Holding both Kalshi outcomes is a different product. Positions include `netting_enabled` (changelog 2026-11-05). **VERIFIED** that the field and the help text exist.

Scalar markets (`market_type: scalar`, `result: scalar`) are a different payout. The plan’s $1/$0 gate should require `market_type == binary`, `notional_value_dollars == 1`, `settlement_bounds_type == default`, no `mve_collection_ticker`, and rules that do not pay a “fair price” or other partial amount.

---

## 8. Orders (later; not part of observation)

Source: OpenAPI `CreateOrderV2Request`, `BookSide`, `SelfTradePreventionType`; lifecycle page; AsyncAPI `fill` and `userOrder` examples; environments page for `warp-ws`.

`POST /trade-api/v2/portfolio/events/orders`. Legacy `POST /portfolio/orders` “will be deprecated no earlier than May 6, 2026.” That earliest date has passed. The legacy schema is still in OpenAPI 3.36.0. **VERIFIED** that the deprecation sentence and the legacy schema both exist. **UNKNOWN** whether the legacy route still accepts orders. New code should call v2 only.

### V2 body

Required: `ticker`, `side`, `count`, `price`, `time_in_force`, `self_trade_prevention_type`.

| Field | Values |
|---|---|
| `side` | `bid` or `ask`. **YES book only.** `bid` = buy YES. `ask` = sell YES, which is economically buy NO at `1 − price` |
| `price` | Fixed-point **YES** dollars. There is no `no_price` on v2 |
| `count` | Fixed-point contract string (`"10.00"`) |
| `time_in_force` | `fill_or_kill`, `good_till_canceled`, `immediate_or_cancel` |
| `expiration_time` | Unix **seconds**. Only with `good_till_canceled`. `GTT` is not a valid API value. Forbidden with `immediate_or_cancel` |
| `self_trade_prevention_type` | `taker_at_cross` or `maker`. Required |
| `post_only` | boolean |
| `reduce_only` | boolean. Orders are rejected unless `time_in_force` is `immediate_or_cancel` |
| `cancel_order_on_pause` | boolean |
| `client_order_id` | optional idempotency key |
| `subaccount` | 0 primary, or 1–63. Restricted keys must omit it or pass their own subaccount |
| `exchange_index` | Omit or `-1` to auto-route by ticker. `>= 0` pins a shard. Pin the market’s `exchange_index` |
| `order_group_id` | optional. Group limit is a rolling 15-second contract count |

**Buy YES** at limit `p`: `side=bid`, `price=p`.
**Buy NO** at NO-price `n`: `side=ask`, `price = 1 − n` (the YES price). Example: buy NO at `$0.60` → `side=ask`, `price="0.4000"`.

`taker_at_cross`: the incoming order stops when the next match would be against your own order. Fills already matched stay. `maker`: your resting order is canceled and matching continues. **VERIFIED.** A later arb taker that should not trade with itself wants `taker_at_cross`. `SelfTradeCancel` is a `lastUpdateReason` on the user-order stream.

### Response is not “the order”

HTTP 201 body: `order_id`, optional `client_order_id`, `fill_count`, `remaining_count`, `ts_ms`, and, only when `fill_count > 0`, `average_fill_price` and `average_fee_paid`.

`average_fee_paid` is the volume-weighted average fee **per contract**, not the total fee. **VERIFIED** in the schema description.

For IOC, `remaining_count` is the state after unfilled contracts are canceled. **VERIFIED.** **UNKNOWN:** whether a FOK that cannot fill completely is HTTP 201 with zero fill or a 4xx. Do not treat 201 as “fully filled.” Compare `fill_count` to the requested count. An ambiguous timeout must be resolved with `GET /portfolio/orders/{order_id}` (2 tokens) and the `user_orders` / `fill` streams, using `client_order_id` as the idempotency key. The exchange docs say REST portfolio views lag and should be combined with WebSocket. **VERIFIED** as that warning (`Get User Data Timestamp` description, also embedded in the SDK).

Cancel v2 returns `{order_id, client_order_id, reduced_by, ts_ms}`, not the order. Auto-route a cancel with `market_ticker`; an order id alone cannot find the shard. **VERIFIED.**

After `close_time`, order operations including cancel return `MARKET_INACTIVE`. **VERIFIED.**

### Private streams

Subscribe to `fill` and `user_orders`. For lower latency, a **second** connection to `warp-ws` can carry only those two channels. The book connection stays on `external-api-ws`.

Fill example fields include `trade_id`, `order_id`, `client_order_id`, `market_ticker`, `exchange_index`, `is_taker`, `yes_price_dollars`, `count_fp`, `fee_cost` (six-decimal dollars, example `"0.010000"`), `outcome_side`, `book_side`, legacy `side` / `action` / `purchased_side`, `post_position_fp`, `subaccount`, `ts_ms`. **VERIFIED** as the published example.

`user_orders` example includes `status` (`resting` and the order-status enum `resting` | `canceled` | `executed`), `outcome_side`, `book_side`, `yes_price_dollars`, fill/remaining/initial counts, separate taker and maker fill cost and fee dollar strings, `self_trade_prevention_type`, `order_group_id`, `subaccount_number`, `created_ts_ms`, `expiration_ts_ms`. **VERIFIED** as the published example.

Queue position: `GET /portfolio/orders/{order_id}/queue_position` and the bulk variant. Price-time priority. **VERIFIED** that the endpoint exists. It does not tell you other people’s place in line from the public book.

Order groups, subaccounts, and sharding are real execution constraints the observation app can ignore. The execution app cannot: an order on shard 3 needs collateral **on shard 3**. App/web orders auto-transfer between shards; API orders do not, unless the account enabled auto-rebalance. The `insufficient_shard_balance` error tells the caller to fund the shard (changelog 2026-11-12). **VERIFIED.**

---

## 9. Terms, automated access, storage

**VERIFIED** links and the pages that could be fetched:

- Docs welcome: continuing to use the API binds you to the [Developer Agreement](https://kalshi.com/developer-agreement).
- That URL, `https://kalshi.com/terms`, `https://kalshi.com/regulatory`, the fee schedule, and `https://kalshi.com/fee-schedule` all returned HTTP **429** Vercel “verifying your browser” this session. The agreement text was **not** read. **UNKNOWN** whether it limits automated trading, scraping, or storing order books.
- `https://docs.kalshi.com/getting_started/terms` is a glossary (series / event / market). It does not state API terms of use.
- Help [Kalshi API](https://help.kalshi.com/en/articles/13823854-kalshi-api) (dated 2026-03-10) says the API exposes your orders, trades, portfolio, all markets’ order books, limited stats, and public market data, and says “Currently, the API is implemented as a REST API.” That sentence is **outdated**: the docs and the 401 handshake show a WebSocket API. The article states no storage ban and no automation ban.
- Help [How is data stored](https://help.kalshi.com/en/articles/13823768-how-is-data-stored) is about KYC and bank data (Plaid, Aeropay), not market-data retention.
- The weather probability feed’s “don’t redistribute or store” line is specific to that licensed provider feed.

Do not treat “the help article didn’t forbid it” as permission. Read the developer agreement in a browser before retaining books or sending orders. Observation of public REST in this research was allowed far enough to return HTTP 200.

---

## 10. Official Python SDK

**VERIFIED** from [Kalshi SDKs](https://docs.kalshi.com/sdks/overview), PyPI JSON, and the `kalshi_python_sync` 3.36.0 wheel.

| Package | Version observed | Uploaded | Requires | Role |
|---|---|---|---|---|
| `kalshi_python_sync` (import `kalshi_python_sync`) | 3.36.0 | 2026-11-11T01:28:42Z | Python ≥ 3.13 | Generated REST client. OpenAPI 3.36.0, generator 7.17.0 |
| `kalshi_python_async` | 3.36.0 | 2026-11-11T01:28:40Z | Python ≥ 3.13 | Async REST twin |
| `kalshi-python` | 2.1.4 | 2025-09-06 | Python ≥ 3.9 | Docs say **deprecated** |

- Maintained: yes, as of this spec revision. Docs say SDKs publish around Tuesday–Wednesday and **lag the API**. They tell production clients to generate from `openapi.yaml` or call HTTP directly. **VERIFIED.**
- WebSocket: the wheel has no websocket module. The only “WebSocket” strings are comments telling you to combine REST with a WebSocket. **VERIFIED** by scanning the wheel.
- Auth: `cryptography>=41`. Ed25519 needs SDK ≥ 3.31.0. The package quick start still sets `host="https://api.elections.kalshi.com/trade-api/v2"`, which remains a supported host and is not the recommended `external-api` host. **VERIFIED.**
- License: `LicenseRef-Proprietary`. **VERIFIED** on PyPI metadata.
- Homepage `https://github.com/Kalshi/exchange-infra` did not appear in the public `GET /orgs/Kalshi/repos` list (9 public repos). `kalshi-starter-code-python` was last pushed 2025-03-07. **VERIFIED.** The generator source is not a public repo this session. **INFERRED:** `exchange-infra` is private.

The Python 3.12 collector should sign requests itself with `cryptography` and speak WebSocket with `websockets`. It should not import `kalshi_python_sync` unless the runtime moves to 3.13 and the proprietary license is accepted. Even then the SDK does not record the book.

---

## Implications for implementation

**Module `kalshi/endpoints.py`.** Constants, not scattered strings:

- `REST_BASE = "https://external-api.kalshi.com/trade-api/v2"`
- `WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"`
- `WS_SIGN_PATH = "/trade-api/ws/v2"`
- `DEMO_REST_BASE` / `DEMO_WS_URL` for the demo hosts in section 1
- Later, optional `FILL_WS_URL = "wss://warp-ws.kalshi.com/trade-api/ws/v2"` used only for `fill` and `user_orders`

**Module `kalshi/auth.py`.** `load_private_key`, `sign_request`, `auth_headers` as in section 2. Config: `KALSHI_API_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH`. Key created with `scopes: ["read"]`. Observation process does not contain `POST /portfolio/events/orders`.

**Module `kalshi/rest.py`.** `httpx` + `Decimal`.

- `get_market(ticker)`, `get_event`, `get_series`, `get_orderbook(ticker, depth=0)`, `get_orderbooks(tickers)` (chunks of 100).
- `iter_markets(...)` follows `cursor` until missing / null / `""`. `limit=1000` for markets. Do not paginate `/series` until a probe shows `limit` is honored; today’s catalog came back in one body.
- Discovery filter for the watchlist builder: `status=open`, `mve_filter=exclude`, then **client-side** `expected_expiration_time` within the horizon. Do not use `min_close_ts` as that horizon. Join series and event with `series_ticker` and `event_ticker`; do not split the ticker string.
- Parse only `*_dollars` and `*_fp`. Reject a payload that lacks them.

**Module `kalshi/fees.py`.** `load_fee_schedule(series, event_overrides, waiver_ts) -> FeeModel`.

- Inputs: `series.fee_type`, `series.fee_multiplier`, the latest `series_fee_change` and `event_fee_change` whose `scheduled_ts` is `<= now`, and `market.fee_waiver_expiration_time`.
- Config `kalshi_member_precision`: `"0.0001"` or `"0.01"`. Unknown precision ⇒ assessment is scenario-only.
- `trade_fee(model_fee) = ceil_to_increment(model_fee, Decimal("0.000001"))`.
- `apply_fill(order_accumulator, revenue, model_fee, precision) -> net_fee` implementing section 6, including the rebate cap.
- The quadratic coefficient stays a config value `KALSHI_QUADRATIC_COEFFICIENT` defaulting to **unset**. Unset ⇒ no qualified fee. Do not hard-code `0.07` until the PDF is stored under `sources/` and the config records the PDF hash and retrieval time.
- Maker path is selected only when `fee_type` is `quadratic_with_maker_fees` (multiplier 0.25) or `quadratic_with_combo_maker_fees` (0.5). A taker arb still records `fee_type`, because the schedule family may differ.

**Module `kalshi/book.py`.** One function, two modes.

- `normalize_rest_book(orderbook_fp) -> Book`: levels ascending; best bid is the last level; YES ask ladder is the NO bids complemented (`price = 1 - no_price`, same size).
- `apply_ws(book, msg, use_yes_price: bool)`: if `use_yes_price` and `side=="no"`, the price is already the YES-scale price of that NO bid (the YES ask). If `use_yes_price` is false, complement NO prices the same way as REST.
- Subscribe with `use_yes_price: true` and `update_interval_ms: 0`.
- On `seq` gap, on disconnect, on parse error: mark the book invalid, then `get_snapshot` or reconnect. Do not apply a delta after a gap. Do not merge a REST book into a live `seq` stream except as a labeled polling fallback that replaces the book and drops the stream’s authority.
- Ignore `client_order_id` for the public level; store it only as “our order touched this delta” for the later execution log.
- Book observation time is `msg.ts_ms` when present, else `sending_ts_ms`. Receipt time is local. A ping does not update either.

**Module `kalshi/metadata.py`.** Refresh rules, series fee, event `collateral_return_type`, `mutually_exclusive`, `price_ranges`, and `exchange_index` on a slow loop (tens of seconds), not every book tick. Hash `rules_primary`, `rules_secondary`, series `contract_terms_url` body if fetched, `settlement_sources`, strike fields, `notional_value_dollars`, `settlement_bounds_type`. Any hash change revokes approval.

**Qualification gate `kalshi/qualify.py`.** Reject unless all of:

- `market_type == "binary"`
- `notional_value_dollars == Decimal("1")`
- `status == "active"`
- `settlement_bounds_type == "default"`
- no `mve_collection_ticker`
- `rules_secondary` / series prohibitions stored and human-approved (the “fair price” postponement clause fails a $1 complement)
- fee model fully resolved, including member precision
- book `seq` healthy and book age under the configured 2 seconds
- prices on the market’s `price_ranges` grid

**Config (observation).** `book_max_age_seconds=2`, `use_yes_price=true`, `update_interval_ms=0`, `orderbook_depth=0` for the polling fallback, `markets_page_limit=1000`, `read_token_cost=10`, `basic_read_refill_per_s=200`. Stay well under the refill. Record `Cache-Control` and `x-kalshi-cache-hits` on every REST book so a cached body cannot look live.

**Tests.**

1. `test_sign_strips_query_and_uses_root_path`: message is `1703123456789GET/trade-api/v2/portfolio/orders` for a URL whose query is `?limit=5`. Ed25519 and RSA-PSS vectors (known PEM, known signature).
2. `test_rest_complement`: fixture of the live NBA tail. Best YES bid `0.5000`, best NO bid `0.4500` ⇒ YES ask `0.5500`, NO ask `0.5000`, sizes taken from the NO and YES bids respectively.
3. `test_ws_use_yes_price_does_not_double_complement`: NO delta `price_dollars=0.70`, `side=no`, `use_yes_price=true` becomes a YES ask at `0.70`, not at `0.30`.
4. `test_seq_gap_discards_delta`: snapshot `seq=2`, next applied message `seq=4` leaves the book invalid and emits no qualified price. After a snapshot replacement, the following `seq` applies.
5. `test_depth_keeps_best_tail`: ascending 40-level fixture, `depth=3` equals the last three levels.
6. `test_fee_rounding_matches_docs`: example 1 numbers (`0.003639` / `0.001361` / `−0.06`) and example 3 direct-member numbers (`0.000061` / `−0.0587`).
7. `test_fee_unknown_coefficient_not_qualified`: missing `KALSHI_QUADRATIC_COEFFICIENT` or missing member precision ⇒ profit is scenario-only.
8. `test_fractional_count`: book level `"10.25"` and trade `"12.34"` survive as `Decimal` and are not truncated to `int`.
9. `test_status_filter_rejects_all`: client maps internal “any” to an omitted `status`, never `status=all`.
10. `test_expected_expiration_not_close_time`: the esoccer pair of timestamps stays inside a 7-day expected-resolution window and outside a 7-day close window.
11. `test_settlement_value_yes_side`: `result=no`, `settlement_value_dollars=0` pays the NO holder `1` and the YES holder `0`.
12. `test_observation_has_no_order_route`: import scan or allow-list. The observation package cannot construct `POST /portfolio/events/orders`.
13. Later, demo-only: `read` scope can subscribe to `orderbook_delta`; a `read` key receives 403 on create-order; FOK and IOC `fill_count` versus `remaining_count`; `taker_at_cross` against a same-account resting order.

---

## Open questions

1. The fee-schedule PDF coefficient, rounding examples in that PDF, and any series named in the “specific trading fees” table. Blocked by HTTP 429 on `kalshi.com`.
2. Whether `quadratic_with_maker_fees` changes the **taker** coefficient or only adds a maker fee at 0.25× the general maker formula.
3. How an API response tells a direct member from a non-direct member.
4. Whether a `scopes: ["read"]` key may open the WebSocket and subscribe to `orderbook_delta`, `ticker`, `trade`, and `market_lifecycle_v2`.
5. Which auto-route write-billing sentence is live (rate-limits page versus sharding page).
6. Exact delta application when `delta_fp` drives a level through zero, and whether a snapshot’s omitted side means “no bids” or “field missing, keep previous.”
7. Anonymous REST rate limit, if any.
8. Whether `GET /markets/{ticker}` is cached for 5 seconds as the 2026-11-12 changelog says. This session saw `x-kalshi-cache-hits: 1` and no `Cache-Control`, and sizes disagreed with the book.
9. Whether `GET /series` will honor `limit` and `cursor` later. Today `limit=1` returned the entire catalog.
10. Developer-agreement text on automation and data retention.
11. FOK failure shape (HTTP status versus a 201 with `fill_count=0`).
12. Whether legacy `POST /portfolio/orders` still matches orders after 2026-05-06.
13. Payout credit latency after WebSocket `settled` / `settlement_ts` (buying-power versus withdrawable cash). The API does not state a deadline.
14. `use_yes_price` default-flip date. Not announced.
15. Authenticated `GET /account/limits` for the actual tier, and `GET /account/endpoint_costs` again once a key exists, in case anonymous and authenticated cost tables differ. This session’s anonymous table matched the documented exceptions that were checked.
