# Prior art, empirical evidence, and risks

*Fetched 2026-11-11. User-Agent `pmarb-research/0.1`. Scope: Kalshi and Polymarket US (`polymarket.us`, QCX LLC), not global Polymarket. Claim labels: VERIFIED (read in a saved primary page, PDF, or repository file, or observed on a public endpoint), INFERRED, UNKNOWN. This is research, not legal, tax, or trading advice.*

## Executive summary

The open-source bots that mention Kalshi and Polymarket do not implement the strategy in `plan.md`. The repository the plan cites, `realfishsam/prediction-market-arbitrage-bot`, does compute both YES/NO directions, then sizes each leg from the same dollar budget, so the contract counts differ, and it treats a successful `createOrder` return as a fill. The two other serious codebases reviewed here are a directional same-token scanner with a flat-percent fee model (`ImMike/polymarket-arbitrage`) and a research terminal that uses a `p(1-p)` fee shape, a settlement-risk label, and often a synthetic book (`rohanthomas1202/truthlayer`). None of them walk equal contract counts through real depth with venue fee-rounding rules, and all of them speak to global Polymarket (`polymarket.com` / CLOB / Gamma), not Polymarket US.

PMXT’s published prices match the plan: Free / Starter $29.99 / Pro $99.99, with 25,000 / 250,000 / 1,000,000 monthly credits, 1 credit per REST call and 0.1 per WebSocket message. Self-hosted `pmxt-core` is MIT. Its own compliance matrix marks `fetchMarkets`, `fetchOrderBook`, `watchOrderBook`, and `createOrder` as implemented for both `kalshi` and `polymarket_us`. That does not make it a substitute for direct adapters: hosted catalog ingestion and hosted writes omit this pair, Kalshi signing in the SDK is RSA-PSS only, and the Polymarket US normalizer returns the long-side book and tells the caller to invert.

Published measurements of Kalshi-versus-Polymarket gaps are about global Polymarket. A 2024 election case study reports execution-adjusted gaps around 3 cents, persisting at least 30 minutes, on contracts that were not the same bet (media call versus inauguration). A vendor study of one busy day of BTC 15-minute markets found the two price paths within about 1 cent. No saved primary source measures executable, fee-aware, equal-quantity gaps between Kalshi and Polymarket US. The clearest historical opposite payout, Cardi B at the 2026 Super Bowl halftime show, is Kalshi versus global Polymarket; Front Office Sports reports that the US app did not list that performer market.

## Where plan.md is wrong, outdated, or missing

`plan.md` is dated the same day as this fetch. Its two claims that this topic can check are not wrong.

**Confirmed**

