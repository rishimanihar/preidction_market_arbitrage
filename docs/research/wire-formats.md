# Wire formats (live public responses)

Captured 2026-11-12 ~01:38–01:41 UTC with `User-Agent: pmarb-research/0.1` and `Accept: application/json`. No auth headers. Bodies below are the raw JSON files in this directory (not reformatted).

Bases checked against the docs:

- Kalshi REST base used here, `https://api.elections.kalshi.com/trade-api/v2`, is listed as a production server on [Get Markets](https://docs.kalshi.com/api-reference/market/get-markets) and [Get Market Orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook). The same pages also list `https://external-api.kalshi.com/trade-api/v2`.
- Polymarket US public gateway `https://gateway.polymarket.us` matches [Introduction](https://docs.polymarket.us/api-reference/introduction).

## Files and URLs

| File | HTTP | URL |
| --- | --- | --- |
| `kalshi_markets.json` | 200 | `https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=5` |
| `kalshi_market.json` | 200 | `https://api.elections.kalshi.com/trade-api/v2/markets/KXNFLGAME-26NOV22NYJLAC-LAC` |
| `kalshi_series.json` | 200 | `https://api.elections.kalshi.com/trade-api/v2/series/KXNFLGAME` |
| `kalshi_orderbook.json` | 200 | `https://api.elections.kalshi.com/trade-api/v2/markets/KXNFLGAME-26NOV22NYJLAC-LAC/orderbook` |
| `polyus_markets.json` | 200 | `https://gateway.polymarket.us/v1/markets?active=true&closed=false&limit=5` |
| `polyus_market.json` | 200 | `https://gateway.polymarket.us/v1/market/slug/tec-mls-winner-2026-11-07-vwh` |
| `polyus_book.json` | 200 | `https://gateway.polymarket.us/v1/markets/tec-mls-winner-2026-11-07-vwh/book` |

Failed attempt, then the documented path:

- `GET /v1/markets/tec-mls-winner-2026-11-07-vwh` → **HTTP 404**, body `{"code":5,"message":"The server was unable to process your request.","details":[]}`. `code` 5 is the gRPC NOT_FOUND number. The body has no HTTP status field.
- Documented alternative [Get Market By Slug](https://docs.polymarket.us/api-reference/markets/get-market-by-slug) is `GET /v1/market/slug/{slug}` (singular `market`, extra `slug` segment). That returned 200 and is what `polyus_market.json` stores. The book path really is `GET /v1/markets/{slug}/book` (plural `markets`).

Chosen markets:

- Kalshi detail/series/book: `KXNFLGAME-26NOV22NYJLAC-LAC` (Los Angeles C to win vs New York J, 2026-11-22). Both sides of the book are non-empty (40 yes levels, 37 no levels). Best yes bid `0.5200` size `16416.37`. Series ticker is the prefix `KXNFLGAME` (markets do not carry `series_ticker`).
- The `limit=5&status=open` page is **not** that market. With no `mve_filter`, the first page is five brand-new multivariate combos (`KXMVECROSSCATEGORY-…`), each with `yes_bid_dollars`/`yes_ask_dollars` `"0.0000"`, sizes `"0.00"`, and `volume_fp` `"0.00"`. A non-empty book required a different ticker. `mve_filter=exclude` is the documented way to skip combos ([Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)).
- Polymarket US: first row of the active page, slug `tec-mls-winner-2026-11-07-vwh` (Vancouver Whitecaps FC, 2026 MLS Cup). Book state `MARKET_STATE_OPEN`, 11 bids and 13 offers.

---

## Kalshi market objects

Wrappers: list is `{ "markets": [ Market, ... ], "cursor": "<opaque string>" }`. Detail is `{ "market": Market }`. Same Market shape on both files.

### Prices and sizes (dollars and fixed-point strings, not cents)

There are **no integer cent fields** (`yes_bid`, `last_price`, `volume` as numbers) on these responses. Prices are `FixedPointDollars` strings, four decimal places, dollars per contract. Sizes are `FixedPointCount` strings, two decimal places. Examples from `kalshi_market.json`:

| Field | JSON type | Example | Meaning |
| --- | --- | --- | --- |
| `yes_bid_dollars`, `yes_ask_dollars`, `no_bid_dollars`, `no_ask_dollars` | string | `"0.5200"`, `"0.5400"`, `"0.4600"`, `"0.4800"` | Best yes/no bid and ask, in dollars |
| `yes_bid_size_fp`, `yes_ask_size_fp` | string | `"16416.37"`, `"225.12"` | Contracts at the best yes bid / yes ask. Fractional contracts are real (not a display artifact) |
| `last_price_dollars`, `previous_price_dollars`, `previous_yes_bid_dollars`, `previous_yes_ask_dollars` | string | `"0.5200"`, `"0.5600"` | Last yes trade, and prior-day bid/ask/last |
| `volume_fp`, `volume_24h_fp`, `open_interest_fp` | string | `"969.45"`, `"284.99"`, `"826.45"` | Contract counts, two decimals |
| `notional_value_dollars` | string | `"1.0000"` | Payout of one contract |
| `price_ranges[].start`, `.end`, `.step` | string | `"0.0000"`, `"1.0000"`, `"0.0100"` | Tick grid in dollars. `price_level_structure` is the string `"linear_cent"` (one cent = `"0.0100"` dollars) |

Empty quote on the combo page is the string `"0.0000"`, not JSON `null`. Empty size/volume is `"0.00"`. `result` and `expiration_value` are `""`, not null.

Identity check on the NFL book: `yes_bid_dollars` `"0.5200"` equals the last `yes_dollars` level, and `yes_bid_size_fp` `"16416.37"` equals that level's size. `yes_ask_dollars` `"0.5400"` equals `1 - best no bid`, and `yes_ask_size_fp` `"225.12"` equals the size at no `"0.4600"`.

### Status

Query `status=open` is a **filter**, not the field value. Every market in `kalshi_markets.json` and the NFL detail has `"status": "active"`.

Documented response enum ([Get Markets](https://docs.kalshi.com/api-reference/market/get-markets)): `initialized`, `inactive`, `active`, `closed`, `determined`, `disputed`, `amended`, `finalized`.

Documented **query** enum on the same page: `unopened`, `open`, `paused`, `closed`, `settled`. The prose above the OpenAPI block omits `paused`. Live `status=open` returns `active`.

Other scalars seen: `market_type` `"binary"`, `strike_type` `"structured"`, `settlement_bounds_type` `"default"`, `can_close_early` boolean, `is_provisional` boolean (combos only), `settlement_timer_seconds` JSON number `60`, `exchange_index` JSON number.

### Rules text

- `rules_primary` (string): one sentence. NFL example starts `If Los Angeles C wins the New York J vs Los Angeles C Pro Football game…`.
- `rules_secondary` (string): longer plain text, includes a blank line (`\n\n`) and the league trademark disclaimer.
- `early_close_condition` (string, detail only in this capture): `This market will close and expire after a winner is declared.`
- `title`, `yes_sub_title`, `no_sub_title` (strings). On this market `no_sub_title` equals `yes_sub_title` (`Los Angeles C`), matching the docs note that `no_sub_title` is deprecated and copies `yes_sub_title`.

### Expiration / end

All timestamps are RFC3339 strings, `Z` suffix. Fractional seconds appear on creation/update (`2026-11-10T05:03:39.250971Z`, microseconds) and are omitted on some close times (`2026-11-24T21:05:00Z`).

| Field | NFL example | Role |
| --- | --- | --- |
| `open_time` | `2026-11-10T05:06:00Z` | Trading open |
| `close_time` | `2026-11-24T21:05:00Z` | Scheduled close |
| `expected_expiration_time` | `2026-11-23T00:05:00Z` | Expected expiration |
| `expiration_time` | `2026-11-24T21:05:00Z` | Present, documented as deprecated |
| `latest_expiration_time` | `2026-11-24T21:05:00Z` | Latest possible expiration |
| `occurrence_datetime` | `2026-11-23T00:05:00Z` | Recorded event time |
| `updated_time` | `2026-11-10T05:06:00.243247Z` | Last **non-trading** metadata update (docs). It does not move with the book |
| `created_time` | `2026-11-10T05:03:39.250971Z` | Created |

No `end_date` field. Combo markets add `custom_strike` (object of strings, including `Associated Markets`), `mve_collection_ticker`, and `mve_selected_legs[]` with `event_ticker`, `market_ticker`, `side` (`"yes"` / `"no"`). The NFL market's `custom_strike` is `{ "football_team": "<uuid>" }`.

### Fees

**Not on the market object.** They are on the series ([Get Series](https://docs.kalshi.com/api-reference/market/get-series)):

- `fee_type`: string. Live: `"quadratic_with_maker_fees"`. Documented enum: `quadratic`, `quadratic_with_maker_fees`, `quadratic_with_combo_maker_fees`, `flat`.
- `fee_multiplier`: JSON number. Live token is `1` (not `"1"`, not `1.0`). Schema says `format: double`.

Series wrapper is `{ "series": { ... } }`. Also present: `ticker`, `title`, `category`, `categories` (array of strings), `tags`, `frequency` (`"custom"`), `contract_url`, `contract_terms_url`, `settlement_sources` (`[{ "name", "url" }]`), `additional_prohibitions` (array of strings), `product_metadata` (object; live `{ "scope": "Game" }`), `exchange_index` (number), `last_updated_ts` (RFC3339 string).

---

## Kalshi orderbook (`kalshi_orderbook.json`)

```text
{ "orderbook_fp": { "yes_dollars": [ [price, size], ... ], "no_dollars": [ [price, size], ... ] } }
```

- Only key on the response is `orderbook_fp`. No sibling `orderbook`, no cent arrays.
- Each level is a JSON array of **two strings**: `[dollars, contract_count_fp]`. Example: `["0.5200", "16416.37"]`. No third “order count” element.
- Prices: four decimal dollar strings (`"0.0100"` … `"0.5200"`). Sizes: two decimal count strings, including fractional (`"2187348.76"`).
- **Bids only.** No ask arrays. A yes bid at price `P` is a no ask at `1 - P` (docs still phrase the complement as `100 - X` cents; the wire complement is `1 - P` dollars).
- **Ordering on this book: ascending price (worst bid first, best bid last)** on both `yes_dollars` and `no_dollars`. Yes runs `"0.0100"` → `"0.5200"`. No runs `"0.0100"` → `"0.4600"`. The best level is the last element, and it matches `yes_bid_dollars` / `no_bid_dollars`.

Docs: [Get Market Orderbook](https://docs.kalshi.com/api-reference/market/get-market-orderbook). Schema `PriceLevelDollarsCountFp` matches the live pair of strings. See discrepancies below for the prose that says “best to worst” and “order counts”.

---

## Kalshi WebSocket (docs only; not captured)

Auth is required to open the socket, so these shapes are from the docs, not a live frame.

- URL: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`. Also still supported: `wss://api.elections.kalshi.com/trade-api/ws/v2`. Demo: `wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2`. Source: [Quick Start: WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets).
- Handshake headers: `KALSHI-ACCESS-KEY` (API key id), `KALSHI-ACCESS-TIMESTAMP` (unix **milliseconds**, decimal string), `KALSHI-ACCESS-SIGNATURE` (base64). Signed text is `timestamp + "GET" + "/trade-api/ws/v2"` (path only, query stripped). RSA-PSS SHA256 or Ed25519 depending on the key.
- Subscribe (orderbook channel name is `orderbook_delta`):

```json
{ "id": 1, "cmd": "subscribe", "params": { "channels": ["orderbook_delta"], "market_tickers": ["TICKER"] } }
```

`market_ticker` (string) is also accepted for one market. `market_id` / `market_ids` are not.

- The quick start lists `orderbook_delta` under **private channels**, but the snapshot/delta payload is the public book. The connection itself is authenticated either way. Public channels that still need the authenticated session: `ticker`, `trade`, `market_lifecycle_v2`, `multivariate_market_lifecycle`, `multivariate`.
- Server sends `orderbook_snapshot` first, then `orderbook_delta`. Spec: [Orderbook Updates](https://docs.kalshi.com/websockets/orderbook-updates).

`orderbook_snapshot` (required: `type`, `sid`, `seq`, `msg`):

| Field | Type | Notes |
| --- | --- | --- |
| `type` | string | `"orderbook_snapshot"` |
| `sid` | integer | Subscription id, ≥ 1 |
| `seq` | integer | **Per subscription**, starts at ≥ 1, contiguous. Not an exchange-wide sequence. Gaps mean missed messages on this subscription |
| `msg.market_ticker` | string | |
| `msg.market_id` | string (uuid) | |
| `msg.clock` | integer | Exchange clock of the newest change included. Same clock can be compared across connections in real-time mode. `0` if the market has no recorded change |
| `msg.yes_dollars_fp`, `msg.no_dollars_fp` | array of `[string, string]` | `[price_in_dollars, contract_count_fp]`. **Omitted entirely** when that side has no offers (not an empty array). Doc example is ascending: `[["0.0800","300.00"],["0.2200","333.00"]]` |
| `sending_ts_ms` | integer | Optional. Unix ms when Kalshi queued the frame |

`orderbook_delta` (`msg` required: `market_ticker`, `market_id`, `clock`, `price_dollars`, `delta_fp`, `side`):

| Field | Type | Notes |
| --- | --- | --- |
| `type` | string | `"orderbook_delta"` |
| `sid`, `seq` | integer | Same `seq` counter as the snapshot |
| `msg.price_dollars` | string | Dollar price of the level (example `"0.9600"`) |
| `msg.delta_fp` | string | Signed fixed-point contract delta, 2 decimals (example `"-54.00"`). Apply to the level; a result of zero removes it |
| `msg.side` | string | `"yes"` or `"no"` |
| `msg.clock` | integer | ≥ 1. Real-time: same value on every connection for that change; several deltas may share a clock. Conflated mode: clock can go backwards; do not drop on clock, follow `seq` |
| `msg.ts_ms` | integer | Optional. Unix ms of the change |
| `msg.ts` | string | Optional, **deprecated** RFC3339. Use `ts_ms` |
| `msg.client_order_id` | string | Optional. Present only when **your** order caused the change |
| `msg.subaccount` | integer | Optional. Same condition, if you use subaccounts |
| `sending_ts_ms` | integer | Optional |

`seq` is how you detect gaps. `clock` is how you line up the same change across connections (real-time mode only).

---

## Polymarket US markets

List wrapper: `{ "markets": [ Market, ... ] }`. No cursor in this response. Detail wrapper: `{ "market": Market }`. The five list rows and the slug detail used the same field set for this capture.

### Prices

Two different encodings on one object:

1. **Amount objects** (decimal **strings** + currency), four places in this capture: `bestBidQuote` / `bestAskQuote` = `{ "value": "0.1540", "currency": "USD" }` and `{ "value": "0.1550", "currency": "USD" }`. Same shape as book `px`.
2. **Bare strings** on each `marketSides[]` entry: `price` `"0.1550"` (long / Yes) and `"0.846"` (short / No). The No side is **three** decimal places, not four. `quote` repeats that as an Amount.
3. **JSON numbers**, not strings: `feeCoefficient` `0.095`, `orderPriceMinTickSize` `0.001`, `minimumTradeQty` `1`. Tick size is a tenth of a cent (`0.001`), finer than Kalshi's `"0.0100"` dollar step on the NFL market.
4. **Deprecated strings that contain JSON text**, not arrays: `outcomes` is the string `["No","Yes"]`. `outcomePrices` is the string `["0.1550","0.846"]`. Zipping them labels No=`0.1550` and Yes=`0.846`, which is the **opposite** of `marketSides` (Yes long `0.1550`, No short `0.846`). `0.846` equals `1 - 0.1540` (the bid), while `0.1550` equals the ask. Do not zip `outcomes` to `outcomePrices`.

`marketSides[]` (this market has two):

| Field | JSON type | Yes (long) | No (short) |
| --- | --- | --- | --- |
| `id` | string | `"15947"` | `"15948"` |
| `marketId` | **number** | `7974` | `7974` |
| `marketSideType` | string | `MARKET_SIDE_TYPE_INSTRUMENT` | same |
| `identifier` | string | the market slug | the same slug |
| `description` | string | `"Yes"` | `"No"` |
| `long` | boolean | `true` | `false` |
| `tradable` | boolean | `true` | `true` |
| `price` | string | `"0.1550"` | `"0.846"` |
| `quote` | Amount | value `"0.1550"` | value `"0.846"` |

Top-level `id` is the **string** `"7974"`, while `marketSides[].marketId` is the number `7974`.

`volume`, `volume24hr`, `volume1wk`, `volume1mo`, `volume1yr` are **absent** on all five list rows and on the detail (not zero). `line`, `rulesDisclaimer`, `rulesDisclaimerPopup`, `subjectId` absent. `assetPriceTerms` and `politics` are JSON `null`. `tags` is `[]`.

### Status

Two layers:

- Gateway `status`: `"MARKET_STATUS_OPEN"`. Documented enum ([Get Markets](https://docs.polymarket.us/api-reference/markets/get-markets)): `MARKET_STATUS_UNSPECIFIED`, `MARKET_STATUS_OPEN`, `MARKET_STATUS_CLOSED`, `MARKET_STATUS_RESOLVING`, `MARKET_STATUS_RESOLVED`, `MARKET_STATUS_HALTED`.
- Booleans alongside it: `active` `true`, `closed` `false`, `archived` `false`, `hidden` `false`.
- Deprecated `ep3Status`: `"OPEN"` (not the `MARKET_STATUS_*` spelling). Docs say prefer `status`.

`sportsMarketType` is the lowercase string `"futures"`. `sportsMarketTypeV2` is `"SPORTS_MARKET_TYPE_FUTURE"`. `marketType` (deprecated) is also `"futures"`. `category` `"sports"`. `eventSubcategory` `"SOCCER"`.

### Rules text

- `description` (string) is the rules/resolution paragraph. This one starts `Will Vancouver Whitecaps FC win the 2026 MLS Cup, scheduled to conclude December 18, 2026?` and says the expiration date is amended if the event is postponed.
- `question` `"Major League Soccer Champion"`, `title` `"Vancouver Whitecaps FC"`, `titleShort` same as title, `subtitle` `"10-2-2"`.
- `rulesDisclaimer` was omitted. Docs say it is omitted when the sports market type has no disclaimer.

### Fees

`feeCoefficient` is a JSON number `0.095` on the market (not on a series object, and not a fixed-point string). No `fee_type` / `fee_multiplier`.

Also on the object, not in the Market schema excerpt used for this note: `singlesRfqConfig` object (`enabled` bool, `midpointDeviation` number `0.2`, `minOrderValueUsd` number `2500`, `maxQuoteAgeSeconds` number `30`, `quoteWindows[].windowMs` **string** `"400"`, `ruleName`, `version`).

### End dates

RFC3339, whole seconds, `Z`:

| Field | Example |
| --- | --- |
| `startDate` | `2026-03-19T21:25:04Z` |
| `endDate` | `2027-01-01T21:20:09Z` |
| `gameStartTime` | `2026-02-21T00:00:00Z` (deprecated in the schema; still present) |
| `createdAt` | `2026-03-19T21:31:54Z` |
| `updatedAt` | `2026-11-12T01:39:21Z` |
| `ep3SyncedAt` | `2026-10-29T12:04:57Z` |

`endDate` is the market end. For automated crypto markets the docs say use `assetPriceTerms.windowEnd` instead; this sports market has `assetPriceTerms: null`.

---

## Polymarket US book (`polyus_book.json`)

```text
{ "marketData": { "marketSlug", "bids", "offers", "state", "stats", "transactTime" } }
```

Path: [Get Market Book](https://docs.polymarket.us/api-reference/markets/get-market-book). Live key is `offers`, not `asks`.

Level: `{ "px": { "value": "<decimal string>", "currency": "USD" }, "qty": "<decimal string>" }`.

- `px.value`: dollars, four decimal places here (`"0.1540"`, `"0.1550"`, `"0.9990"`).
- `qty`: contract size string, four decimal places even when whole (`"343.0000"`, `"1.0000"`, `"250000.0000"`).
- **Bids: descending price (best / highest first).** First `"0.1540"` qty `"343.0000"`, last `"0.0010"` qty `"250000.0000"`.
- **Offers: ascending price (best / lowest first).** First `"0.1550"` qty `"685.0000"`, last `"0.9990"` qty `"249999.0000"`.
- This matches the docs: “highest bid first, lowest ask first” ([Markets WebSocket](https://docs.polymarket.us/api-reference/websocket/markets) order-book section, same level shape as REST).

`state`: `"MARKET_STATE_OPEN"`. Book-state enum (not the same as market `status`): `MARKET_STATE_OPEN`, `MARKET_STATE_PREOPEN`, `MARKET_STATE_SUSPENDED`, `MARKET_STATE_HALTED`, `MARKET_STATE_EXPIRED`, `MARKET_STATE_TERMINATED`, `MARKET_STATE_MATCH_AND_CLOSE_AUCTION`.

`transactTime`: RFC3339 with **nanoseconds**, `"2026-11-12T01:40:06.545601376Z"`.

`stats` prices are Amounts. Sizes are strings. Captured fields:

| Field | Type | Example |
| --- | --- | --- |
| `openPx`, `closePx`, `lowPx`, `highPx`, `lastTradePx`, `settlementPx`, `currentPx` | Amount | last trade `{ "value": "0.1550", "currency": "USD" }`; `currentPx` `"0.1545"` (mid of 0.1540/0.1550) |
| `sharesTraded`, `openInterest`, `lastTradeQty` | string, 4 decimals | `"3626164.0000"`, `"121250.0000"`, `"1.0000"` |
| `notionalTraded` | Amount | `"816259.1860"` USD |
| `*SetTime` (`open`, `close`, `high`, `low`, `lastTrade`, `settlement`, `openInterest`, `notional`) | string | nanosecond RFC3339 |
| `settlementPriceCalculationMethod` | string | `"SETTLEMENT_PRICE_CALCULATION_METHOD_EVENT_TIER_2"` |
| `lastPriceSample` | object | `longPx` Amount `"0.1550"`, `shortPx` Amount `"0.845"` (**3** decimals), `ts` nanosecond RFC3339 |

`settlementPx` is filled (`"0.2240"`) while `state` is still `MARKET_STATE_OPEN`, so on this market it is not “present only after resolution”. Schema fields `indicativeOpenPx` and `settlementPreliminaryFlag` were absent.

`shortPx` `"0.845"` equals `1 - 0.155` and does not use the four-decimal form `"0.8450"`. Same inconsistency as `marketSides[].price` for No (`"0.846"`).

---

## Polymarket US WebSocket (docs only; not captured)

Auth is required, so shapes are from [Markets WebSocket](https://docs.polymarket.us/api-reference/websocket/markets) and [WebSocket API Overview](https://docs.polymarket.us/api-reference/websocket/overview).

- URL: `wss://api.polymarket.us/v1/ws/markets` (market data). Private account stream is `wss://api.polymarket.us/v1/ws/private`. These are on `api.polymarket.us`, not the public gateway.
- Handshake headers: `X-PM-Access-Key` (key id), `X-PM-Timestamp` (unix **milliseconds**), `X-PM-Signature` (base64). Signed text: `timestamp + "GET" + path`, path `/v1/ws/markets` or `/v1/ws/private`.
- Client subscribe:

```json
{ "subscribe": { "requestId": "md-sub-1", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA", "marketSlugs": ["slug"] } }
```

`subscriptionType` also: `SUBSCRIPTION_TYPE_MARKET_DATA_LITE`, `SUBSCRIPTION_TYPE_TRADE`. Optional `responsesDebounced: true`. Max 100 slugs per subscription.

- Full market-data push: `{ "requestId", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA", "marketData": { ... } }`. `marketData` matches the REST book: `marketSlug`, `bids[]`, `offers[]` (`px` Amount + `qty` string), `state` (`MARKET_STATE_*`), `stats` (`lastTradePx` Amount, `sharesTraded` string, `openInterest` string, `highPx`, `lowPx`), `transactTime`. Doc example qty values are short strings (`"0.50"`, `"150000"`), not the four-decimal `"343.0000"` the REST book returned.
- Lite push: `marketDataLite` with `currentPx`, `lastTradePx`, `bestBid`, `bestAsk` (Amounts), `bidDepth` / `askDepth` (integers), `sharesTraded`, `openInterest` (strings).
- Trade push: `trade.price` Amount, `trade.quantity` Amount (currency on the quantity, unlike book `qty` which is a bare string), `tradeTime`, `maker` / `taker` `{ "side": "ORDER_SIDE_BUY"|"ORDER_SIDE_SELL", "intent": "ORDER_INTENT_BUY_LONG"|"ORDER_INTENT_SELL_LONG"|"ORDER_INTENT_BUY_SHORT"|"ORDER_INTENT_SELL_SHORT" }`.

---

## Docs vs live

1. **Kalshi status vocabulary.** Filter `status=open` returns markets whose body `status` is `active`. Query enum (`unopened|open|paused|closed|settled`, plus `paused` only in the OpenAPI enum) is a different set from the body enum (`initialized|inactive|active|closed|determined|disputed|amended|finalized`). Prose on the Get Markets page also drops `paused`.
2. **Kalshi REST book order.** Prose says levels are “organized from best to worst”. Live `yes_dollars` and `no_dollars` are **ascending** (best bid last). The websocket snapshot example is also low-to-high (`0.0800` then `0.2200`), so the example agrees with live REST and the REST prose does not.
3. **Kalshi “order counts”.** Orderbook prose says each level has quantities **and order counts**. Schema and live levels are exactly two strings: `[dollars, fp count]`.
4. **Kalshi cent language in prose.** The orderbook page still explains the complement as “a yes bid at 7¢ is the same as a no ask at 93¢” and “100−X”. The wire is dollar strings, and the complement is `1 - price`. Schema text is already in dollars (`"0.1500"`). No cent integers were returned.
5. **Kalshi `fee_multiplier`.** Schema `format: double`. Live JSON encodes a whole multiplier as `1`, not `1.0` and not a string. `fee_type` string matches the documented enum.
6. **Polymarket market-by-slug path.** `GET /v1/markets/{slug}` is 404 (`code` 5). The working route is `GET /v1/market/slug/{slug}`. The book route keeps the plural: `GET /v1/markets/{slug}/book`.
7. **Polymarket price precision is not fixed at four places inside one payload.** Bid/ask quotes and long `price` are `"0.1540"` / `"0.1550"`. Short side `price` and `quote.value` are `"0.846"`. Book `lastPriceSample.shortPx.value` is `"0.845"`. `outcomePrices` embeds both `"0.1550"` and `"0.846"`, and its order does not follow `outcomes` (`["No","Yes"]`).
8. **Polymarket `id` vs `marketId`.** `market.id` is a string (`"7974"`). `marketSides[].marketId` is a JSON number (`7974`).
9. **Polymarket numeric fields the schema marks `format: decimal`.** `feeCoefficient`, `orderPriceMinTickSize`, and `minimumTradeQty` arrived as JSON numbers (`0.095`, `0.001`, `1`), not decimal strings. Book prices and qtys are strings.
10. **Polymarket volume fields omitted.** Schema lists `volume` / `volume24hr` / `volume1wk` / `volume1mo` / `volume1yr`. None were present on this active, traded market (the book shows `sharesTraded` `"3626164.0000"`). `rulesDisclaimer` omitted, which the docs allow.
11. **Polymarket book `settlementPx` on an open market.** Present with `SETTLEMENT_PRICE_CALCULATION_METHOD_EVENT_TIER_2` while `state` is `MARKET_STATE_OPEN`. `indicativeOpenPx` and `settlementPreliminaryFlag` from the schema were absent. Extra live stats vs the fields highlighted in the overview example: `currentPx`, `lastPriceSample`, `settlementPriceCalculationMethod`, and the `*SetTime` timestamps (nanosecond precision; the overview example uses millisecond `2025-01-20T12:30:45.123Z`).
12. **Polymarket market object has fields not in the published Market property list used here:** `eventSubcategory`, `politics` (null), `singlesRfqConfig`. `sportsMarketType` value `"futures"` is not one of the `SPORTS_MARKET_TYPE_*` enum values; that enum is on `sportsMarketTypeV2`.
13. **Book overview sample vs live qty.** [Markets API Overview](https://docs.polymarket.us/api-reference/market/overview) shows `qty` like `"750"` and prices like `"0.54"`. This book uses four-decimal qty `"685.0000"` and four-decimal prices `"0.1550"`.
14. **Two status enums.** Market resource uses `MARKET_STATUS_OPEN`. Book and WS market data use `MARKET_STATE_OPEN`. `ep3Status` uses a third spelling, `OPEN`.
