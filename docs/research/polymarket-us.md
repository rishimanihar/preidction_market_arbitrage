# Polymarket US API (polymarket.us / QCX)

> Live API values in examples were replaced with synthetic values; formats and field names are accurate.

**Researched:** 2026-11-12, about 01:40–02:00 UTC (2026-11-11 evening America/Chicago).  
**Scope:** The CFTC-regulated US venue. Retail public host `https://gateway.polymarket.us`. Retail authenticated host `https://api.polymarket.us`.  
**Method:** Official Markdown docs from `https://docs.polymarket.us/llms.txt`, the Participant Agreement and Exchange Rulebook PDFs, PyPI, the official Python SDK source for release 2.4.1, and unauthenticated GETs. User-Agent `pmarb-research/0.1`. No account, no orders.  
**Raw pages:** `sources/`. Live JSON: `sources/live/`.

Claim labels: **VERIFIED** (read in a primary doc or seen in a live response), **INFERRED**, **UNKNOWN**.

This report is about exchange mechanics for an observation tool. It is not a recommendation to trade.

## Do not use the global Polymarket stack

Polymarket US docs identify a separate international product. **VERIFIED:** [What is Polymarket US?](https://docs.polymarket.us/getting-started/what-is-polymarket-us.md) says Polymarket (international) is the crypto product and Polymarket US is the fiat DCM/DCO. The welcome page links international docs at `https://docs.polymarket.com`. **VERIFIED** on PyPI: `py-clob-client` 0.34.6 is “Python client for the Polymarket CLOB” and its homepage is `https://github.com/Polymarket/py-clob-client`. That package, `clob.polymarket.com`, Gamma, and Polygon wallets are the global venue.

The US retail SDK is a different package: `polymarket-us` / import `polymarket_us`. See section 9.

Three US surfaces still get mixed up inside `docs.polymarket.us`:

| Surface | Host | Auth | What the plan should call it |
|---|---|---|---|
| Retail read | `https://gateway.polymarket.us` | none | Public discovery and books |
| Retail trade | `https://api.polymarket.us` | Ed25519 headers | Orders, portfolio, both WebSockets |
| Exchange / institutional | `https://api.prod.polymarketexchange.com`, preprod `https://api.preprod.polymarketexchange.com`, gRPC, FIX | Auth0 private-key JWT, scopes, often `x-participant-id` | A different integration. Preprod exists here only |

**VERIFIED** hosts: [API introduction](https://docs.polymarket.us/api-reference/introduction.md), [Environments](https://docs.polymarket.us/trader-guide/environments.md). The legal entities are **QCX LLC** d/b/a Polymarket US (DCM) and **QC Clearing LLC** d/b/a Polymarket Clearing (DCO). **VERIFIED:** [End-user legal agreements](https://docs.polymarket.us/partners/onboarding/legal-agreements.md).

## Where plan.md is wrong, outdated, or missing

`plan.md` is right about the retail split (`gateway` public, `api` authenticated), one YES instrument, NO as a collateralized short, NO acquisition at `1 − YES bid` with the bid size, authenticated full market-data (not the lite feed), the published casing conflict, the absence of a documented sequence-gap protocol, taker fee `Θ × C × p × (1 − p)` with Θ from 0.095 to 0.0695, half-even cents, a cumulative order cap, onboarding exceptions, and “settlement docs do not give a payout deadline.”

These plan statements need a change:

1. **Rate limit is not a single 25/s IP budget.** The page the plan cites does say 25 requests/s per source IP and Cloudflare location ([Rate Limits](https://docs.polymarket.us/api-reference/rate-limits.md)). The orders overview still says **20 requests/s per API key** ([Orders API Overview](https://docs.polymarket.us/api-reference/orders/overview.md)). The price-history page says **20 requests/s per IP** for the public limit and links that same rate-limits page ([Get Price History](https://docs.polymarket.us/api-reference/price-history/get-price-history.md)). Institutional public REST is another 20/s per IP, and authenticated `GetOrderBook` is **12 requests/minute per firm** ([Trader guide rate limits](https://docs.polymarket.us/trader-guide/rate-limits.md)). **WRONG / INCOMPLETE** to implement only “25/s.”

2. **Short buying power is `q × (1 − p)`, with `p` the YES sale price.** At $0.50 the plan’s “100 × $0.50” matches. At any other price it does not. Selling YES at $0.40 consumes $0.60 of buying power per contract. **VERIFIED** formula in [Collateral and Margin](https://docs.polymarket.us/market-structure/collateral-and-margin.md). The architecture section of the plan already says NO cost is `1 − YES bid`. The worked example should say that explicitly so a later change of the 0.50 price does not get coded as “consumption equals the YES sale price.”

3. **`market.feeCoefficient` is not the account’s Θ.** Every market in the 7-day end-date pull (3,784) and every market in the first 40,000 open rows carried `feeCoefficient: 0.095`. **VERIFIED** live. The account coefficient depends on trailing volume and the onboarding window. A new account’s taker coefficient is **0.0695 for ET days 1–7**, not 0.095 ([fees.md](https://docs.polymarket.us/fees.md)). The plan’s $2.38 fee is the tier-1 number after that window. Using the market field as “verified account Θ” would be wrong.

4. **`active=true` is not “open for trading.”** `GET /v1/markets?limit=1&active=true` returned a `MARKET_STATUS_RESOLVED`, `closed: true` NFL market from 2025. `status=MARKET_STATUS_OPEN` was ignored and also returned resolved markets. **VERIFIED** live. Discovery has to filter on `closed=false` and then on `status`.

5. **There is no retail sandbox.** Preprod is `https://api.preprod.polymarketexchange.com` with Auth0, and credentials come from a Polymarket contact ([Environments](https://docs.polymarket.us/trader-guide/environments.md)). **MISSING** from the plan. The retail hosts have no documented test environment.

6. **Retail API keys are self-serve after KYC, and the customer agreement also demands written production approval.** See sections 1 and 11. The plan’s “if credentials are unavailable, ship a public REST scanner” still holds. It should not assume a developer-portal key is permission to run unattended production orders.

7. **WebSocket “full market data” is documented as the top of book, with no depth count, no sequence number, and no delta opcode.** The plan’s “replace the local book on each complete message” matches the shape of the published example and is what an implementer should do until a captured frame proves otherwise. It is **UNKNOWN** whether that message is the entire ladder. Institutional REST L2 defaults to 3 levels and caps at 10 ([Order Book API Overview](https://docs.polymarket.us/institutional/orderbook/overview.md)).

8. **`transactTime` is the last book change, and it can be hours old on a book just fetched.** The 3x3 basketball book fetched around 2026-01-01T02:00:00Z had `transactTime` e.g. `2026-01-01T00:00:00.123456789Z` and `sharesTraded` zero. Applying the plan’s two-second maximum age to `transactTime` would mark a quiet, successfully fetched book as stale. Age the receipt of the snapshot. Store `transactTime` beside it.

9. **Contract `endDate` is not event time and not payout time.** A basketball market whose question says the game is 2026-01-02 16:00 UTC has `endDate` e.g. `2026-01-05T16:00:00Z`. US Open concession markets have `gameStartTime` e.g. `2026-01-01` and `endDate` `2026-04-01`. **VERIFIED** live. The plan already refuses to treat end time as payout time. It should also refuse to treat `endDate` as “the game is this week.”

10. **Standard maker economics are rebates, not fees, and only above $10M trailing maker volume.** Combo maker fees (`0.03 × C × p × (1 − p)`) start **2026-11-14 00:00 ET**. Combo taker fees changed **2026-11-11 13:00 ET**. **VERIFIED:** [fees.md](https://docs.polymarket.us/fees.md). The plan excludes combos from the first build; the fee module still has to reject combo symbols rather than price them with the standard formula.

11. **Thursday maintenance is a hard collection gap.** 06:00–08:00 ET, resting orders canceled, API returns 503, books reopen empty ([General FAQs](https://docs.polymarket.us/faqs/general-faqs.md), [Trading Hours](https://docs.polymarket.us/learn/trading/access-and-limits/trading-hours.md)). **MISSING** from the plan.

12. **Sports exceptions pay $0.50 or last fair market price.** A US pair that assumes $1/$0 complementarity fails on postponement, cancellation, ties, and several league-specific cases ([Sports FAQs](https://docs.polymarket.us/faqs/sports-faqs.md), [Market Rules](https://docs.polymarket.us/learn/advanced/rule-structure.md)). The plan’s exception table is the right idea; the US catalog is more specific than a generic void.

13. **Order price is always the YES price, including `ORDER_INTENT_BUY_SHORT`.** Desired NO price `X` is sent as `price.value = 1 − X`. **VERIFIED:** [Orders API Overview](https://docs.polymarket.us/api-reference/orders/overview.md). The plan says to map intent and native price. The mapping is this formula.

14. **`DAY` time-in-force is not currently canceling at 17:00 ET.** The orders overview says to use `TIME_IN_FORCE_GOOD_TILL_DATE` until that is fixed. **MISSING** for the later execution milestone.

15. **Official SDK 2.4.1 does not expose `outcomeSide`/`action` or the `idempotencyKey` header** that current order docs describe for safe retries. Pinning the SDK does not pin the wire contract.

## 1. Who can use it

**Retail trading account.** **VERIFIED** [Create an Account](https://docs.polymarket.us/learn/get-started/signup.md): Google or Apple sign-up, date of birth, phone, legal name, residential address, Social Security number, then usually instant KYC, otherwise a government ID and selfie. KYC is required before deposit or trade. The account must belong to a natural person at least 18 and the age of majority. **VERIFIED** Participant Agreement §II.1 (PDF effective 2026-08-06, saved under `sources/legal/`).

**Geography.** The agreement does not say “US persons only.” It bars OFAC comprehensively sanctioned jurisdictions (Cuba, Iran, North Korea, Crimea, Donetsk, Luhansk), FATF black and grey lists, and countries that ban retail products comparable to these contracts. **VERIFIED** §II.8. The marketing page says the product is “Built for US residents” ([What is Polymarket US?](https://docs.polymarket.us/getting-started/what-is-polymarket-us.md)). **UNKNOWN** whether a person without an SSN can finish KYC. Signup collects an SSN, which is US-shaped, and the agreement contemplates a country of residence.

**API keys.** **VERIFIED** [Authentication](https://docs.polymarket.us/api-reference/authentication.md) and [Quickstart](https://docs.polymarket.us/getting-started/quickstart.md):

1. Install the Polymarket US iOS app (`https://apps.apple.com/us/app/polymarket/id6648798962`).
2. Finish identity verification. The docs say trading and API access both wait on approval.
3. Sign in at `https://polymarket.us/developer` with the same method used in the app (Apple, Google, or email). Switching methods can break key access.
4. Create a key. The response is a Key ID and a secret shown once.

There is no documented retail scope checklist and no documented API waitlist. The same page says an **invite code for the app** may be required: email `support@polymarket.us`. **INFERRED:** app access can be gated; once the app account is approved, key creation is self-serve in the developer portal. The portal HTML fetched here is a Next.js shell, so the key form itself was not executed. **UNKNOWN** whether the portal can mint a read-only key. The auth doc describes one key that signs trading, portfolio, and WebSocket calls.

**Institutional and data access are separate.** Individual traders are told to use the retail quickstart, not the institutional application ([Trader onboarding](https://docs.polymarket.us/trader-guide/onboarding.md)). Firms apply at `https://institutional.polymarketexchange.com/register`; review is described as 1–3 business days. Read-only exchange data is a Market Data Agreement via `data@polymarket.us`, with RSA public keys and scopes `read:marketdata`, `read:l2marketdata`, and `read:instruments` ([Data onboarding](https://docs.polymarket.us/data-guide/onboarding.md)). Those scopes do not require participant KYC for reference data and, per the order-book overview, for L2/BBO. **VERIFIED** as documentation. This path was not called.

**Environments.** Retail docs publish only production gateway and `api.polymarket.us`. Exchange preprod and prod are in the table above. **VERIFIED** `GET https://gateway.polymarket.us/v1/health` returned `{"status":"ok"}` with no credentials. The published health OpenAPI is a different object: `GET /v1/health` on `https://api.prod.polymarketexchange.com` with bearer auth ([Health check](https://docs.polymarket.us/api-reference/health/health-check.md)).

## 2. Authentication

Retail authenticated calls use three headers. **VERIFIED** [Authentication](https://docs.polymarket.us/api-reference/authentication.md) and the orders OpenAPI security scheme:

| Header | Value |
|---|---|
| `X-PM-Access-Key` | Key ID |
| `X-PM-Timestamp` | Unix milliseconds, as a string |
| `X-PM-Signature` | Base64 Ed25519 signature |

**Payload:** `f"{timestamp}{METHOD}{path}"`. Method is the HTTP verb (`GET`, `POST`). Path examples are `/v1/portfolio/positions` and, for WebSocket, `/v1/ws/markets` or `/v1/ws/private`. The timestamp must be within **30 seconds** of server time. The documented payload does not include the body or the query string.

The official SDK signs the same string and passes query parameters separately (`polymarket_us/auth.py`, `polymarket_us/client.py`). **VERIFIED** in source. **UNKNOWN** whether a server would reject a signature that also covered the query. Follow the SDK: sign the path only.

Secret handling. **VERIFIED** in the doc snippet and the SDK:

- The secret is base64.
- Ed25519 uses a 32-byte seed.
- If the decoded secret is 64 bytes, use the first 32. The doc snippet always slices `[:32]`. The SDK slices only when the length is 64.

No retail scope parameter is documented. Institutional JWT scopes are a different system (`read:marketdata`, and `permission denied: missing required scope …` on gRPC). **VERIFIED:** [Streaming authentication](https://docs.polymarket.us/streaming-endpoints/authentication.md).

Minimal signing, as published (cryptography library). The SDK does the same sign with PyNaCl `SigningKey`:

```python
import base64, time
from cryptography.hazmat.primitives.asymmetric import ed25519

def auth_headers(key_id: str, secret_b64: str, method: str, path: str) -> dict[str, str]:
    seed = base64.b64decode(secret_b64)
    if len(seed) == 64:
        seed = seed[:32]
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed)
    timestamp = str(int(time.time() * 1000))
    message = f"{timestamp}{method}{path}"
    signature = base64.b64encode(private_key.sign(message.encode())).decode()
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": timestamp,
        "X-PM-Signature": signature,
        "Content-Type": "application/json",
    }
```

WebSocket uses the same headers on the handshake, method `GET`, path `/v1/ws/markets` or `/v1/ws/private`. **VERIFIED:** [WebSocket overview](https://docs.polymarket.us/api-reference/websocket/overview.md). That page’s “see authentication” link points at `https://docs.polymarket.us/api/authentication.md`, which is not the live doc path. The live path is `/api-reference/authentication`.

## 3. REST

Public base `https://gateway.polymarket.us`. Authenticated base `https://api.polymarket.us`. **VERIFIED** introduction and live GETs (HTTP 200, Cloudflare).

### Markets

`GET /v1/markets` query parameters in the OpenAPI include `limit`, `offset`, `orderBy`, `orderDirection`, `id`, `slug`, `archived`, `active`, `closed`, volume bounds, `startDateMin`/`Max`, `endDateMin`/`Max`, `categories`, `sportsMarketTypes`, `marketTypes`, `includeHidden`, `tagIds`. **VERIFIED** schema: [Get Markets](https://docs.polymarket.us/api-reference/markets/get-markets.md). The response is `{ "markets": [ Market, ... ] }`. There is no `nextCursor` and no total count.

Live behavior on 2026-11-12:

| Parameter | Result |
|---|---|
| `active=true` alone | Included resolved, closed markets |
| `active=true&closed=false` | Usable open-ish set. Still contained 19 `MARKET_STATUS_RESOLVED` rows inside the first 40,000 |
| `categories=politics` (and crypto, sports, culture) | Filtered to that `category` string |
| `categories=weather` at a later check | Empty list |
| `endDateMin` + `endDateMax` | Filtered. Used for the 7-day table |
| `status=MARKET_STATUS_OPEN` | Ignored. Returned resolved markets |
| `sportsMarketTypes=SPORTS_MARKET_TYPE_MONEYLINE` | Ignored. First rows were crypto |
| `orderBy=endDate&orderDirection=asc` | Same first slugs as the unsorted call (2027 MLS futures). Sort was not applied on that probe |

Pagination that completed: `limit=100` until a short page. The 7-day query ended with 84 rows on the last page. The unfiltered open list was still full at offset 39,900; a later binary search put the length near 78,935. **VERIFIED.** Callers stop on a short page. They cannot assume a maximum offset.

`GET /v1/market/slug/{slug}` returns `{ "market": Market }`. **VERIFIED** schema and one live fetch.

Market fields that matter for pairing (schema **VERIFIED**, values **VERIFIED** where a live body is cited):

| Field | Role |
|---|---|
| `id`, `slug` | Identifiers. Slug is the book and order key |
| `question`, `title`, `titleShort`, `subtitle` | Display. `title` was null on several live markets; `question` was populated |
| `description` | The rules text. See the Eagles and BTC examples below |
| `rulesDisclaimer`, `rulesDisclaimerPopup` | Short UI disclaimer. Null on the resolved NFL sample |
| `category` | String. Observed values in section 10 |
| `status` | `MARKET_STATUS_UNSPECIFIED`, `OPEN`, `CLOSED`, `RESOLVING`, `HALTED`, `RESOLVED`. Schema says unrecognized values are treated as unspecified, and pre-trading upstream states show up as unspecified. `RESOLVING` means past expiry, final settlement not published. `RESOLVED` means final settlement published |
| `active`, `closed`, `archived`, `hidden` | Booleans. Do not trust `active` alone. `archived` is deprecated in the schema |
| `ep3Status` | Raw upstream state, deprecated. Live resolved NFL market: `EXPIRED` while `status` was `MARKET_STATUS_RESOLVED` |
| `endDate`, `startDate` | Strings. Not a payout time. Often not the event start |
| `gameStartTime` | Present and deprecated. Can be months before `endDate` |
| `orderPriceMinTickSize` | Number. Live values in one 40,000-row slice: `0.01` (36,464), `0.005` (2,619), `0.001` (891), `0.0025` (26) |
| `minimumTradeQty` | Number. Same slice: `0.01` (39,788), `1` (212). Docs: 0.01 means 1% of a contract |
| `feeCoefficient` | Number. 0.095 on every checked open market. Not the account tier |
| `outcomes`, `outcomePrices` | Deprecated JSON strings. Live NFL: `["Packers","Eagles"]` and `["1","0"]` |
| `marketSides[]` | Two sides on every open market in the 40,000-row slice. `long: true/false`, `description` (“Yes”/“No” or a team name), `identifier` (the market slug on the samples), `marketSideType`, `price`, `tradable` |
| `sportsMarketType` | Free string on the wire (`moneyline`, `futures`, `table_tennis_match_winner`, …). The enum `sportsMarketTypeV2` is deprecated |
| `marketType` | Deprecated. Live values included moneyline, futures, props, totals, spreads, drawable_outcome |
| `assetPriceTerms` | Crypto/commodity reference terms. Null on non-asset markets. `windowEnd` is the scheduled settlement instant for those markets; `ONE_TOUCH` may settle Yes earlier. Schema says use `windowEnd` rather than `endDate` for that settlement time |
| `line`, `tags`, `subject`, `comboEnabled` | Structure and UI. Combos are out of the first build |
| `metadata` | Schema says the shape is not a public contract |
| `bestBidQuote`, `bestAskQuote` | Amounts on the market object when present. The list payload used for the census often omitted them |
| `volume`, `volume24hr`, `volume1wk`, `volume1mo`, `volume1yr` | Schema fields. Null on the resolved NFL sample |

Live extra keys not in the OpenAPI component read for this report: `politics`, `singlesRfqConfig`. **VERIFIED** on `sources/live/markets_limit1.json`. Treat the published schema as behind the gateway.

`marketSides[].marketSideType` includes `MARKET_SIDE_TYPE_ERC1155` in the US OpenAPI. That name is the global conditional-token model. Live sides checked were `MARKET_SIDE_TYPE_INSTRUMENT`. **VERIFIED** schema vs live. Do not branch into ERC-1155 token ids because the enum exists.

### Events, search, books, BBO, settlement, history

- `GET /v1/events`, `GET /v1/events/slug/{slug}`, `GET /v1/events/id/{id}`. Offset pagination. An event nests `markets`. One live event had `description` equal to the title, while the nested market `description` held the rules. Event fields `payoutEstimateTime` and `payoutEstimateMessage` exist and were empty strings on three open events. `insiderTradingRules` exists; its contents were not copied for this report. **VERIFIED** [Get Events](https://docs.polymarket.us/api-reference/events/get-events.md) and `sources/live/events_limit1.json`.
- `GET /v1/search?query=`. Page parameter is `page`, not `offset`. **VERIFIED** schema. A live `query=nfl&limit=1` returned 200.
- `GET /v1/markets/{slug}/book` → `{ "marketData": { marketSlug, bids, offers, state, stats, transactTime } }`. **VERIFIED** schema and two live books.
- `GET /v1/markets/{slug}/bbo` → `marketData` of type `MarketDataLite`: `currentPx`, `lastTradePx`, `settlementPx`, `bestBid`, `bestAsk`, `bidDepth`, `askDepth`, `bidShares`, `askShares`, `longQuote`, `shortQuote`, `state`, and other stats. **VERIFIED.**
- `GET /v1/markets/{slug}/settlement` → `{ "slug", "settlement" }` with `settlement` a JSON number. Live resolved Eagles market: `{"slug":"aec-nfl-phi-gb-2025-11-10","settlement":1}`. The book for that slug had empty ladders, `state: MARKET_STATE_EXPIRED`, `settlementPx.value: "1.0000"`, `settlementPriceCalculationMethod: SETTLEMENT_PRICE_CALCULATION_METHOD_EVENT_TIER_1`. **VERIFIED.** Schema: 404 when not found or not settled.
- `GET /v1/price-history?symbol={slug}&fixedInterval=INTERVAL_1H&fidelity=1`. `symbol` is the slug. History points are `{ timestamp, longPrice, shortPrice }` with **JSON numbers**, not decimal strings. Docs: `longPrice` is normally the best ask and `shortPrice` is normally `1 − best bid`; they can sum to more than 1 because they keep the spread. **VERIFIED** doc and a live hour that showed e.g. `longPrice: 0.40`, `shortPrice: 0.70` while the book was bid 0.30 / ask 0.40. These are display prices, not trades. There is no public trades list: `GET /v1/markets/{slug}/trades` returned **404**.

### Book shape (live)

BTC market `cpc-btc-hit-h-w-2026-11-13-85000` at fetch time:

- Bids: one level, `px.value` `"0.2000"`, `qty` `"2678.5000"`.
- Offers: `"0.3000"` qty `"1500.2500"`, then `"0.4000"` qty `"500.7500"`.
- Best bid is the first bid. Best offer is the first offer (lowest ask). The second book, `aec-bskt3x3-run-cap-2026-11-12`, confirmed the sort with three levels: bids `0.4000, 0.3000, 0.2000` and offers `0.6000, 0.7000, 0.8000`.
- Prices and sizes are **strings**. Sizes can be fractional. Prices on the wire used four decimal places even when the tick is 0.01.
- `transactTime` uses nine fractional digits (e.g. `2026-01-01T00:00:00.123456789Z`). Python 3.12 `datetime.fromisoformat` accepts that string and truncates to microseconds (`…123456`). Keep the raw string if those digits matter.
- `state` is a string enum: `MARKET_STATE_OPEN`, `PREOPEN`, `SUSPENDED`, `HALTED`, `EXPIRED`, `TERMINATED`, `MATCH_AND_CLOSE_AUCTION`. The SDK type also lists `MARKET_STATE_CLOSED`, which is not in this OpenAPI enum.
- BBO `bidShares` / `askShares` are rounded down to two significant figures. Schema text **VERIFIED**. On the BTC book, e.g. bid qty `2678.5000` became `bidShares` `"2600"`, and ask quantities summing to 2001 became `"2000"`. `bidDepth` is a level count. Use the book quantities for size.
- On that same BBO, `longQuote` was `0.3000` (the best ask) and `shortQuote` was `0.80` (`1 − 0.20` best bid). `currentPx` was `0.2500`. The empty basketball book’s `currentPx` was `0.5000` with a 0.40 / 0.60 spread and zero trades. `currentPx` is not an executable price.

The BTC offers are a real tight spread, not a mirror of the bids. The basketball offers happened to be the complements of the bids with the same sizes. Both shapes occur. Build YES asks from `offers` and NO asks from `bids` via `1 − px`. Do not add the two representations together.

`stats` on the book can include `openPx`, `highPx`, `lowPx`, `lastTradePx`, `lastTradeQty`, `sharesTraded`, `openInterest`, `notionalTraded`, `currentPx`, `lastPriceSample` (`longPx`, `shortPx`, `ts`), and many `*SetTime` fields. Several were absent on the quiet basketball book.

## 4. Market structure

**One instrument per market.** Buying it is YES. Selling it without a long is a short and is the economic NO. **VERIFIED:** [Orders & Trading](https://docs.polymarket.us/concepts/orders.md), [Market Structure](https://docs.polymarket.us/learn/trading/basics/buying-yes-vs-selling-no.md). There are no YES and NO tokens on this venue.

Team markets still have two labeled sides. The orders overview says the first team in the slug is the long/YES side and the second is the short/NO side. The resolved slug `aec-nfl-phi-gb-2025-11-10` had `long: true` on “Eagles,” matching `phi` first. **VERIFIED** for that market. Crypto markets label the sides “Yes” and “No” and share one `identifier`. Prefer `marketSides[].long` over slug order. A draw is often a third market (`drawable_outcome` / winner-with-tie), not the short of one team. Shorting “Team A wins” is “Team A does not win,” which includes a draw when the rules allow one.

### Collateral

Fully collateralized. No margin calls. **VERIFIED** [Collateral and Margin](https://docs.polymarket.us/market-structure/collateral-and-margin.md).

For quantity `q` at YES trade price `p` (dollars per contract, before fees):

| Position | Cash | Margin locked | Buying power change |
|---|---|---|---|
| Buy YES (long) | pay `q × p` | 0 | `− q × p` |
| Sell YES (short) | receive `q × p` | `q × $1` | `− q × (1 − p)` |

The short’s maximum loss is `q × (1 − p)`. The locked margin is the full $1 payout, not the max loss. Sale proceeds increase fiat balance and fund part of that $1. Worked in the doc at $0.60: proceeds +$0.60, margin $1.00, buying power −$0.40. At $0.40 the same page shows the seller’s buying power −$0.60.

So the NO leg of the arb, hitting a YES bid at `p`, consumes `1 − p` dollars of buying power per contract. That equals the NO acquisition price. Terminal value of a completed pair is still $1 per contract pair when both rules pay $1/$0 on the same outcome. Do not add the released margin again as profit.

A trade that would take buying power below zero fails. **VERIFIED.**

Open orders are risk-checked **per instrument**, not against the sum of worst-case losses across the account. With $10 buying power, a $10 order on instrument A and a $10 order on instrument B can both be accepted. Within one instrument, the charge is the worse of “all bids fill” and “all offers fill,” with credit for orders that close an existing position. A later fill can cause **unsolicited cancels** of resting orders on other instruments. Those cancels arrive on the order stream. Executed trades are not unwound for collateral. **VERIFIED** same page. The collector’s own cash ledger should still reserve the full worst case of its outstanding orders. The exchange will not do that across markets.

Portfolio margin can reduce requirements for mutually exclusive or directional **positions**. It does not change the per-instrument open-order check. **VERIFIED.** The exact portfolio formulas are in the mutually-exclusive and directional pages. They matter after a one-sided fill, not for the first observation milestone.

### Settlement, cancellation, void

Learn pages: winning contracts settle at $1 and losing contracts at $0; Polymarket Clearing updates balances automatically after the outcome is finalized; some markets have alternative terms in the rules, including $0.50 or last fair market price; resolutions are described as final under the rulebook. **VERIFIED:** [Market Settlement](https://docs.polymarket.us/learn/markets/contract-settlement.md), [Market Resolution](https://docs.polymarket.us/learn/markets/market-resolution.md), [Market Rules](https://docs.polymarket.us/learn/advanced/rule-structure.md).

No numeric payout SLA is published. Event `payoutEstimateTime` was empty on the samples. The settlement REST body is a price, not a timestamp.

The institutional settlement read says a 200 with stats absent means the newest record is not a final `EVENT_TIER_1` settlement, and “later reads can reflect corrections.” **VERIFIED:** [Get instrument settlement](https://docs.polymarket.us/api-reference/settlement/get-instrument-settlement.md). That sentence sits next to the learn-page claim that resolutions are final. Treat a published `EVENT_TIER_1` / retail `settlement` value as the current official number, and keep watching it until the position balance actually changes. **UNKNOWN** how often a correction happens.

Sports rules that break a $1/$0 hedge, all **VERIFIED** in [Sports FAQs](https://docs.polymarket.us/faqs/sports-faqs.md):

- Winner without ties: a tie settles at **$0.50** (example: NFL ends tied).
- Winner with a draw market: exactly one of three contracts pays $1, the others $0. Soccer league matches use regulation plus stoppage, not extra time.
- Postponed before play and rescheduled before expiration (described as typically two weeks after the original event date): the rescheduled result counts. If it falls outside expiration, contracts settle at **last fair market price** (LFMP). Postponement does not automatically move the contract.
- Soccer home/away reversal on a reschedule: original contract settles at LFMP.
- Canceled and not replayed, abandoned, or no official result by expiration: LFMP, or $0.50 where a league section says so (ITF tennis is called out as $0.50; other tennis pre-serve cancellations are LFMP; one section treats tie, draw, no-result, and abandonment as $0.50).
- Co-winners: $1 divided by the number of winners, rounded down to the tick; others $0.
- LFMP is the fair price at the announcement, not the last trade, and not trades after the announcement.

A live NFL `description` said a full cancellation with no makeup game resolves **50-50**, and a postponement keeps the market open until the game is completed. That text is the contract, and it can differ from the FAQ’s “typically two weeks” sentence. Hash `description` (and any event rules field that is non-empty) into the pair approval.

Instrument lifecycle on the exchange API, useful when a retail `status` is unspecified: `PENDING → OPEN → CLOSED → EXPIRED → TERMINATED`, plus `SUSPENDED` (cancels allowed, no matching) and `HALTED` (cancels also blocked). `TERMINATED` removes the book and closes positions. **VERIFIED** [Order Book API Overview](https://docs.polymarket.us/institutional/orderbook/overview.md).

## 5. WebSocket

**VERIFIED** URLs:

- `wss://api.polymarket.us/v1/ws/markets`
- `wss://api.polymarket.us/v1/ws/private`

Both require the Ed25519 handshake. Public gateway has no documented market-data socket. These sockets were not opened for this research (they need a key). Everything below is docs plus SDK source.

### Casing and enums disagree

[WebSocket overview](https://docs.polymarket.us/api-reference/websocket/overview.md) says all messages are **snake_case**, and its subscribe example uses numeric `subscription_type` (`1`, `2`, `3`) and `request_id` / `market_slugs`.

[Markets WebSocket](https://docs.polymarket.us/api-reference/websocket/markets.md) and [Private WebSocket](https://docs.polymarket.us/api-reference/websocket/private.md) use **camelCase** and string enums: `requestId`, `subscriptionType: "SUBSCRIPTION_TYPE_MARKET_DATA"`, `marketSlugs`.

The SDK sends the camelCase string form and parses camelCase keys (`marketData`, `marketDataLite`, `trade`, `heartbeat`, `orderSubscriptionSnapshot`, …). For private messages it also accepts a few alternate names (`ordersSnapshot`, `orderUpdate`, `positionUpdate`, `accountBalanceUpdate`). It does not parse the overview’s snake_case book fields. **VERIFIED** `polymarket_us/websocket/base.py`, `markets.py`, `private.py`. **UNKNOWN** which form the server emits until a frame is captured. The plan’s instruction to reject an unsupported schema is the right gate. Prefer the channel pages and the SDK as the client contract, and keep the overview’s snake_case parser only if a frame shows it.

### Channels

Markets:

| String enum | Docs | SDK event |
|---|---|---|
| `SUBSCRIPTION_TYPE_MARKET_DATA` | Full order book and stats. “Top levels.” Sort best to worst. `px` + `qty`. Qty may be decimal | `market_data` |
| `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` | BBO-style: `currentPx`, `lastTradePx`, `bestBid`, `bestAsk`, depths, `sharesTraded`, `openInterest` | `market_data_lite` |
| `SUBSCRIPTION_TYPE_TRADE` | Trade print with maker/taker `side` and `intent` | `trade` |

The example book uses `bids` / `offers`, `px: {value, currency}`, `qty` as a string, `state: "MARKET_STATE_OPEN"`, `transactTime`. That matches REST camelCase, not the overview.

Private:

| String enum | Role |
|---|---|
| `SUBSCRIPTION_TYPE_ORDER` | Snapshot (`orderSubscriptionSnapshot.orders`, `eof: true`) then `orderSubscriptionUpdate.execution` |
| `SUBSCRIPTION_TYPE_ORDER_SNAPSHOT` | Listed on the private page. The overview’s numeric table does not include it. The SDK subscribes with `SUBSCRIPTION_TYPE_ORDER` |
| `SUBSCRIPTION_TYPE_POSITION` | `beforePosition` / `afterPosition`. Decimal fields (`netPositionDecimal`, …) are authoritative; older integer fields are rounded |
| `SUBSCRIPTION_TYPE_ACCOUNT_BALANCE` | Balance snapshot then updates |
| `SUBSCRIPTION_TYPE_RFQ` | RFQ lifecycle. Not needed for the first book recorder |

Overview numeric private types are only 1 ORDER, 3 POSITION, 4 ACCOUNT_BALANCE. The channel page’s string list is wider. Another docs conflict.

### Semantics the recorder needs

- **Heartbeat.** Server sends `{ "heartbeat": {} }`. Docs: “Clients should respond to heartbeats or implement their own keep-alive mechanism.” No response body is specified. The SDK emits a `heartbeat` event and does not reply. **UNKNOWN** whether silence alone drops the socket, and what a legal reply is. Reconnect if heartbeats stop, as the overview says, and capture whether a reply is required.
- **Snapshot vs delta.** Private orders are explicitly a snapshot (`eof`) and then execution updates. Market data examples are a complete `bids`/`offers` array with no `action`, no level delta, and no sequence. `responsesDebounced: true` batches updates “at regular intervals rather than on every change.” The interval is not specified. **INFERRED** from the example shape: each `marketData` message replaces the levels it contains. **UNKNOWN** until a live frame whether a message can be partial, and how many levels “top” means. The plan should keep “replace, do not patch” and should refuse to qualify a book until the first `marketData` on that connection.
- **Sequence numbers.** None in the retail WS docs or the SDK types. **VERIFIED** as an absence in those sources. Do not invent a gap detector. The institutional gRPC market-data and drop-copy streams are a different protocol and do have resume tokens ([Streaming best practices](https://docs.polymarket.us/streaming-endpoints/streaming-best-practices.md)).
- **Limits.** At most 100 market slugs per subscription; open more subscriptions if needed. **VERIFIED** markets page. The SDK does not enforce it.
- **Reconnect.** Overview and SDK docs: unique request ids, exponential backoff, process in order, resubscribe. Nothing says the server will replay missed market data. After reconnect, drop the previous connection’s book and wait for a new `marketData`. That part of the plan matches the docs. Private order recovery is snapshot-then-delta and can be rebuilt from the snapshot plus `GET /v1/orders/open`.

The quickstart’s “WebSockets” card links to `/streaming-endpoints`, which is institutional gRPC, not `wss://api.polymarket.us`. **VERIFIED** [Quickstart](https://docs.polymarket.us/getting-started/quickstart.md). Follow `/api-reference/websocket/markets` instead.

## 6. Rate limits

Published numbers that an implementer can hit depending on which page they trust:

| Source | Limit | Key |
|---|---|---|
| [Retail rate limits](https://docs.polymarket.us/api-reference/rate-limits.md) | **25 requests/s** general, shared by endpoints other than the exceptions below | Per source IP **and** Cloudflare location. Different API keys on the same IP share the counter |
| Same page | `POST /v1/combos` and `POST /v1/rfqs` together, and several RFQ GETs: **1/s**. Quote create and quote delete: **100/s** each | Separate from the 25/s pool |
| [Orders overview](https://docs.polymarket.us/api-reference/orders/overview.md) | **20 requests/s** “across all endpoints” | **Per API key**, enforced at Cloudflare |
| [Price history](https://docs.polymarket.us/api-reference/price-history/get-price-history.md) | **20 requests/s per IP** “public limit,” and cache identical history responses at least 30 seconds | The sentence links to the 25/s page |
| [Institutional rate limits](https://docs.polymarket.us/trader-guide/rate-limits.md) | Public unauthenticated **20/s per IP**. `GetOrderBook` and `GetBBO` **12/min per firm**. `ListInstruments` **6/min** | Exchange API, per firm, not the retail 25/s |

429 body on the retail page: `{ "status": 429, "message": "Too Many Requests" }`. The institutional page shows `{ "code": 8, "message": "rate limit exceeded", "details": [] }`. Backoff in both retail examples starts at 1 second and doubles.

**Order latency stopgap, not a rate limit.** If a new order or cancel-replace is still unprocessed after 5 seconds, it is rejected with text `Order Processing Max Delay Exceeded` (older text: `Global Rate Limit Exceeded`). Pure cancels are exempt. The docs say to resubmit without treating it as throttling. **VERIFIED** both rate-limit pages.

No `Retry-After` or rate-limit headers were present on the public GETs used here. **VERIFIED** for those responses.

A book poller for ten pairs at 2 Hz is 20 requests/s before metadata refresh. That sits on top of whichever of 20 or 25 is real, and price history adds more. The plan’s “start well below the limit” is the operable rule. A concrete starting budget is in the implementation section. WebSocket market data is the documented way to stay under the cap, and it needs a key.

Higher retail limits are a support request to `support@polymarket.us`, not a self-serve flag. **VERIFIED.**

## 7. Fees

Source: [fees.md](https://docs.polymarket.us/fees.md), fetched 2026-11-12. Standard formula, in force exchange-wide since **2026-10-28 22:00 ET**:

```text
fee = Θ × C × p × (1 − p)
```

`C` is contracts, `p` is the execution price in dollars from $0.01 to $0.99, `Θ` is the coefficient. The formula is symmetric, so a YES buy at `p` and a YES sale at `p` (the NO buy) use the same `p × (1 − p)`. Charging the NO price by mistake does not change this particular formula. It does change buying power.

### How Θ is chosen

Taker fee tier uses taker dollar volume over the preceding **30 completed ET calendar days, excluding today**. Set daily. **VERIFIED.**

| Tier | Trailing 30-day taker volume | Θ |
|---|---|---|
| 1 | Below $10,000 | 0.095 |
| 2 | $10,000 to less than $50,000 | 0.085 |
| 3 | $50,000 to less than $250,000 | 0.075 |
| 4 | $250,000 or more | 0.0695 |

Volume per fill is the dollars paid for the side received, not face value:

```text
buy volume  = C × p
sell volume = C × (1 − p)
```

Combo fills count. Voided fills do not. **VERIFIED.**

**Onboarding.** Day 1 is the ET calendar day of the first fill (maker or taker), with no hourly proration.

- Days 1–7: taker Θ = **0.0695** regardless of volume. Maker and taker **rebate** tiers still use trailing volume.
- Days 8–30: taker fee, maker rebate, and taker rebate use the higher of (first-7-day volume × 30/7) and trailing 30-day volume.
- Day 31 on: trailing 30-day volume only.
- Accounts whose first fill was in the six days before launch keep the remaining window. During November, existing accounts get the better of the trailing-30-day tier and the October-volume tier.
- Manual overrides win.
- **The coefficient is fixed when the order is inserted**, not when it fills.

Table tennis uses this standard Θ. The page says so. Maker rebates use the same maker coefficients on table tennis and crypto.

### Maker rebates and combo fees

Standard makers. Tier 1 (maker volume below $10,000,000): **no rebate**. Tier 2: Θ = −0.0035. Tier 3: −0.0070. Tier 4 ($200,000,000+): −0.0104. Credited at the fill. **VERIFIED.** Baseline P&L should use zero maker rebate unless the account’s tier is known. A negative fee is not cash the arb can count before the fill.

Taker rebates are a weekly percent of taker fees paid (0% below $250k taker volume, then 3/5/10/15/30%). Exclude them from baseline.

Combo taker, fills **from 2026-11-11 13:00 ET**:

```text
fee = C × p × [Θ × (1 − p) + 0.06 × (1 − p)^4]
```

Combo maker, fills **from 2026-11-14 00:00 ET**:

```text
fee = 0.03 × C × p × (1 − p)
```

Before that timestamp, combo maker fee is $0. This research’s clock (2026-11-11 evening ET) is after the taker change and before the maker-fee change. Combos are outside the first binary-pair build. If a slug is combo-enabled, do not price it with the standard formula.

### Rounding and the cumulative cap

**VERIFIED** wording: round half to even, to the nearest $0.01. The page’s examples: $0.025 → $0.02, $0.035 → $0.04. `Decimal` `ROUND_HALF_EVEN` matches those and the published 100-lot table (0.0695 × 100 × 0.50 × 0.50 = 1.7375 → $1.74; 0.0695 × 100 × 0.10 × 0.90 = 0.6255 → $0.63).

When one aggressive order hits several resting orders: each fill is charged its own half-even fee, then reduced if needed so the sum of commissions on that order never exceeds the half-even rounding of the cumulative exact fee. The adjustment only reduces a fill. Maker rebates are per fill and are not part of that cap. The page does not give a multi-fill numeric example. The running-cap reading below is **INFERRED** from that sentence.

Fees are taken from balance at fill. Cancels, expires, and rejects are free. A small fee can round to $0.00. **VERIFIED.**

Execution reports may carry scaled integers (`price_scale`, `fractional_quantity_scale`, `commission_notional_collected`). Retail order responses observed in the schema use decimal `Amount` strings for `commissionNotionalCollected`. Decode with the scale only on the institutional/FIX report path. **VERIFIED** fees.md pointer to the partner data model; the retail create-order schema shows `Amount`.

### Three numeric examples

Arithmetic uses `ROUND_HALF_EVEN` and matches the published table where the inputs are the table’s inputs. Θ is an assumption labeled on each row, not a live account.

**Example A. Tier-1 taker, 100 contracts at $0.50 (the plan’s US fee).**  
Exact `0.095 × 100 × 0.50 × 0.50 = 2.375`. Half-even to the cent: the thousandths digit is 5 and the hundredths digit 7 is odd, so **$2.38**. This is the plan’s figure. It applies after onboarding day 7, at tier 1. It is not the days-1–7 coefficient.

**Example B. Tier-4 taker, 100 at $0.10, and the onboarding equivalent of Example A.**  
`0.0695 × 100 × 0.10 × 0.90 = 0.6255` → **$0.63**. Published table cell.  
The same Θ on Example A’s trade: `0.0695 × 100 × 0.25 = 1.7375` → **$1.74**. That is what a days-1–7 account pays on the plan’s 100-at-0.50 US leg, not $2.38.

**Example C. Cumulative cap, tier-4, two separate 1-contract fills at $0.50 on one aggressive order.**  
Exact per fill `0.0695 × 0.25 = 0.017375` → **$0.02** alone.  
Two fills, exact sum `0.03475` → half-even **$0.03**.  
Charging $0.02 + $0.02 = $0.04 would exceed the cap, so the second fill’s charge is reduced to **$0.01**. Total collected **$0.03**. A calculator that rounds each fill and sums them fails this case.

**Buying power beside Example A, and a non-mid short.**  
Short 100 YES at $0.50: buying power down `100 × 0.50 = $50`. Fee $2.38 at tier 1. Capital for that leg $52.38 before any Kalshi leg.  
Short 100 YES at bid $0.40 (NO costs $0.60): buying power down **$60**. Fee `0.095 × 100 × 0.40 × 0.60 = 2.28` exactly → **$2.28**. Capital for that leg **$62.28**. Using $40 (the sale proceeds) as capital understates the lock by $20.

## 8. Orders (later; not called)

`POST https://api.polymarket.us/v1/orders`. **VERIFIED** [Create Order](https://docs.polymarket.us/api-reference/orders/create-order.md) and [Orders overview](https://docs.polymarket.us/api-reference/orders/overview.md).

Body fields:

| Field | Notes |
|---|---|
| `marketSlug` | Only field in the OpenAPI `required` array |
| `intent` | `ORDER_INTENT_BUY_LONG` buy YES; `SELL_LONG` sell YES / close long; `BUY_SHORT` buy NO; `SELL_SHORT` sell NO / close short |
| `outcomeSide` + `action` | Alternative. If both forms are sent, outcome+action wins. `OUTCOME_SIDE_NO` + `ORDER_ACTION_BUY` = `BUY_SHORT` |
| `type` | `ORDER_TYPE_LIMIT` or `ORDER_TYPE_MARKET` |
| `price` | `{ "value": "<decimal string>", "currency": "USD" }`. **Always the YES price**, for every intent |
| `quantity` | Number. Decimals allowed when `minimumTradeQty < 1` |
| `cashOrderQty` | Amount, for market orders sized in dollars |
| `tif` | `TIME_IN_FORCE_DAY`, `GOOD_TILL_CANCEL`, `GOOD_TILL_DATE`, `IMMEDIATE_OR_CANCEL`, `FILL_OR_KILL` |
| `goodTillTime` | For GTD |
| `participateDontInitiate` | True means post-only; reject if it would take |
| `manualOrderIndicator` | `MANUAL_ORDER_INDICATOR_MANUAL` or `AUTOMATIC`. Prose says required for compliance. OpenAPI does not list it in `required` |
| `synchronousExecution`, `maxBlockTime` | Block until a terminal state, capped at 5 seconds. Default is async. Use async for resting orders |
| `slippageTolerance` | `currentPrice`, `bips`, or `ticks` (ticks win). Market orders default to unlimited slippage |

Price convention, from the USC/Iowa table in the overview: buy the long team at 0.83 → `BUY_LONG` at `0.83`. Buy the short team at 0.83 → `BUY_SHORT` at **`0.17`**. To buy NO at economic price `X`, set `price.value = 1 − X`. A YES bid at 0.60 and a NO bid whose wire price is also 0.60 are the same instrument and the same price; that is the documented self-match.

Absolute price limits are **0.01 to 0.99**. A price outside that range can still return an order id and then reject. Tick and min quantity come from the market (`orderPriceMinTickSize`, `minimumTradeQty`). Extra precision may be normalized (the doc’s example: 0.515 with a 0.01 tick returned as 0.51). Validate locally.

IOC fills what it can and cancels the rest. FOK fills entirely or cancels. Both exist. **VERIFIED.** `DAY` is the one to avoid until the 17:00 ET cancel behavior is fixed; use GTD.

Header `idempotencyKey`: non-empty, at most 255 bytes. Same key replays the recorded response for 24 hours. A concurrent retry can return `ABORTED`. On an ambiguous timeout, retry the **same** key and reconcile with the private order stream and `GET /v1/orders/open` before creating a new key. **VERIFIED** orders overview. This matches the plan’s “no blind retry,” and names the header the plan does not yet name.

Preview is `POST /v1/order/preview`. Close-position is a separate route. Batch create is up to 20 and is rejected entirely on request-shape errors; per-order matching results come on the stream, not in the HTTP body. **VERIFIED** in the orders index descriptions. Not exercised.

`manualOrderIndicator` for a bot is `MANUAL_ORDER_INDICATOR_AUTOMATIC`. Rulebook §5.2(i) requires automated systems to have pre-trade throttles, price collars, and a kill switch (section 11).

## 9. Official Python SDK

| Item | Value | Status |
|---|---|---|
| PyPI name | `polymarket-us` | **VERIFIED** PyPI JSON, version **2.4.1**, upload time `2026-10-08T18:02:32` |
| Import | `polymarket_us.PolymarketUS` | **VERIFIED** `pyproject.toml` and docs |
| Repo | `https://github.com/Polymarket/polymarket-us-python` | **VERIFIED** GitHub API. Default branch `main`, MIT, not archived, pushed `2026-10-09T20:55:22Z`, description “Official Polymarket US Python SDK” |
| Python | `>=3.10`, classifiers through 3.13. 3.12 is listed | **VERIFIED** |
| License | MIT, copyright 2026 Polymarket | **VERIFIED** `LICENSE` |
| Dependencies | `httpx>=0.27`, `pynacl>=1.5`, `websockets>=13` | **VERIFIED** |
| Status classifier | Development Status :: 4 - Beta | **VERIFIED** |
| Maintenance | Last release about five weeks before this research. Docs have since added order fields the SDK types do not contain | **VERIFIED** by diffing source to the orders overview |

Coverage in 2.4.1 source:

- REST resources: events, markets (`list`, `retrieve`, `retrieve_by_slug`, `book`, `bbo`, `settlement`), orders, portfolio, account, series, sports, search, RFQs.
- Sync and async clients. Public calls use `gateway_base_url` default `https://gateway.polymarket.us`. Authenticated calls use `api_base_url` default `https://api.polymarket.us`.
- WebSocket: `client.ws.markets()` and `client.ws.private()`, async only, camelCase subscribe, heartbeat detected and not answered.
- Auth: path-only Ed25519 as in section 2. Header `poly-correlation-id` on HTTP calls.
- Retries: idempotent methods only, on timeout, connection errors, and 408/409/429/5xx. Order placement is not retried. The retry logic does not send `idempotencyKey`.

Wire names the SDK actually sends for orders are the dict keys in `CreateOrderParams`: `marketSlug`, `intent`, `type`, `price`, `quantity`, `tif`, `participateDontInitiate`, `goodTillTime`, `cashOrderQty`, `manualOrderIndicator`, `synchronousExecution`, `maxBlockTime`, `slippageTolerance`. **VERIFIED** `polymarket_us/types/orders.py`. There is no `outcomeSide`, `action`, or idempotency header in that file.

`MarketDetail` in the SDK is a short TypedDict (`slug`, `title`, `outcome`, `active`, `closed`, `liquidity`, `volume`, `eventSlug`, `team`, …). It omits `endDate`, `status`, `description`, `category`, `feeCoefficient`, `orderPriceMinTickSize`, `minimumTradeQty`, and `marketSides`, all of which the live gateway returns. The client still returns the raw JSON. Use the raw body, not the TypedDict, as the field list.

Do not install `py-clob-client` for this venue.

## 10. Market universe (live)

Anchor: **2026-11-12T01:51:31Z**. Open-ish filter: `active=true&closed=false`. Counts are the last offset that still returned a row, plus one, from `limit=1` binary search. The catalog moved during the search: the ten real categories sum to **78,942** and the unfiltered search returned **78,935**. Treat both as a snapshot, off by a handful of listings, not as an exchange census file.

Strings `weather`, `economics`, `metals`, `commodities`, `entertainment`, `news`, and `esports` returned **no** markets on a direct `limit=2` check a few minutes later. A binary search had briefly reported 1 for some of them. Those strings are not categories in this snapshot. Weather-style products showed up under **`climate`**. The docs also have weather, crypto, and metals FAQs; the live category string for metals was not `metals`.

### Open markets by category

| Category | Count (binary search) |
|---|---:|
| sports | 69,120 |
| politics | 4,817 |
| culture | 2,417 |
| crypto | 1,486 |
| macro | 545 |
| technology | 239 |
| climate | 173 |
| finance | 108 |
| geopolitics | 27 |
| science | 10 |
| **Sum of these rows** | **78,942** |
| Unfiltered `active=true&closed=false` | 78,935 |

Sports is the bulk. It is not the only category. Politics, culture, crypto, macro, technology, climate, finance, geopolitics, and science were all non-empty.

Inside the first 40,000 rows of an unordered scan (a truncated slice, useful for field distributions, not for category totals): status was `MARKET_STATUS_OPEN` on 39,981 and `MARKET_STATUS_RESOLVED` on 19; every row had two `marketSides` and a long side; `feeCoefficient` was 0.095 on all 40,000; `minimumTradeQty` was 0.01 or 1; ticks were 0.01, 0.005, 0.001, and 0.0025. `sportsMarketType` had 91 distinct strings in that slice, led by `futures` and football/basketball game markets. That slice is saved as `sources/live/census_summary.json` and `open_markets_compact.json`.

### `endDate` in the next 7 days

Window: `endDate` from **2026-11-12T01:51:31Z** through **2026-11-19T01:51:31Z**, `active=true&closed=false`. Paged to a short page. **3,784 markets, all `MARKET_STATUS_OPEN`.** All of them had tick 0.01, minimum quantity 0.01, and `feeCoefficient` 0.095.

By category:

| Category | Markets |
|---|---:|
| sports | 2,257 |
| crypto | 1,368 |
| climate | 144 |
| finance | 14 |
| politics | 1 |
| **Total** | **3,784** |

By UTC date of `endDate`:

| UTC date | Markets |
|---|---:|
| 2026-11-12 (partial, after 01:51Z) | 854 |
| 2026-11-13 | 616 |
| 2026-11-14 | 1,770 |
| 2026-11-15 | 503 |
| 2026-11-16 | 40 |
| 2026-11-17 | 0 |
| 2026-11-18 | 1 |
| 2026-11-19 (partial, before 01:51Z) | 0 |

Sports in that window, by `sportsMarketType`:

| Type | Markets |
|---|---:|
| `table_tennis_match_total_sets` | 695 |
| `table_tennis_match_winner` | 437 |
| `table_tennis_set_1_winner` | 348 |
| `table_tennis_set_2_winner` | 348 |
| `table_tennis_set_3_winner` | 348 |
| `efootball_team_full_time_winner` | 33 |
| `basketball_team_full_game_winner` | 24 |
| `beach_volleyball_match_winner` | 18 |
| `ebasketball_team_full_game_winner` | 6 |
| **Sports subtotal** | **2,257** |

The 1,374 rows with an empty `sportsMarketType` are the non-sports names (crypto 1,368 plus a few others). `futures` (153) in the type histogram sits on finance, climate, and similar listings, including “How Many Honey Deuces Will Be Sold at the 2026 US Open?” with `gameStartTime` months before its `endDate` (that `endDate` still falls inside this window).

No NFL or NBA `sportsMarketType` appeared in this end-date window. Near-term **games** can still exist with a later contract `endDate`. The basketball sample’s question schedules the game days before the market `endDate` (e.g. game 2026-01-02, `endDate` 2026-01-05), which is why a near-term game can sit inside a short `endDate` window. A contract whose expiry is “about two weeks after the game,” as the sports FAQ describes, will not show up in a 7-day `endDate` filter even when the game is tomorrow.

Compact rows: `sources/live/enddate_7d_compact.json`. Counts: `sources/live/counts.json`.

## 11. Terms restrictions on automated access and data

The customer legal set is five documents. **VERIFIED** [End-user legal agreements](https://docs.polymarket.us/partners/onboarding/legal-agreements.md): Participant Agreement, Exchange Rulebook, Clearinghouse Rulebook, Risk Disclosure, Privacy Policy. Current participant-agreement version string in that page: `08.06.2026`. If the agreement and the rulebook conflict, the rules govern (agreement §XI).

**API access to production.** Participant Agreement §I.19: the participant shall complete Polymarket US conformance tests when connecting via API, and shall not use the system in a live production environment until Polymarket US gives **written approval**. The participant is liable for API implementation errors. **VERIFIED** from the PDF text extracted to `sources/legal/participant-agreement.txt`. The retail quickstart does not mention that approval step. Both documents are official. **UNKNOWN** which one a developer-portal key satisfies. Do not treat “I created a key” as written production approval for order entry. Read-only public gateway GETs do not use a key. The redistribution rule below still applies to information a participant receives through the system.

**Automated trading controls.** Exchange Rulebook §5.2(i), PDF filename dated 2026-09-30: participants using automated trading systems must implement pre-trade risk controls including order throttles, price collars, and kill switches. §5.3(b)–(c): the company may cap orders per second, and anyone submitting orders or cancels, manually or automatically, must prevent excessive messaging. §5.2(h): keep an audit trail that can reconstruct order entry, modification, cancellation, and execution. **VERIFIED** `sources/legal/rulebook.txt`.

**Market data redistribution.** Agreement §I.22: data available via the system is proprietary to PMUS and may be used only as the agreement, the rules, and law permit. It may not be redistributed except as PMUS expressly permits. Rulebook §5.3(g): any person receiving information through the trading system is prohibited from redistributing it unless the company expressly permits. **VERIFIED.** A local observation file used by the same participant is the question the agreement leaves to “as permitted.” Redistributing books, building a vendor feed, or publishing the raw archive is outside what these clauses allow. **INFERRED** that a private local recorder for the account holder is the kind of use the trading system is for; it is not an express license to republish. There is no separate “market data fee” on the retail pages reviewed. The institutional path has a Market Data Agreement instead.

**Other conduct.** Agreement §I.13: PMUS may limit message volume. §I.24: credentials may not be shared; the account holder is responsible for activity under them. Rulebook §5.3(d): no orders without funds to satisfy them. §5.3(j): the company can adjust trade prices or cancel trades after a platform malfunction or an erroneous order. Market-integrity rules bar trading on confidential outcome information and some event-connected people ([Trading Restrictions](https://docs.polymarket.us/learn/trading/access-and-limits/trading-restrictions.md), `https://integrity.polymarket.us/`).

`polymarket.us/terms` and `/legal` returned the marketing site HTML shell, not a separate terms document. The agreement PDF is the terms text used for participants.

## Docs and SDKs that conflate products

- **Global vs US.** `py-clob-client`, `docs.polymarket.com`, CLOB, Gamma, Polygon. US welcome page also says “redeem winning positions,” which is global-token language. US settlement is a balance update by the clearinghouse.
- **US schema leftover.** `MARKET_SIDE_TYPE_ERC1155` on an otherwise instrument-based market.
- **Retail vs institutional inside one docs site.** Quickstart “WebSockets” card → gRPC streaming guide. WebSocket pages link to `/api/authentication.md`. Overview (snake_case, numeric subscription types) vs channel pages and SDK (camelCase, string enums). Trader-guide crypto schema says production still carries hand-listed Bitcoin markets and automated families are in preprod ([Crypto schema](https://docs.polymarket.us/trader-guide/crypto-schema.md)). The retail gateway on this date returned open short-dated BTC markets such as `cpc-btc-hit-h-w-2026-11-13-85000` with a BRTI “at or above at any point” description. The retail catalog and that trader-guide sentence disagree.
- **Health.** Unauthenticated `gateway.polymarket.us/v1/health` vs bearer-auth `api.prod.polymarketexchange.com/v1/health`.
- **Two 429 JSON shapes** and **20 vs 25** request budgets, as in section 6.

## Implications for implementation

Modules below are the retail adapter. They do not import `py-clob-client` and they do not construct an order client in `scan` or `replay`.

**Config**

- `polymarket_us.gateway_base_url = https://gateway.polymarket.us`
- `polymarket_us.api_base_url = https://api.polymarket.us`
- `polymarket_us.ws_markets_url = wss://api.polymarket.us/v1/ws/markets`
- `polymarket_us.ws_private_url = wss://api.polymarket.us/v1/ws/private`
- `polymarket_us.public_rps = 8` and `polymarket_us.auth_rps = 8` as the starting budgets. The published ceilings disagree (20 and 25). Eight per second leaves headroom for a 429 backoff and for recovery GETs. Raise only after measuring the 429 on the host actually used.
- `polymarket_us.fee_theta` is an account setting. Allowed published values: `0.095`, `0.085`, `0.075`, `0.0695`. Default for an unknown account: **unset**, and the assessment is scenario-only. Do not read `market.feeCoefficient`. If the account is in ET days 1–7 after its first fill, set `0.0695` even when trailing volume would be tier 1.
- `polymarket_us.maker_rebate_theta = 0` unless the account’s maker tier is known.
- `polymarket_us.book_freshness` uses local receipt time of the last complete snapshot. `transactTime` is stored and displayed. It does not, by itself, expire a book that was fetched successfully.
- `polymarket_us.discovery_clock = market.endDate` for the contract-expiry filter, plus `gameStartTime` and the question text as separate fields. A 7-day `endDate` filter is not a 7-day event filter.

**Functions**

- `sign_request(key_id, secret_b64, method, path) -> headers` as in section 2. Path has no query and no body. Clock skew over 30 seconds is an auth failure, not a retry storm.
- `list_open_markets(end_min, end_max, category)` pages `limit=100` with `active=true&closed=false` until `len(markets) < limit`. It then drops rows whose `status` is not `MARKET_STATUS_OPEN`. It does not pass `status` or `sportsMarketTypes` as filters until a test shows the server honors them.
- `normalize_market(raw)` reads `description` as rules, `marketSides` for YES/NO or team labels (`long == true` is YES), `orderPriceMinTickSize`, `minimumTradeQty`, `endDate`, `gameStartTime`, `assetPriceTerms.windowEnd` when present, and `comboEnabled`. `comboEnabled` true, or more than one instrument id on the sides, disqualifies the pair for the v1 binary calculator.
- `book_to_ladders(market_data)` parses `px.value` and `qty` as `Decimal`. YES asks are `offers` in given order (checked best-first). NO asks are each bid converted with price `1 - bid` and the same qty. It does not also walk offers for the NO ladder. It records `state`. Any state other than `MARKET_STATE_OPEN` makes the book invalid for qualification.
- `taker_fee(theta, fills)` implements per-fill half-even and the cumulative cap from section 7. `buying_power_short(q, p) = q * (1 - p)`. `buying_power_long(q, p) = q * p`.
- `ws_markets_session` subscribes with camelCase `SUBSCRIPTION_TYPE_MARKET_DATA` only (not lite). On each `marketData`, replace that slug’s levels. On disconnect, mark the book invalid. Ignore `heartbeat` for quote age. If a frame is snake_case or lacks `bids`/`offers`, count a schema failure and do not merge it.
- Order submission stays out of the process. When it exists later: `price.value` is the YES price; `ORDER_INTENT_BUY_SHORT` sends `1 - no_price`; `tif` is IOC or FOK; `manualOrderIndicator` is `AUTOMATIC`; `idempotencyKey` is stable across retries; `DAY` is not used.

**Tests**

1. Sign fixture: timestamp `1700000000000`, method `GET`, path `/v1/ws/markets` produces the message `1700000000000GET/v1/ws/markets` and a 64-byte Ed25519 signature. A 64-byte decoded secret uses the first 32 bytes. The query string is absent from the message.
2. Book fixture from `sources/live/sample_book.json`: best YES ask `0.3000` size `1500.2500`; NO ask from the bid is `0.8000` size `2678.5000`. `currentPx` `0.2500` is not used as a fill price. BBO `bidShares` `"2600"` is not used as size.
3. Sort fixture from `sources/live/sample_book_sports.json`: bids descend, offers ascend. Complementary equal sizes do not get added into both ladders.
4. Fee A/B/C from section 7, including the two-fill cap ($0.02 then $0.01, total $0.03) and half-even of `2.375` → `2.38`, `0.025` → `0.02`, `0.035` → `0.04`.
5. Short 100 at YES price `0.40` consumes buying power `60`, fee tier-1 `2.28`. Short 100 at `0.50` consumes `50`.
6. `endDate` vs event: a market with question time 2026-01-02 and `endDate` 2026-01-05 does not get a payout time of either timestamp. Payout stays unknown.
7. Rules hash: changing `description` from a $1/$0 sentence to a 50-50 cancellation sentence revokes qualification.
8. `active=true` fixture that is `MARKET_STATUS_RESOLVED` is excluded. `MARKET_STATE_EXPIRED` book is excluded.
9. Quiet-book fixture: receipt time is now, `transactTime` is two hours ago, state `OPEN`. The book stays qualified on receipt age. A receipt older than the configured max does not.
10. Nanosecond `transactTime` is stored raw. A parser that drops the last three digits must not be the identity of the timestamp.
11. Schema gate: a snake_case WS frame and a camelCase frame are classified differently; only the pinned schema updates the book.
12. Reconnect test: levels from connection generation N are invisible to generation N+1 until a new full `marketData`.
13. Discovery: `categories` and `endDateMin`/`endDateMax` are sent; `status` and `sportsMarketTypes` are not, until a regression test shows they filter.
14. Safety: the scan/replay import graph has no `orders.create` symbol.

## Open questions

1. Which of 20/s per key, 20/s per IP, and 25/s per IP plus Cloudflare location is actually enforced on `gateway.polymarket.us` and on `api.polymarket.us`? Only a 429 experiment, or a written answer from `support@polymarket.us`, settles it.
2. What bytes does `wss://api.polymarket.us/v1/ws/markets` send for subscribe, heartbeat, and the first book: camelCase or snake_case, full ladder or top N, snapshot every time or deltas? A key is required. The retail docs do not say N.
3. Does the server require a heartbeat reply, and what is the payload? The SDK sends none.
4. Does a retail developer-portal key satisfy Participant Agreement §I.19 written production approval, or is that clause only the institutional conformance path?
5. Can the developer portal issue a key that cannot place orders? The public auth doc describes one key.
6. Is there an authenticated endpoint that returns the account’s current Θ, onboarding day, and maker tier? None was found on the public market object. `feeCoefficient` on the market was uniformly 0.095.
7. Is `payoutEstimateTime` ever non-empty, and does any retail field give a buying-power release time distinct from `endDate`?
8. After `EVENT_TIER_1`, how often does the settlement price later change? The institutional sentence allows corrections; the learn page says resolutions are final.
9. Are offset pages stable while new markets are listed, and is there a maximum `limit` below 100 that the server silently clamps? The 7-day pull with `limit=100` returned 100 until the tail, which is consistent with 100 being honored for that call.
10. For team slugs, is “first team is YES” true for every league, or only when `marketSides[].long` agrees? One NFL slug agreed. A systematic check was not done.
11. Will non-US residents pass KYC given the SSN field, despite the agreement’s country-of-residence wording?

## Sources

Primary docs and live bodies are under `sources/`, named by sanitized URL. The ones this report relies on most:

- `https://docs.polymarket.us/llms.txt` and `llms-full.txt`
- `https://docs.polymarket.us/api-reference/introduction.md`
- `https://docs.polymarket.us/api-reference/authentication.md`
- `https://docs.polymarket.us/api-reference/rate-limits.md`
- `https://docs.polymarket.us/fees.md`
- `https://docs.polymarket.us/market-structure/collateral-and-margin.md`
- `https://docs.polymarket.us/api-reference/markets/get-markets.md`
- `https://docs.polymarket.us/api-reference/markets/get-market-by-slug.md`
- `https://docs.polymarket.us/api-reference/markets/get-market-book.md`
- `https://docs.polymarket.us/api-reference/markets/get-market-bbo.md`
- `https://docs.polymarket.us/api-reference/websocket/overview.md`
- `https://docs.polymarket.us/api-reference/websocket/markets.md`
- `https://docs.polymarket.us/api-reference/websocket/private.md`
- `https://docs.polymarket.us/api-reference/orders/overview.md`
- `https://docs.polymarket.us/api-reference/orders/create-order.md`
- `https://docs.polymarket.us/faqs/sports-faqs.md`
- `https://docs.polymarket.us/trader-guide/environments.md`
- `https://docs.polymarket.us/trader-guide/rate-limits.md`
- `https://polymarketexchange.com/files/legal/latest/participant-agreement` (PDF, effective 2026-08-06)
- `https://polymarketexchange.com/files/legal/latest/rulebook` (PDF, file date 2026-09-30)
- `https://pypi.org/pypi/polymarket-us/json`
- `https://github.com/Polymarket/polymarket-us-python` source files in `sources/sdk/`
- Live: `sources/live/counts.json`, `enddate_7d_compact.json`, `sample_book.json`, `sample_book_sports.json`, `sample_bbo.json`, `sample_price_history.json`, `sample_settlement_resolved.json`