- `realfishsam/prediction-market-arbitrage-bot` considers both directions and sizes legs from an equal dollar amount, so contract counts diverge when prices differ. It treats order-submission success as trade success. See the code review below. [arbitrage.js](https://github.com/realfishsam/prediction-market-arbitrage-bot/blob/main/src/arbitrage.js), [bot.js](https://github.com/realfishsam/prediction-market-arbitrage-bot/blob/main/src/bot.js). VERIFIED.
- PMXT hosted prices and credit weights match the Rate Limits page and the pricing HTML. Hosted catalog ingestion includes Kalshi and omits Polymarket US. Hosted writes are Polymarket (global), Opinion, and Limitless. VERIFIED. [rate limits](https://www.pmxt.dev/docs/rate-limits.md), [venues](https://www.pmxt.dev/docs/concepts/venues.md), [hosted trading](https://www.pmxt.dev/docs/concepts/hosted-trading.md).

**Missing, not contradicted**

- The plan never separates global Polymarket evidence from Polymarket US evidence. Every quantified paper and dataset saved here is global CLOB, Gamma, or on-chain Polygon data.
- The plan does not record Kalshi Developer Agreement v1.1 §§3.1 and 3.5: API data may be stored only to facilitate the member’s own trading and may not be shared, and API access for benchmarking or competitive monitoring is prohibited. Suspension can be immediate. [Developer Agreement v1.1](https://assets.kalshi.com/Kalshi-Developer-Agreement.pdf).
- The plan does not record that Polymarket US Rulebook Rule 5.3(i) (30 September 2026) requires automated systems to have order throttles, price collars, and kill switches, and that orders are labeled `MANUAL_ORDER_INDICATOR_AUTOMATIC`. [Rulebook PDF](https://www.polymarketexchange.com/files/legal/Polymarket%20US%20Rulebook%20(2026.09.30).pdf), [orders overview](https://docs.polymarket.us/api-reference/orders/overview.md).
- The plan’s PMXT note “method support varies” is true and incomplete. For the four methods asked about, the generated compliance matrix says both venues implement them. The differences that matter are hosted-versus-local, RSA-PSS-only Kalshi auth, and a long-side-only US book. [COMPLIANCE.md](https://github.com/pmxt-dev/pmxt/blob/main/core/COMPLIANCE.md), [kalshi/auth.ts](https://github.com/pmxt-dev/pmxt/blob/main/core/src/exchanges/kalshi/auth.ts), [polymarket_us/normalizer.ts](https://github.com/pmxt-dev/pmxt/blob/main/core/src/exchanges/polymarket_us/normalizer.ts).
- Hosted plans also cap requests per minute (60 / 300 / 1,000) and concurrent WebSocket streams (5 / 20 / 100). Continuous recording can hit the stream cap before the monthly credit cap. [rate limits](https://www.pmxt.dev/docs/rate-limits.md).
- No reviewed project implements equal-quantity ladder walks, fee rounding, or one-sided-fill accounting. The plan is ahead of the public code, and it should not import execution from any of these repositories.
- Position limits are per contract, in that contract’s terms, and aggregate across commonly controlled accounts. They are not a single account-wide number, and they were absent from the one public Kalshi market payload sampled.
- Polymarket US documents that card and ACH deposits typically clear in 3–4 business days before the related cash, including trading proceeds tied to that deposit, can be withdrawn. That is a capital-lock constraint separate from contract settlement. [withdrawal rules](https://docs.polymarket.us/learn/deposits/withdraw-funds/rules.md), [withdrawal overview](https://docs.polymarket.us/learn/deposits/withdraw-funds/overview.md).
- Tax character and information reporting are unresolved. The only primary sentence saved is KPMG’s statement that the IRS has not weighed in on information reporting for these contracts, plus a Polymarket US consent clause that mentions Form 1099-B. Not advice.

**Not established as wrong**

- Nothing in the saved PMXT or `realfishsam` sources shows a numeric claim in the plan’s “Existing project” or “PMXT” paragraphs to be factually false as of this fetch.

## 1. Open-source cross-venue projects

### How the list was built

GitHub search `kalshi polymarket arbitrage`, sorted by stars, returned `total_count` 338 on 2026-11-11. A second query, `prediction market arbitrage kalshi`, returned 224. A recency query, `polymarket kalshi arbitrage bot` sorted by `updated`, returned 101 and is dominated by repositories updated in October–November 2026 with 0–5 stars. Saved responses: `sources/github-search-*.json`. Repository metadata below is from `GET /repos/{owner}/{repo}` unless noted as search-only.

Many high-star hits are not arbitrage engines. `alsk1992/CloddsBot` (3,100 stars, TypeScript, pushed 2026-11-06) is a multi-venue agent. `pmxt-dev/pmxt` (2,208, TypeScript, pushed 2026-09-30) is a unified API. `guzus/dr-manhattan` (206) and `gtg7784/dr-manhattan-ts` (56) are CCXT-style libraries. `braedonsaunders/homerun` (221) is a strategy platform. `YichengYang-Ethan/oracle3-prediction-market-agent` (569, Apache-2.0, pushed 2026-10-12) describes fee-adjusted no-arbitrage across related contracts; its code was not read, so it is not scored as cross-venue. `tarpboi/Prediction-Markets-Trading-Bot-Toolkits` (528, Rust) is a toolkit, not reviewed.

The table is the highest-star repositories whose name or description claims Kalshi–Polymarket arbitrage. “Not read” means the capability cells are UNKNOWN rather than guessed from the README.

| Repository | Stars | Pushed | Language | License (API) | Venues in description | Code read |
|---|---:|---|---|---|---|---|
| [ImMike/polymarket-arbitrage](https://github.com/ImMike/polymarket-arbitrage) | 313 | 2025-12-09 | Python | none (README badge says MIT; API `license` is null) | Global Polymarket Gamma/CLOB + Kalshi | Yes |
| [CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot](https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot) | 257 | 2026-05-09 | Python | MIT | Global Polymarket CLOB + Kalshi, BTC 1-hour | Yes, strategy file in full |
| [dexorynlabs/polymarket-trading-bots](https://github.com/dexorynlabs/polymarket-trading-bots) | 228 | 2026-11-11 | Python | MIT | Claims a Polymarket–Kalshi arb bot among five | No. GitHub `size` 56,582 KB |
| [realfishsam/prediction-market-arbitrage-bot](https://github.com/realfishsam/prediction-market-arbitrage-bot) | 187 | 2026-01-16 | JavaScript | MIT | Global `polymarket.com` + Kalshi via `pmxtjs` ^0.4.4 | Yes |
| [AsArchitects/Kalshi-trade-bot](https://github.com/AsArchitects/Kalshi-trade-bot) | 82 | 2026-03-27 | none | MIT | 15-minute BTC, both venues | No. GitHub `size` 8 KB, so it cannot contain a real engine |
| [llllllilllll/Kalshi-Trade-Bot](https://github.com/llllllilllll/Kalshi-Trade-Bot) | 78 | 2026-03-27 (search) | none | not fetched | Same description as the row above | No. Likely a duplicate listing |
| [rohanthomas1202/truthlayer](https://github.com/rohanthomas1202/truthlayer) | 67 | 2026-04-20 | Python | none | Polymarket + Kalshi scanner; later text adds Metaculus, PredictIt, Manifold | Yes, detector and settlement modules |
| [polystrategist/kalshi-arbitrage-bot](https://github.com/polystrategist/kalshi-arbitrage-bot) | 54 | 2026-07-13 | Python | MIT | Kalshi only (YES+NO sum and spreads), per API description | No |
| [TopTrenDev/polymarket-kalshi-arbitrage-bot](https://github.com/TopTrenDev/polymarket-kalshi-arbitrage-bot) | 50 | 2026-06-15 | Rust | none | Both venues, description only | No. `size` 123 KB |
| [Synpath-ai/prediction-market-arbitrage-trading-bot](https://github.com/Synpath-ai/prediction-market-arbitrage-trading-bot) | 45 | 2026-10-15 | Python | MIT | Scan, paper, backtest. Description only | No |
| [mbordash/DRADIS](https://github.com/mbordash/DRADIS) | 36 | 2026-11-12 (search) | Rust | not fetched | Multi-strategy Kalshi and Polymarket bot | No |
| [akhan280/event-contract-arbitrage](https://github.com/akhan280/event-contract-arbitrage) | 28 | 2026-10-22 | TypeScript | none | Polymarket, Kalshi, Robinhood. Created 2024-11-05 | No |
| [defi-ape/polymarket-kalshi-arbitrage-bot](https://github.com/defi-ape/polymarket-kalshi-arbitrage-bot) | 13 | 2026-05-29 | TypeScript | none | Polls Kalshi Trade API and Polymarket CLOB, 15-minute | No. `size` 32,634 KB |
| [grzresearch/Polymarket-Kalshi-Arbitrage](https://github.com/grzresearch/Polymarket-Kalshi-Arbitrage) | 10 | 2026-10-30 | Python | none | Empty description | No |
| [RichardFeynmanEnthusiast/kalshi-polymarket-arbitrage-bot](https://github.com/RichardFeynmanEnthusiast/kalshi-polymarket-arbitrage-bot) | 9 | 2025-12-12 | Python | GPL-3.0 | Empty description | No |

Star counts and push dates above that say “search” come from the search payload rather than a separate repo fetch. VERIFIED against those saved JSON files.

### Code review: realfishsam/prediction-market-arbitrage-bot

This is the repository `plan.md` cites. It is small (17 blobs), last pushed 2026-01-16, MIT, and depends on `pmxtjs` `^0.4.4`. The configured URLs are `https://polymarket.com/event/...` and a Kalshi event URL. The README links `https://docs.polymarket.com`, the global docs. It is not a Polymarket US bot. VERIFIED in `config.js`, `package.json`, and `README.md`.

What the plan got right:

- `calculateArbitrage` prices both locks: Polymarket YES + Kalshi NO, and Kalshi YES + Polymarket NO, in cents, profit `100 - cost`. VERIFIED, `src/arbitrage.js`.
- `executeArbitrage` sets one dollar budget (`1000` cents in `YOLO`, else `tradeAmountCents`) and sets `contracts = floor(budget / legPrice)` independently. Different prices produce different contract counts, so the $1 terminal values do not cancel. VERIFIED, `src/bot.js` lines 97–104 of the saved file.
- Both `createOrder` calls run with `Promise.all`. If both return without throwing, the code stores `order.id` and treats the pair as open. The dry-run path returns `success: true` with a fake id. There is no read of filled size. VERIFIED, `executeTrade` and `executeArbitrage`.

Bugs the plan did not list, all VERIFIED in the saved sources:

- Prices come from `outcome.price`, scaled by 100 and rounded to two decimals. There is no order-book walk and no ask-versus-bid distinction.
- Fees are omitted. The README says so.
- Matching is Jaccard plus Levenshtein on titles, threshold 0.7. Rules, resolution source, and expiry are not compared. `src/matcher.js`.
- Default `minProfitCents` in `config.js` is `0.001` while the README table says `1`. With prices in cents, `0.001` accepts numerical dust.
- If one live order succeeds and the other throws, the function logs failure and returns false without canceling or hedging the leg that was accepted.
- `exitPosition` sells both legs, and if either sell fails it still sets `currentPosition = null`. The comment says a production system would need manual intervention. The code does the opposite.
- Exit is not “hold to settlement.” The bot sells when the displayed gap falls below `minProfitCents`, or when some other title has a larger gap. That abandons the lock and adds a second round of execution risk.
- `YOLO` is a hard-coded 1,000 cents, not the account balance. The README’s “all in” description does not match the code.
- Kalshi credentials are `KALSHI_API_KEY` and `KALSHI_API_SECRET`. That shape does not match Kalshi’s documented signed API-key schemes. Whether `pmxtjs` 0.4.4 mapped those fields was not tested.

### Code review: ImMike/polymarket-arbitrage

Highest-star dedicated Python repository in the search (313). Pushed 2025-12-09. GitHub reports no license file. The README badge says MIT. VERIFIED.

The cross-venue engine is not the plan’s lock. `CrossPlatformArbEngine.check_arbitrage` looks for buy-YES on one venue and sell-YES on the other, and the same for NO. That requires an existing inventory or a short sale of the same token. It is not “buy YES here and NO there.” VERIFIED, `core/cross_platform_arb.py`.

Other defects, VERIFIED in that file and `core/execution.py`:

- Fees are `price * 0.015` on Polymarket and `price * 0.01` on Kalshi, plus `gas_cost * 2` defaulting to `$0.02` per order. That is not Kalshi’s `0.07 * p * (1-p)` schedule and it imports on-chain gas into a Kalshi leg. At a 50-cent contract the gas term alone is 4 cents, larger than a typical displayed gap.
- Depth used is the top-of-book size only. `max_size = min(buy_liquidity, sell_liquidity)` and `suggested_size = min(max_size, 100)`. The comment says “$100” while the quantity is a contract size. Units are mixed.
- Matching is `SequenceMatcher` plus entity overlap, default threshold 0.5, with sports and person heuristics. No rules text.
- `ExecutionEngine._handle_place_orders` loops legs independently. A slippage or risk rejection `continue`s. An exception increments `orders_rejected` and proceeds to the next leg. There is no paired cancel.
- `_place_order` retries the same submission. A timeout that actually reached the venue can duplicate an order. The plan’s “no blind retry after an ambiguous timeout” is the correction.
- The README’s “real” mode is Gamma plus Polymarket CLOB. Kalshi is described as new. Simulation mode injects mispricings. The README’s “99.6% win rate, $573 profit” is explicitly a simulation screenshot. Do not cite it as a track record.

Reusable idea: a separate risk manager and a kill switch exist as objects, and slippage is checked against the snapshot that generated the signal. The slippage check returns true (allow) when the snapshot quote is missing, which is the wrong default.

### Code review: rohanthomas1202/truthlayer

67 stars, pushed 2026-04-20, no license file, Python scanner rather than an order sender. The README says it does not execute. VERIFIED.

What is worth copying as a checklist, not as code:

- `compute_venue_fee` uses `fee_rate * price * (1 - price)`, which is the right shape for both venues’ published taker formulas. VERIFIED, `backend/services/arb_detector.py`.
- `compute_settlement_risk` returns red on rejected framing or disagreeing resolution sources, and yellow on date or small threshold differences. `framing_detector.py` names 17 patterns, including inclusive-versus-exclusive boundaries, different resolution sources, subset thresholds, and conditional versus unconditional. That list is a useful review checklist. It is not a substitute for storing the venue’s rule text.
- Filters can drop red settlement risk before an opportunity is shown.

What not to copy, VERIFIED in the same files:

- The trade it detects is buy at one venue’s ask and sell at the other’s bid, after an optional `1 - price` inversion. That is directional, not a $1 lock.
- Fee coefficients are `polymarket: 0.02` and `kalshi: 0.07`, and the spread subtracts `gas_cost_usd * 2`. The 0.07 multiplier matches Kalshi’s standard coefficient only if it is applied as `0.07 * p * (1-p)` per contract; the code does that. The 0.02 multiplier and the gas term are global-Polymarket assumptions and are wrong for Polymarket US, whose published coefficients are in a different range (the plan cites 0.0695 to 0.095; this review did not re-fetch the fee schedule).
- When a book is missing, the scheduler builds a synthetic bid and ask at `yes_price ± spread/2` with `quantity = volume`. A later liquidity sum then treats that fabrication as depth.
- Liquidity is `sum(price * quantity)` over the top three levels, in dollars, not a contract-count walk.
- The client file is `backend/clients/polymarket.py`. Nothing in the files read targets `gateway.polymarket.us`.

### Also read: CarlosIbCu BTC hourly scanner

Not one of the three deepest engineering reviews, but it is the highest-star file that actually tests a complementary payout. `backend/arbitrage_bot.py` (257 stars, MIT, pushed 2026-05-09) fetches Polymarket “Up/Down” and Kalshi yes/no asks for BTC hourly strikes.

When strikes differ, the code buys the pair that the comments argue has a minimum payout of $1 and sometimes $2 (price in the open interval between strikes wins both). When strikes are equal, it checks Poly Down + Kalshi Yes and Poly Up + Kalshi No against $1. It uses asks, not mids. It does not size contracts, walk depth, charge fees, or send orders. It polls every second.

The boundary comment is the useful part: Polymarket Down is coded as price strictly below the strike and Kalshi Yes as price at or above the strike. If either venue’s real rule differs by one tick, both legs can lose at the boundary. The scanner never loads those rules. Calling every cheaper-than-$1 pair “risk-free” is the bug to avoid. The README says the Polymarket side is the CLOB. VERIFIED in `README.md` and `arbitrage_bot.py`.

### Pattern across the code that was read

| Question | realfishsam | ImMike | truthlayer | CarlosIbCu |
|---|---|---|---|---|
| Equal contract counts | No. Dollar budget per leg | Top-of-book min size, same token buy/sell | Dollar liquidity, not contracts | Implicit 1 contract, no depth |
| Fees | None | Flat percent + gas | `p(1-p)` with wrong US coefficient, plus gas | None |
| Depth | None. `outcome.price` | Best level only | Top 3 levels, or a synthetic book | None. Displayed ask |
| Settlement equivalence | Title similarity 0.7 | Title/entity similarity 0.5 | Source, date, threshold, 17 framing labels | Strike comparison only |
| One-sided fill | Accepted leg left on; failed exit forgotten | Legs independent; retry can duplicate | No orders | No orders |
| Venue | Global Polymarket + Kalshi | Global Polymarket + Kalshi | Global Polymarket + Kalshi | Global CLOB + Kalshi |

## 2. PMXT

### What was fetched

Saved under `sources/pmxt/`: `llms.txt`, `llms-full.txt`, venues, hosted trading, rate limits, fetch-markets, fetch-order-book, watch-order-book, API overview, self-hosted guide, server page, introduction, quickstart, the pricing HTML, GitHub `LICENSE`, `core/COMPLIANCE.md`, `kalshi/auth.ts`, and `polymarket_us/normalizer.ts`. `create-order` and `architecture` doc paths returned HTTP 404. The hosted create-order page is `createOrderHosted`. Local architecture is the self-hosted guide plus `docs/sdk/server`.

License: MIT, copyright 2026 pmxt.dev. VERIFIED, `LICENSE` on `main`. The pricing HTML contains the string `MIT`. VERIFIED.

### Pricing versus plan.md

[Rate Limits](https://www.pmxt.dev/docs/rate-limits.md), fetched as markdown:

| Plan | Monthly credits | Per minute | WebSocket streams | Price |
|---|---:|---:|---:|---|
| Free | 25,000 | 60 | 5 | $0 |
| Starter | 250,000 | 300 | 20 | $29.99 |
| Pro | 1,000,000 | 1,000 | 100 | $99.99 |
| Enterprise | custom | custom | custom | contact |

“1 REST call = 1 credit, 1 WebSocket message = 0.1 credits. Health checks are free.” VERIFIED. The pricing HTML contains `25,000`, `250,000`, `1,000,000`, `29.99`, and `99.99`. VERIFIED.

The plan’s dollar prices, credit allowances, and 1 / 0.1 weights are correct. The plan does not mention the per-minute cap, the stream cap, the enterprise tier, or free health checks. Those are omissions.

A ten-pair recorder on hosted WebSockets is a credit problem and a stream-cap problem. Two markets, full book messages, can exhaust the Free tier’s 5 streams immediately if each outcome is its own stream, and can exhaust 25,000 credits at 0.1 credit per message after 250,000 messages. That arithmetic is INFERRED from the published weights; this review did not open a socket. Direct venue streams do not use these credits. The plan’s choice to start with direct APIs is consistent with this.

### Local server

Self-hosted mode is the absence of `pmxt_api_key`. The SDK spawns `pmxt-core`, waits until it is healthy, and sends requests to `http://localhost:3847`. WebSockets use `ws://localhost:3847/ws`. Override with `PMXT_LOCAL_PORT`, `PMXT_BASE_URL`, or `PMXT_ALWAYS_RESTART`. One process serves every venue. `pmxt.server.start/stop/health/status/logs/restart` exists; constructing a client also starts the server. VERIFIED, [self-hosted](https://www.pmxt.dev/docs/guides/self-hosted.md) and [server](https://www.pmxt.dev/docs/sdk/server.md).

Hosted mode does not spawn that process. It talks to `api.pmxt.dev` and `trade.pmxt.dev`. The pass-through shape is `POST /api/:exchange/:method` with `{"args":[...],"credentials":{...}}`. VERIFIED, [overview](https://www.pmxt.dev/docs/api-reference/overview.md).

### Method support for kalshi and polymarket_us

Source of the matrix: `core/COMPLIANCE.md` on `main`, which says it is generated from `core/src/exchanges/*/index.ts`. VERIFIED as the contents of that file. Not live-called.

| Method | Kalshi | Polymarket US |
|---|---|---|
| `fetchMarkets` | Y | Y |
| `fetchOrderBook` | Y | Y |
| `watchOrderBook` | Y | Y |
| `createOrder` | Y | Y |
| `fetchOHLCV` | Y | not supported |
| `fetchTrades` | Y | not supported |
| `fetchBalance` / `fetchPositions` / `fetchOpenOrders` | Y | Y |
| `cancelOrder` | Y | Y |
| `watchTrades` | Y | Y |

`watchOrderBook`’s doc page repeats both wire keys in its supported-venue list and shows both `wss://api.pmxt.dev/ws` and `ws://localhost:3847/ws`. VERIFIED, [watch-order-book](https://www.pmxt.dev/docs/api-reference/watch-order-book.md). Whether the hosted socket actually emits `polymarket_us` books was not tested. UNKNOWN at runtime.

Hosted catalog ingestion, a different list, is Polymarket, Kalshi, Opinion, Limitless, Smarkets, Probable, and Myriad. Polymarket US is not in it. VERIFIED, [venues](https://www.pmxt.dev/docs/concepts/venues.md). Hosted writes are Polymarket, Opinion, and Limitless only. Kalshi is “read-only via catalog” on the hosted trading page, with a pointer to self-hosted for venue-native writes. VERIFIED, [hosted trading](https://www.pmxt.dev/docs/concepts/hosted-trading.md). So:

- Self-hosted `createOrder` is marked implemented for both Kalshi and Polymarket US.
- Hosted `createOrder` is not available for this pair.
- Hosted cross-venue search will not find Polymarket US markets, because they are not ingested.

`fetchSeries` docs say Polymarket US has no series concept and returns an empty array. VERIFIED in `llms.txt` description of that method.

### Capability comparison versus direct adapters

| Need in the plan | Direct adapters | PMXT self-hosted | PMXT hosted |
|---|---|---|---|
| Public discovery and rules text | Native market payloads, including Kalshi `rules_primary` / `rules_secondary` (seen on one live market) | Unified market object. Rules survive only if `sourceMetadata` keeps them. Not audited field by field | Catalog omits Polymarket US, so the pair cannot be discovered there |
| Executable books | Plan already specifies Kalshi complement-of-bid and US long book with NO = `1 - YES bid` | Kalshi normalization not read. US `normalizeOrderBook` returns long bids and long offers only. Comment: callers must invert. A missing short quote on the market object is filled with `1 - longPrice`, which is not an executable NO ask | Same code path if the socket is the local server; hosted US books were not probed |
| Stream recovery | Plan uses Kalshi sequence numbers and US full-book replace | Opaque unified book with a millisecond timestamp. Sequence gaps are not in the OrderBook schema on the watch page | Same, plus credit and stream caps |
| Auth | Kalshi Ed25519 or RSA-PSS per the plan; US Ed25519 | `kalshi/auth.ts` signs RSA-PSS only (`RSA_PKCS1_PSS_PADDING`, salt length digest) and rejects a missing RSA private key. Ed25519 is not implemented in that file. US expects `POLYMARKET_US_KEY_ID` and `POLYMARKET_US_SECRET_KEY` | Hosted writes do not take Kalshi or US keys. They take an EVM key for Polygon escrow |
| Orders | Out of scope until a later milestone. US requires `manualOrderIndicator` | `createOrder` is implemented and easy to call by accident | Cannot place this pair |
| Fees | Must come from Kalshi series metadata and the US coefficient | Not in the compliance matrix | Not a reason to use hosted |
| Cost | No PMXT fee established | Process on localhost, MIT | Credits and stream caps make full-book recording a paid, capped feed |
| Custody | Accounts stay at the venues | Venue credentials stay local | Hosted custody is a Polygon `PreFundedEscrow` for global Polymarket, Opinion, and Limitless. Irrelevant to Kalshi and Polymarket US, and the page says the contracts were unaudited as of 2026-06-09 |

Direct adapters remain the right first build. PMXT is a plausible later cross-check of normalized top-of-book prices, not the system of record for rules, depth, or fees. Using it for Kalshi requires an RSA key even if the account’s primary key is Ed25519.

The US normalizer’s complement fill is the sharpest trap. In `polymarket_us/normalizer.ts`, if only a long price was quoted, `shortPrice` is set to `1 - longPrice`. The order-book comment in the same file says the implicit short-side book is `1 - longPrice` for each level and that callers needing the short side must invert prices themselves. Inverting a long ask produces a short bid, not the cost of buying NO. Buying NO lifts the long bid (`1 - yesBid`), which is the mapping `plan.md` already specifies. A scanner that reads PMXT `short.price` or `1 - ask` will invent a NO cost. VERIFIED in the saved `normalizer.ts`.

## 3. Empirical evidence

### Global Polymarket and Polymarket US are different markets

Dune’s prediction-market catalog is “onchain activity from Polymarket” on Polygon plus Kalshi partner data. It does not list Polymarket US. VERIFIED, [Dune overview](https://docs.dune.com/data-catalog/curated/prediction-markets/overview.md). The cross-venue schema’s resolution vocabulary includes `scalar` and parlays, and Polymarket prices are the on-chain venue’s Yes side. VERIFIED, [cross-venue overview](https://docs.dune.com/data-catalog/curated/prediction-markets/prediction_markets/overview.md). Kalshi public tables there are daily aggregates (`trade_report`), not a full book. A Dune dashboard cannot, from these docs, measure Polymarket US books.

The Zenodo record’s references are `gamma-api.polymarket.com`, `clob.polymarket.com`, and `docs.polymarket.com`. VERIFIED, saved record text. That is global Polymarket.

CryptoStruct’s BTC 15-minute note says Polymarket prices are in USDC and resolution is on-chain, versus Kalshi exchange determination. VERIFIED in the saved article text. That is global Polymarket.

No saved source is a measurement of Polymarket US versus Kalshi.

### Academic and working-paper results (global venue)

**Semantic non-fungibility, arXiv:2601.01706.** HTML and PDF saved. The abstract says about 6% of events are listed on more than one platform and that semantically equivalent markets show persistent execution-aware deviations of 2–4% on average. The 2024 presidential case is the part that matters for this project. Polymarket’s contract paid on media calls; Kalshi’s paid on inauguration, so Polymarket YES is a superset of Kalshi YES. The paper says Polymarket was a zero-fee CLOB with a 0.1-cent tick and that Kalshi charged no transaction fee on that market and used a 1-cent tick. Execution-adjusted spreads before election night averaged about $0.03 and reached about $0.07 for intervals that lasted at least 30 minutes. Slippage is assumed negligible because the books were deep. VERIFIED, paper text. Those contracts would fail the plan’s payout-table test. The 2–4% figure is an average across many platform pairs and fee assumptions, not a Kalshi–Polymarket US edge after current taker fees.

**Ng, Peng, Tao, Zhou, SSRN abstract 5331995.** Direct fetch returned HTTP 403. The search-indexed abstract says Polymarket led Kalshi in price discovery around the 2024 election, that substantial discrepancies persisted, and that on a 2023–2026 matched set leadership is bidirectional (Kalshi in sports and when it has more volume; Polymarket in politics and entertainment). Not saved. Treat as an indexed abstract, not a verified paper. The venue is the 2024-era Polymarket, which is global.

**Krause, SSRN abstract 6905683.** Direct fetch returned HTTP 403. The indexed abstract says, for the Digital Asset Market Clarity Act, mean arbitrage profit after fees was 4.87% with 89.1% of trading days showing an opportunity, and for the July 2026 Fed meeting 1.38% with 63.9% of days, using trade data, and it describes Polymarket as a cryptocurrency-native venue. Not saved. Do not use these percentages as design inputs until the PDF is read. Trade prints are not simultaneous executable asks, which is the objection the next paper raises.

**Moulinier, SSRN abstract 7170178.** Direct fetch returned HTTP 403. The indexed abstract says a pre-registered test on Kalshi one-minute bid/ask candles joined to the full Polymarket CLOB archive, 855 MLB moneylines and the June 2026 Fed decision, found no fee-clearing edge that survives alignment. It reports a median cross-venue mid divergence of 0.60 cent against a 2.00 cent Kalshi fee, a fee-clearing edge in 7.03% of 20,915 aligned minutes, median life one minute, and 0.00% of 2,617 pre-game minutes profitable. It says shifting one venue’s quotes against the other makes the apparent edge grow, which is the measurement artifact. Not saved. If the PDF matches this abstract, it is the strongest argument for the plan’s one-second skew limit, two-second book age, and replay delays of 100 ms through 2 s. It is still global CLOB evidence, and the “2.00 cent Kalshi fee” is a flat comparison figure, not the `p(1-p)` fee at every price.

**Vultax / Zenodo 10.5281/zenodo.23693998.** Record saved. Published 16 September 2026, observation window stated as 2026-07-17 through 2026-09-18. The full text and numerical findings are temporarily restricted. The methods note that is still public says books were read to 20 levels on each venue every five minutes; a gap that opens and closes between readings is invisible; Kalshi’s 0.07 coefficient came from “independent trackers” because the fee schedule could not be retrieved; rounding is ignored; deposits, withdrawals, capital holding cost, and a failed second leg are excluded. Polymarket endpoints cited are Gamma and CLOB. VERIFIED as the record page. No numeric gap from this study is cited here, because the findings are restricted. The method is a warning: a five-minute sampler cannot support the plan’s episode-duration measurement.

**CryptoStruct, “Kalshi vs Polymarket: the Same Market, Two Order Books.”** Saved HTML. One UTC day, 2026-08-21, BTC up/down 15-minute windows. The author says that day was about 2.8 times Kalshi’s trailing-30-day average, so it is not a typical day. Settlement-endgame curves are described as within about 1 cent at every minute. Median time-weighted quoted spread that day: 0.82 cent on Kalshi (p25–p75 0.70–0.92) and 1.02 cent on Polymarket (1.01–1.03). Touch size about 3,200–3,400 contracts on Kalshi versus about 250–280 on Polymarket. Polymarket quotes are USDC and resolution is described as on-chain. VERIFIED as the author’s claims in the saved page. These are within-venue spreads and a one-day price-path comparison, not a fee-adjusted lock, and not Polymarket US.

### What this implies for “do gaps survive fees?”

On global Polymarket during the 2024 election, the paper’s own fee assumption for that market was zero on both sides, and the economically interesting gap was about 3 cents on contracts that did not pay in the same states. Under today’s Kalshi taker formula, `0.07 * p * (1-p)` is at most 1.75 cents per contract at p = 0.5, and the plan’s US coefficient 0.095 is about 2.4 cents at p = 0.5, so a two-leg taker round trip near 50 cents is on the order of 4 cents before rounding. INFERRED from the plan’s cited formulas, which this review did not re-derive from the fee PDFs. A 1-cent mid gap, which is what the one-day BTC note describes, does not survive those fees. A 3-cent executable gap might, if both books have size, the rules match, and both legs fill. Nothing saved here shows that this happens on Polymarket US with any stated frequency or duration.

Commercial pages that quote a median 1.8-cent cross-spread, a Sharpe ratio, or section 1256 treatment were not saved and are not used. Their fee tables disagree with the venue schedules the plan already cites.

## 4. Risks

### Kalshi API and trading rules

[Developer Agreement v1.1](https://assets.kalshi.com/Kalshi-Developer-Agreement.pdf), PDF saved:

- Using the API accepts the agreement.
- Use “is expressly limited to facilitating a member’s own trading.” Other uses “may result in account suspension.”
- §3.1 prohibits collecting, caching, aggregating, or storing API data except to facilitate that member’s own trading, and prohibits sharing it with third parties without written authorization.
- §3.3 prohibits spoofing, wash sales, and other rule violations.
- §3.5 prohibits access “for purposes of monitoring the availability, performance, or functionality” of Kalshi’s services “or for any other benchmarking or competitive purposes.”
- §5: Kalshi may set call limits without notice and “may immediately suspend or terminate” API access without notice if it believes the agreement is violated.
- Termination section: Kalshi may terminate the agreement and the API license at its sole discretion, at any time, for any reason, by providing notice. The “without notice” sentence is specifically the violation case in §5.

A local recorder used to decide whether the same member will later trade is closer to the §3.1 exception than a published dataset is. Sharing books, or running the collector to compare venue quality, sits on the prohibited side of §§3.1 and 3.5. That is a reading of the text, INFERRED as to how Kalshi would apply it. The current member-agreement PDF at `kalshi.com/docs/kalshi-member-agreement.pdf` returned HTTP 429, so this review does not quote it.

[Kalshi finance FAQ](https://assets.kalshi.com/kalshi_finance_faq.pdf), saved: “Kalshi’s Contracts are not subject to pattern day-trading restrictions. We make our API open and available to all, and traders may use whatever trading style suits them best.” VERIFIED. Automated style is described as allowed. That sentence does not waive the Developer Agreement.

The CFTC rule filing saved as `rules07012525155.pdf` is a July 2025 KalshiEX rulebook text with redline numbering, not a document fetched from Kalshi’s live rulebook page (that page was not successfully downloaded). In the filing, position limits are whatever each contract’s terms state. Exceeding a limit, or entering an order that would exceed it, is a violation. Kalshi may direct a reduction and liquidate if the trader does not comply. A later violation within 12 months goes to disciplinary proceedings. Limits aggregate accounts under common control and people acting together. Market makers in a program can be carved out of the limit and kept on accountability levels. VERIFIED as the text of that filing. Whether those rule numbers are the live numbers is UNKNOWN.

A public `GET https://api.elections.kalshi.com/trade-api/v2/markets?limit=1&status=open` on 2026-11-11 returned one open market, ticker `KXMVECROSSCATEGORY-S2026K4EWW45ZZWF6AIKY-ZHP7MCBZS`, with `rules_primary`, `rules_secondary`, and `settlement_timer_seconds`, and no position-limit field. VERIFIED for that response. It was a multivariate contract, which the plan already excludes. The absence of a limit field on this payload does not mean contracts have no limit.

### Polymarket US rules

The legal set is five documents: Participant Agreement, Exchange Rulebook, Clearinghouse Rulebook, Risk Disclosure, Privacy Policy. QCX LLC is the DCM and QC Clearing LLC is the DCO. The partner docs say the current participant-agreement version string is `08.06.2026`. VERIFIED, [legal agreements](https://docs.polymarket.us/partners/onboarding/legal-agreements.md).

Participant Agreement dated 6 August 2026, first eight pages saved:

- PMUS may limit, condition, restrict, suspend, and/or terminate participant status and trading privileges under the rules.
- PMUS may limit messaging volume, for example messages per second.
- Production API use is not allowed until conformance requirements are met. The participant bears errors in its API implementation.
- The customer consents to electronic delivery of tax forms, “including, without limitation, IRS Form 1099-B.” That is a delivery consent, not a statement that event-contract profit is reported on 1099-B or how it is characterized.

Rulebook dated 30 September 2026, full text saved:

- Rule 5.3(i): participants using automated trading systems must implement pre-trade risk controls including order throttles, price collars, and kill switches.
- Rule 5.3(c): manual or automated messaging must not be excessive or disruptive.
- Rule 5.3(d): orders without sufficient funds are prohibited.
- Rule 5.9: position limits may be set per contract in the product specifications; the exchange aggregates accounts under CFTC Regulation 150.4; exemptions can be granted and rescinded. Position-accountability requests can require an explanation of the trading strategy, and the exchange can order that a position not be increased.
- Disruptive-practice text in the same file prohibits spoofing and wash sales. Closing a position with an opposing order before expiration is expressly not a wash sale. A cross-venue lock is not a wash sale under that sentence, because the two orders are on different exchanges. INFERRED: the wash-sale sentence is about transactions on this company. It is not a blessing of cross-venue arbitrage.

Orders API, saved markdown: `manualOrderIndicator` is required and must be `MANUAL_ORDER_INDICATOR_AUTOMATIC` for an automated system. The orders API rate limit stated there is 20 requests per second per API key. VERIFIED, [orders overview](https://docs.polymarket.us/api-reference/orders/overview.md). The plan’s separate note of a 25 requests/second retail limit was not re-checked.

The retail trading-limits page, saved, says an in-app or web market buy is limited to $1,000,000 and that large orders can partially fill. VERIFIED, [trading limits](https://docs.polymarket.us/learn/trading/access-and-limits/trading-limits.md). That is a UI order-size note, not the product-spec position limit.

Account closure risk is explicit in both venues’ texts: Kalshi can cut off the API, and Polymarket US can suspend trading privileges. Neither text says “arbitrage is prohibited.” Both leave the decision with the venue.

### Settlement divergence that actually happened

**Cardi B, Super Bowl LX halftime, February 2026. Kalshi versus global Polymarket. Not Polymarket US.**

- CBS News: Kalshi settled at the last traded price before the pause, 74 cents to No and 26 cents to Yes, citing ambiguity. A user complaint to the CFTC is described. Polymarket’s final decision was still pending at the time of that article. VERIFIED, saved CBS article text.
- Front Office Sports: Kalshi volume on the performer market is described as more than $47 million, and Polymarket’s international site as just under $10 million, of which more than $5 million was the Cardi B outcome. The same article says the $10 million “comes only from Polymarket’s international site; its U.S. app had only one Super Bowl market—on which team would win.” Kalshi paid 26 cents to Yes and 74 cents to No. Polymarket’s rules were “performs live and in person,” with a later “additional context” note, and the first resolution to Yes was disputed through UMA. VERIFIED, saved Front Office Sports text.
- AP News returned HTTP 403 to this user-agent. Not saved. Business Insider was not fetched.

This is the payout-table failure mode: one venue paid about 0.26/0.74 under an ambiguity rule, the other was on a path to 1/0 under different wording and an oracle dispute. A trader who was long Yes on both did not hold a $1 lock. It is not evidence that Polymarket US diverged, because the US app is reported not to have listed the contract.

**2024 presidential election, structural mismatch, not an opposite ruling on the same text.** arXiv:2601.01706, saved. Polymarket resolved on media network calls; Kalshi resolved on inauguration. The paper treats Polymarket YES as a superset of Kalshi YES. Prices differed by about 3 cents on an execution-adjusted basis for at least 30 minutes at a time. VERIFIED in the paper. The plan’s “exceptions and finality” review is exactly this case. The contracts did not silently become the same bet after the fact.

No saved source documents a case in which Polymarket US and Kalshi resolved the same event to opposite $1 and $0 outcomes. UNKNOWN whether that has happened.

Rule-comparison sites (samebetornot, OddsShopper) describe possible future splits on CPI delays, Fed rounding, and shutdown clocks. Those pages were not saved. They are not cited as historical resolutions.

Polymarket US’s own resolution page, saved, says the exchange determines the result from the market’s description and rules and from publicly verifiable information, with backup sources. It is not an optimistic-oracle vote. VERIFIED, [market resolution](https://docs.polymarket.us/learn/markets/market-resolution.md). Global Polymarket’s UMA dispute path therefore does not transfer.

### Withdrawals and capital lock

Polymarket US, saved docs:

- Card and ACH deposits typically take 3–4 business days to clear. Wire deposits, 1 business day. Funds in flight can be traded and cannot be withdrawn.
- “This includes instant buying power and any proceeds from trading activity tied to that deposit.”
- Withdrawals return to the original funding source, FIFO. Debit and ACH withdrawals typically arrive in 3–4 business days. App wire withdrawal is not available; support wires are described as 1 business day. The overview says withdrawals of cleared cash are free.

VERIFIED, withdrawal rules and overview. A seven-day resolution horizon can still leave cash uncleared for most of that week if it was funded by card or ACH. The plan’s instruction not to recycle capital at event end is necessary and not sufficient: cleared-versus-in-flight is a second clock.

Kalshi withdrawal timing was not retrieved. The member-agreement and help URLs returned HTTP 429. UNKNOWN.

Neither venue’s docs saved here promise that a winning contract’s cash is withdrawable on the resolution timestamp.

### Tax, brief, not advice

Saved KPMG article HTML (2026, “The State of Information Reporting for Event-Based Contracts”): “the Internal Revenue Service (“IRS”) has not yet weighed in on the information reporting considerations.” VERIFIED as that sentence in the saved file. The rest of the article body did not extract as text from the HTML, so this review does not attribute a 1099-form matrix to KPMG.

The Polymarket US participant agreement consents to electronic delivery of forms including Form 1099-B. VERIFIED. That does not establish section 1256 treatment, ordinary income, or wagering income.

Kalshi’s own tax help page was not retrieved (HTTP 404 on one guess, HTTP 429 on `kalshi.com`). Secondary summaries of Kalshi’s help center were not saved and are not used.

Do not copy any blog’s claim that Kalshi event contracts are section 1256 60/40 contracts. That characterization was not found in a saved primary source. UNKNOWN. A user needs a tax adviser before the replay’s profit figure is treated as an after-tax number.

## 5. What to copy, avoid, and measure

**Copy**

- Both-direction screening, from `realfishsam`, and then stop. Keep the calculator; discard the executor.
- Ask prices, not mids, from `CarlosIbCu`, plus an explicit boundary table when two thresholds are not identical. The plan’s payout table is the right home for that.
- TruthLayer’s red/yellow checklist: different resolution source, date mismatch, inclusive versus exclusive threshold, conditional versus unconditional, subset versus superset. Store it as reviewer prompts next to the rule text, not as an automatic approval.
- ImMike’s idea of checking the quote again at send time, with the opposite default: a missing quote rejects, it does not allow.
- The plan’s existing freshness, skew, fee-rounding, and one-sided-fill tests. The public code is evidence that those failures are the default, not edge cases.

**Avoid**

- Equal-dollar leg sizing. It manufactures an unhedged residual.
- Treating `createOrder` success, a dry-run stub, or an order id as a fill.
- Clearing local position state when one exit fails.
- Selling the lock because the displayed gap closed or a different market looks better. The hedge is the settlement, not a later sale.
- Flat percent fees, gas on a Kalshi or Polymarket US leg, and `p(1-p)` with a hardcoded 0.02 US coefficient.
- Synthetic books built from last price and volume.
- Title similarity above any threshold as approval. 0.5 and 0.7 both appear in real code and both ignore rules.
- PMXT `short.price` when it was filled by `1 - longPrice`, and `1 - longAsk` as the cost of buying NO.
- Hosted PMXT as a data dependency for this pair.
- Five-minute samples as evidence that a gap “persisted.”
- Any win rate printed from a simulator that injects mispricings.
- Publishing or redistributing recorded Kalshi API data.

**Measure, in the 30-day observation the plan already specifies**

- Gap in cents of executable equal-quantity cost, after the account’s actual fee function, at sizes 1, 10, and 100. Report the distribution, not the maximum.
- Lifetime of a qualified episode in seconds, ended by a book gap. Do not interpolate across disconnects.
- Fraction of episodes still qualified after a 100 ms, 250 ms, 500 ms, 1 s, and 2 s decision delay, separately for each leg.
- How often the gross gap is below the sum of the two taker fees at the observed prices. That fraction is the “does not survive fees” rate.
- Depth at the profitable levels versus the requested size, and the largest size that survives fees.
- Rule-review rejection reasons, especially source, threshold boundary, and ambiguity/last-trade settlement.
- For resolved pairs, the two venues’ actual payouts, including any non-0/1 settlement, and the clock from expected resolution to observed buying-power release.
- Polymarket US funds-in-flight versus settled cash, if the account is funded during the study. Kalshi withdrawal timing once a primary page is in hand.
- Share of qualified episodes coming from one event. The plan’s “not one event” gate is the right response to the election papers, where one contract family dominates the published gap.

## Implications for implementation

- Module `adapters/kalshi` and `adapters/polymarket_us` stay the book source. Do not add `pmxt` to the observation milestone. If a later comparison harness is built, put it in `adapters/pmxt_shadow` and never let it approve a pair or supply a fee.
- Function `acquisition_cost(book, side, qty) -> LadderFill` walks YES asks, or NO asks defined as `1 - yes_bid` with the bid’s size, for exactly `qty` contracts. Unit test: prices 0.40 and 0.55, dollar budget $100, must not return 250 and 181 contracts. That is the `realfishsam` fixture, and the residual `250 - 181` contracts must be reported as unhedged.
- Function `normalize_pmxt_us_book` is out of scope. Test the local normalizer instead: a long bid of 0.62 for 30 contracts is a NO ask of 0.38 for 30 contracts. A long ask of 0.64 is not a NO bid that can be bought. Fixture name: `test_no_cost_is_complement_of_yes_bid_not_of_displayed_price`.
- Config `book_max_age_seconds = 2`, `book_max_skew_seconds = 1`. Replay delays `[0.1, 0.25, 0.5, 1, 2]` seconds, plus an asymmetric case. These match the plan; the indexed Moulinier abstract is the reason they are short, and the PDF still needs to be filed under `sources/` before the numbers 0.60 cent and 7.03% are quoted in the UI.
- Config `min_sample_period_seconds` does not exist. Do not add a five-minute scanner. The Zenodo method states that gaps between five-minute reads are invisible.
- Fee function stays venue-specific and versioned. Unknown coefficient sets `assessment.qualified = false` and `fee_status = "scenario"`. Do not port `FEE_RATES = {"polymarket": 0.02, "kalshi": 0.07}` from TruthLayer, and do not port `gas_cost`.
- Test `test_ack_is_not_a_fill`: a response with an order id and `filled_qty = 0` leaves the pair unhedged and does not open an episode fill. Test `test_one_leg_reject_does_not_drop_exposure`: if the simulated second leg throws, the first leg’s contracts remain in `exposure` and the exit path cannot null them out. Those are the `realfishsam` `executeArbitrage` and `exitPosition` failures.
- Test `test_ambiguous_timeout_does_not_retry`: one submit, unknown result, no second submit. That is the ImMike `_place_order` retry.
- Module `review/payout_table`: rows for Yes, No, and each named exception, including “venue settles at last trade” and “media call versus inauguration.” If any row is not {1, 0} or {0, 1} with opposite winners, `qualification_block = "settlement_mismatch"`. Fixture: Cardi B, Kalshi 0.26/0.74 versus a $1 Yes resolution, must block even though both titles contain “Cardi B” and “halftime.” Label the fixture global-Polymarket so it is not stored as a Polymarket US fact.
- Config `kalshi_key_alg`: `ed25519` or `rsa-pss`, chosen from the key actually issued. Do not route Kalshi auth through PMXT unless `kalshi/auth.ts` grows an Ed25519 path. A unit test can assert our signer has both branches; it should not import PMXT.
- Live-trading config, still default-off: `manual_order_indicator = MANUAL_ORDER_INDICATOR_AUTOMATIC`, `kill_switch = true`, `order_throttle_per_second` and `price_collar_ticks` required before a client can be constructed. Those names track Polymarket US Rule 5.3(i) and the orders overview. Position limit is `per_contract_from_product_spec`, aggregated across the operator’s accounts, default 0.
- Config `capital_clocks`: `resolution_time`, `payout_credit_time`, `funds_cleared_time`. Polymarket US card/ACH clearing default assumption 3–4 business days, marked as a venue doc default, not as a fill. Replay must not free cash at `resolution_time`.
- Storage layout: raw Kalshi payloads stay on the operator’s machine, gitignored, and are not copied into a fixture repo that will be published. The Developer Agreement §3.1 sharing ban is why fixtures should be hand-built or redacted, not captured production books checked in.
- Episode metric `duration_seconds` uses receipt timestamps of the first and last qualifying assessment and stops at the first missing book. Report median and the share of episodes shorter than 60 seconds. Do not annualize.

## Open questions

- The SSRN PDFs for Krause (6905683), Moulinier (7170178), and Ng et al. (5331995) returned HTTP 403. The figures quoted from those abstracts are indexed text, not saved papers. Moulinier’s in particular should be read before anyone cites 0.60 cent or a one-minute half-life as a measured fact.
- Zenodo 10.5281/zenodo.23693998 has its numerical findings restricted. There is no usable after-fee gap number from that study in the public record page.
- No primary measurement of Kalshi versus Polymarket US executable books was found. The 30-day collector is the study. Outside numbers should not be used as a prior for expected profit.
- Has any Polymarket US contract and the Kalshi contract on the same real-world event settled to different economic outcomes? Not found.
- Live Kalshi member agreement and the current consolidated rulebook PDF were not downloaded (`kalshi.com` returned HTTP 429). The July 2025 CFTC filing may have been amended.
- Numeric position limits for the series the operator will actually trade. They are in contract terms, not in the one market payload sampled.
- Kalshi withdrawal and buying-power release timing, from a Kalshi page. Not retrieved.
- Does hosted `wss://api.pmxt.dev/ws` emit `polymarket_us` books in practice, given the catalog does not ingest that venue? Docs list the venue; it was not called.
- Will Kalshi accept the operator’s Ed25519 key on the stream the collector will use, and is there any PMXT release after `main` as of 2026-11-11 that signs Ed25519? The `auth.ts` on `main` does not.
- IRS character of a Kalshi contract and of a Polymarket US contract, and which form either venue will issue for 2026. Not found in a saved primary tax document beyond KPMG’s statement that the IRS has not addressed information reporting, and the Polymarket US 1099-B delivery consent.
- Whether a private recorder that never shares data is inside Developer Agreement §3.1’s “own trading” exception. The text supports that reading and also supports suspension if Kalshi disagrees. Counsel, not this report, has to close it.

## Sources

Primary files saved under `sources/`. The URLs below are the ones relied on.

1. GitHub search and repo metadata, 2026-11-11. `https://api.github.com/search/repositories?q=kalshi+polymarket+arbitrage` and the repo URLs in the table.
2. `https://github.com/realfishsam/prediction-market-arbitrage-bot` (`src/arbitrage.js`, `src/bot.js`, `src/matcher.js`, `config.js`, `package.json`, `README.md`).
3. `https://github.com/ImMike/polymarket-arbitrage` (`core/cross_platform_arb.py`, `core/execution.py`, `README.md`).
4. `https://github.com/rohanthomas1202/truthlayer` (`backend/services/arb_detector.py`, `settlement_risk.py`, `framing_detector.py`, `README.md`).
5. `https://github.com/CarlosIbCu/polymarket-kalshi-btc-arbitrage-bot` (`backend/arbitrage_bot.py`, `README.md`).
6. `https://www.pmxt.dev/docs/rate-limits.md`, `https://www.pmxt.dev/pricing`, `https://www.pmxt.dev/docs/concepts/venues.md`, `https://www.pmxt.dev/docs/concepts/hosted-trading.md`, `https://www.pmxt.dev/docs/guides/self-hosted.md`, `https://www.pmxt.dev/docs/sdk/server.md`, `https://www.pmxt.dev/docs/api-reference/overview.md`, `https://www.pmxt.dev/docs/api-reference/watch-order-book.md`, `https://www.pmxt.dev/llms.txt`.
7. `https://github.com/pmxt-dev/pmxt` `LICENSE`, `core/COMPLIANCE.md`, `core/src/exchanges/kalshi/auth.ts`, `core/src/exchanges/polymarket_us/normalizer.ts`.
8. `https://arxiv.org/pdf/2601.01706` and `https://arxiv.org/html/2601.01706v1`.
9. `https://zenodo.org/records/23693998`.
10. `https://cryptostruct.com/guides/kalshi-vs-polymarket-data`.
11. `https://docs.dune.com/data-catalog/curated/prediction-markets/overview.md` and `https://docs.dune.com/data-catalog/curated/prediction-markets/prediction_markets/overview.md`.
12. `https://www.cbsnews.com/news/cardi-b-super-bowl-prediction-market-dispute/`.
13. `https://frontofficesports.com/newsletter/cardi-b-halftime-bettors-cry-foul/`.
14. `https://assets.kalshi.com/Kalshi-Developer-Agreement.pdf`.
15. `https://assets.kalshi.com/kalshi_finance_faq.pdf`.
16. `https://www.cftc.gov/sites/default/files/filings/orgrules/25/07/rules07012525155.pdf`.
17. `https://api.elections.kalshi.com/trade-api/v2/markets?limit=1&status=open`.
18. `https://www.polymarketexchange.com/files/legal/Polymarket%20US%20Rulebook%20(2026.09.30).pdf`.
19. `https://www.polymarketexchange.com/files/legal/Polymarket%20-%20Individual%20Participant%20and%20Clearing%20Member%20Agreement%20(2026.08.06).pdf`.
20. `https://docs.polymarket.us/partners/onboarding/legal-agreements.md`.
21. `https://docs.polymarket.us/api-reference/orders/overview.md`.
22. `https://docs.polymarket.us/learn/trading/access-and-limits/trading-limits.md`.
23. `https://docs.polymarket.us/learn/markets/market-resolution.md`.
24. `https://docs.polymarket.us/learn/deposits/withdraw-funds/rules.md` and `overview.md`.
25. `https://kpmg.com/us/en/articles/2026/event-based-contract-reporting.html`.
26. Indexed only, HTTP 403 on direct fetch: SSRN abstracts 6905683, 7170178, 5331995.
