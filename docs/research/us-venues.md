# US event-contract venues beyond Kalshi and Polymarket US

> Live API values in examples were replaced with synthetic values; formats and field names are accurate.

**As of:** 11 November 2026 (US Central); CFTC pages and live calls were retrieved 12 November 2026, 01:10–02:10 UTC.  
**Question:** Which US-legal prediction-market or event-contract venues a US retail person can trade are real third order books, and which apps only display Kalshi (or another exchange already in the set)?  
**Plan reviewed:** a local worktree `plan.md` (documentation date in the file: 11 November 2026).

Status labels:

- **VERIFIED** — read from a primary page, PDF, or live unauthenticated response saved under `sources/`.
- **INFERRED** — a conclusion from those sources, not a sentence the source states.
- **UNKNOWN** — not established. Not filled in.

Offshore venues that block US persons (global Polymarket / CLOB / Gamma, Betfair, SX Bet, Stake, the existing UK Smarkets exchange) are out of scope. They are listed only in [Excluded](#excluded-and-not-yet-tradable).

---

## How this was checked

Primary regulatory list: CFTC Designated Contract Market filings, both pages, sorted by date descending.

- https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizations?col=Date&dir=DESC
- page 2: same URL with `page=1`
- Product filings: https://www.cftc.gov/IndustryOversight/IndustryFilings/TradingOrganizationProducts

The Event + Certified filter is dominated by Kalshi (organization code `KEX`, 79 pages). Counting that filter alone **misses Rothera**. Rothera’s sports and corporate contracts are filed as category **Financial Instrument**, not Event. An Event-only scan of `ROTHDCM` returned three older crypto binaries. The unfiltered `ROTHDCM` query returned three pages of swaps, including pro and college basketball and football templates certified 16–22 October 2026. **VERIFIED.**

Live unauthenticated calls that returned data: PredictIt `GET /api/marketdata/all/`, Crypto.com `GET /api/v1/predictions/events` and `/contracts/{symbol}/price`, Gemini `GET /v1/prediction-markets/events?status=active` and `GET /v1/book/{instrumentSymbol}`, ForecastEx public S3 files, Rothera `mkt_eod_20261111.csv`. Novig and ProphetX production hosts answered **401**. CME rulebook PDFs returned **403** to `curl` and were read through a second fetch of the same URLs.

---

## Where plan.md is wrong, outdated, or missing

The plan never names a third venue. That silence is not a factual error about Kalshi or Polymarket US. It is a hole if a later milestone adds “US retail apps” without an exchange identity.

### MISSING

1. **App versus exchange.** Coinbase and Webull are FCMs in front of **Kalshi**. Adding either as a third collector records the same book twice and can manufacture a false arb. Sources in [Same book](#same-book-do-not-add-these-as-a-third-venue).
2. **One brand, several books.** Interactive Brokers’ public commission page prices three different exchanges: ForecastEx, Kalshi, and CME. DraftKings’ own releases name CME, then Crypto.com Derivatives North America, then a planned Railbird integration. FanDuel’s 9 June 2026 release names CME and “OG Prediction Markets.” A single broker screen is not one book.
3. **The Kalshi fee depends on the path in.** The plan’s direct taker formula `0.07 × p × (1−p)` is the direct-member model the plan already flags as account-specific. IBKR’s published Kalshi schedule is a flat **USD 0.01 commission plus USD 0.01 exchange fee per contract**, not that quadratic formula. **VERIFIED** on https://www.interactivebrokers.com/en/pricing/commissions-events.php. Comparing an IBKR-routed Kalshi fill with a direct Kalshi fill is a fee-path difference on **one** book, not a second venue.
4. **Payout is not universally $1.** ForecastEx Rule 603 pays **$1.00 or $0.00** per contract. **VERIFIED** in the rulebook. CME Rule 2202.G pays a **Settlement Value defined in an external table**, not a number fixed in Chapter 22. **VERIFIED** from https://www.cmegroup.com/rulebook/CME/I/22.pdf. NADEX’s 1 August 2026 fee schedule has **$1, $10, and $100** contract values, and $10 crypto event contracts. **VERIFIED** in the public fee PDF. Novig’s two fee pages disagree on whether a contract pays $1 or 1¢. See [Novig](#novig--ludlow-exchange-llc). The plan’s `NetProfit = q − capital` is valid only after a venue adapter proves a $1 terminal value.
5. **ForecastEx is not a Kalshi-style book.** Rule 401 pairs a Yes bid with a No bid only when they sum to **$1.00**. There is no separate resting offer to “sell Yes.” Public data is a **pair tape**, not a depth ladder. The plan’s ladder walk and 2-second book age do not apply to the public files.
6. **Rothera’s category label.** Anyone using the CFTC “Event” filter to build a universe will omit the exchange that, on 11 November 2026, printed tens of millions of contracts in its clearing file.

### Not wrong, on the facts checked here

The plan’s choice of Kalshi (`KEX`, designated 3 November 2020) and Polymarket US (`QCX LLC d/b/a Polymarket US`, designated 9 July 2025) matches the CFTC list. **VERIFIED.** Nothing in this pass shows those two share an order book.

### OUTDATED

Nothing in the plan’s Kalshi or Polymarket US fee and API sections was re-litigated here. The IBKR Kalshi per-contract fee is an access path the plan does not mention. It does not by itself retire the direct-member formula.

---

## Regulatory map (CFTC, this retrieval)

Designated DCMs that matter for this question. Status and date are from the DCM table. **VERIFIED.**

| CFTC organization | Status | Designation date | Event-contract evidence this pass |
|---|---|---|---|
| Kalshi | Designated | 2020-11-03 | `KEX` binaries. Already in the plan. |
| QCX LLC d/b/a Polymarket US | Designated | 2025-07-09 | `QCEX` binaries. Already in the plan. Remark: operating as Polymarket US. |
| North American Derivatives Exchange, Inc. d/b/a crypto.com | Designated | 2004-02-18 | `CDNA`. Fee PDF says the same company also brands **OG Prediction Markets**. |
| ForecastEx LLC | Designated | 2024-06-24 | `FEX` forecast contracts. |
| Chicago Mercantile Exchange, Inc. | Designated | 2000-12-21 | `CME` Event Contract Swaps, including elections certified 2026-08-25. |
| Gemini Titan, LLC | Designated | 2025-12-10 | `GMNI` binaries, including unemployment and GDP templates dated 2026-11-05. |
| Aristotle Exchange DCM, Inc. | Designated | 2025-09-05 | `ARST` binaries, including same-day multi-game contracts dated 2026-11-06. |
| Railbird Exchange, LLC | Designated | 2025-06-13 | `REX` sports binaries, including NBA contracts dated 2026-11-06. |
| Rothera Exchange and Clearing LLC | Designated | 2019-06-24 (LedgerX; renamed 2026-01-20) | Sports and corporate swaps under Financial Instrument, October 2026. |
| Ludlow Exchange, LLC | Designated | 2026-06-16 | `LEX` NFL/NBA prop and award swaps, October 2026. Link to the Novig brand is **INFERRED**, not stated on the CFTC row. |
| ProphetX LLC | Designated | 2026-06-11 | `PROPHX` binaries, including IPO contracts dated 2026-11-10. |
| XV Exchange, LLC | Designated | 2026-11-03 | `XVE` soccer binaries dated 2026-11-05. Eight days old at retrieval. |
| Coinbase Derivatives, LLC | Designated | 2020-11-23 | `COIN` had **no** Event-category rows. The Coinbase **app** product is Kalshi, not this DCM. |
| Sporttrade DCM LLC | Designated | 2026-10-30 | Not in the product-organization dropdown. No certified products found. |
| Juice Exchange, LLC | Designated | 2026-07-24 | Not in the product dropdown. No products found. |
| Water Street Labs, LLC | Designated | 2026-07-16 | Same. |
| Optex Markets LLC | Designated | 2026-07-09 | Same. |
| Xchange Alpha, LLC | Designated | 2026-01-30 | Same. |
| Electron Exchange DCM, LLC | Designated | 2025-08-29 | `ETRON` Event filter: no entries. |
| tZERO DCM, LLC | Designated | 2026-11-10 | Not in the product dropdown. Product set **UNKNOWN**. |

Pending applications are not venues a person can trade today. Relevant names on the same table: **PredictIT Exchange DCM, Inc.** (pending 2026-10-05), 365Prediction, Limitless Markets US, Smarkets Board of Trade Exchange LLC, ForumEX, Eventive III, Bullish Markets, Six Markets, TLD Markets, PredictCraft Mkt Inc. d/b/a DimeTrades, PMEX Markets, Nasdaq MRX Futures, New Derivatives Markets, RSBIX, OneChronos Markets DCM. **VERIFIED** as pending. The operating PredictIt site is a **different** legal arrangement from the pending DCM. See [PredictIt](#predictit).

Older CBOT and NYMEX rows titled “Event Contracts” (certified 2022-09-01) are type **Future**, not the 2026 binary swaps. **VERIFIED** in the Event filter. They are not candidates for the $1 binary pair engine.

---

## Same book: do not add these as a third venue

| Retail surface | What it actually is | Evidence |
|---|---|---|
| Coinbase predictions, via Coinbase Financial Markets | Kalshi | **VERIFIED.** Coinbase Help: “Prediction markets on Coinbase are operated by Kalshi.” US residents except Nevada; sports unavailable in Michigan, and open sports positions were closed 9 October 2026. https://help.coinbase.com/en/coinbase/trading-and-funding/prediction-markets/intro |
| Webull event contracts, via Webull Futures | KalshiEX, cleared at Kalshi Klear | **VERIFIED.** https://www.webull.com/help/faq/11164 — “Webull provides customers access to eligible Kalshi-listed event contracts.” |
| IBKR access to “KALSHI Event Contracts” | The Kalshi book, with IBKR’s per-contract commission | **VERIFIED** commission table. Not a second book. |
| DraftKings Predictions’ CME markets | CME | **VERIFIED** DraftKings release, 19 December 2025: “beginning with CME Group at launch.” |
| DraftKings’ Crypto.com markets | NADEX / CDNA | **VERIFIED** DraftKings release, 6 February 2026, naming Crypto.com \| Derivatives North America. |
| FanDuel Predicts’ OG contracts | NADEX branded OG, per NADEX’s own fee schedule | **VERIFIED** that FanDuel’s 9 June 2026 release calls OG “Crypto.com’s CFTC-regulated exchange and clearinghouse,” and that the NADEX fee PDF says NADEX “does business under two brands: Crypto.com \| Derivatives North America (CDNA) and OG Prediction Markets (OG).” Whether OG was later separated into a new DCM is **UNKNOWN**: no separate OG row exists on the DCM list retrieved this session. |
| FanDuel / IBKR / DraftKings CME contracts | One CME book | **INFERRED** from the three firms naming CME as the listing exchange. |

Robinhood is **not** in the verified table. `curl` of `https://robinhood.com/us/en/support/articles/event-contracts/` returned **404**. Secondary write-ups quote a Robinhood disclosure that names KalshiEX, ForecastEx, Rothera, and North American Derivatives Exchange. That routing is **INFERRED** until the disclosure itself is retrieved. If it is accurate, only the non-Kalshi routes are extra books, and the Kalshi route is not.

No public evidence in this pass shows Coinbase Derivatives, LLC listing the contracts shown in the Coinbase app. The app’s rules link out to Kalshi. **VERIFIED** (Coinbase Help).

---

## Ranked third-venue candidates

Ranked for **this** tool: an independent book, binary (or proven $1) contracts that can overlap Kalshi and Polymarket US, and a data feed that can support a depth walk. Liquidity and a trading API matter after that. “Best” here means best to instrument next, not largest handle.

| Rank | Venue | Independent of Kalshi and Polymarket US? | Public live depth? | Why this rank |
|---|---|---|---|---|
| 1 | Gemini Titan | Yes | Yes, unauthenticated REST book | Only candidate where a full bid/ask ladder was retrieved with no key, and YES/NO quotes matched `1 −` the other side. |
| 2 | NADEX / CDNA (Crypto.com and OG) | Yes | Top of book only, no size | Real DCM, politics and sports in the live catalog, fee schedule in hand. Documented public API has no quantity. Sample spread was very wide. |
| 3 | ForecastEx | Yes | No. Intraday pair tape, not a book | Clean $1 inverse binary and a known settlement clock. Live book requires an authenticated IBKR session. Weather and macro overlap; election volume was small on 11 Nov. |
| 4 | Rothera | Yes | No live book found. Daily public files | Large 11 Nov print, sports overlap, but the collector cannot see the book. Retail path not verified beyond the files. |
| 5 | CME Event Contract Swaps | Yes | No. Rule PDF fetched; market-data URL returned 403 | Real binaries on a CLOB, used by FanDuel, IBKR, and DraftKings. Payout size is not $1 in the chapter text. Data is not openly readable with this client. |
| 6 | Railbird (DraftKings-owned DCM) | Yes | None found | Exchange is listing NBA binaries at the CFTC as of 6 Nov 2026. DraftKings said in February it would integrate Railbird “in the coming months.” Whether that is done, and whether any book is public, is **UNKNOWN**. |
| 7 | ProphetX | Yes, if the API is this DCM | Host up, **401** without a token | Documented prices and quantities. IPO binaries at the CFTC, so not sports-only. Useless to the unauthenticated collector until a key exists. |
| 8 | Novig / Ludlow (link inferred) | Likely yes | **401**, signature required | Book model matches the plan (bids only; ask = `1 −` other bid). Fee documents conflict. Sports-heavy. |
| 9 | Aristotle Exchange | Yes | None found | Many certified binaries, including parlays. No public market-data API found. Parlays are outside the plan’s standard binary rule anyway. |
| 10 | XV Exchange | Yes | None found | Designated 3 Nov 2026 and already certifying soccer binaries. Too new to treat as a feed. |
| — | PredictIt | Not a DCM | Top of book, **no sizes** | Usable as a capped political tape. Poor as an execution venue for this strategy. |
| — | Coinbase app, Webull, IBKR-Kalshi | No | n/a | Same Kalshi book. |

---

## Venue notes

### Gemini Titan, LLC (`GMNI`)

- **Regulatory.** Designated DCM 10 December 2025. **VERIFIED**, CFTC table. DCO status was **not** checked in the DCO registry this pass. **UNKNOWN.**
- **Retail eligibility and states.** **UNKNOWN.** No eligibility page was retrieved.
- **Book.** Independent. Live `GET https://api.gemini.com/v1/prediction-markets/events?status=active` returned `pagination.total = 1000` and, on the first 50, categories Sports 24, Crypto 10, Weather 10, Commodities 6. **VERIFIED.**
- **Contract shape.** A Houston-vs-Golden-State contract’s `prices` had the shape buy yes / sell yes / buy no / sell no (e.g. 0.60 / 0.59 / 0.41 / 0.40): a one-cent spread with NO equal to `1 −` the YES bid/ask. A natural-gas contract matched the same identity (`buy.no = 1 − sell.yes`). **VERIFIED** on those two payloads. Tick and quantity increment on that contract object: **0.01**. **VERIFIED.**
- **Live book.** `GET https://api.gemini.com/v1/book/GEMI-NBA-2611120230-GSW-HOU-M-HOU` returned **200** with no API key. Bids and asks with `price` and fractional `amount` (e.g. bid 0.59 amount 100.25, ask 0.60 amount 200.50), plus further levels. Those prices match the event payload. **VERIFIED.** The book JSON does not name the amount unit. The event schema’s example text calls `volume` “Total trading volume in USD.” **INFERRED** that `amount` is instrument quantity, because `quantityIncrement` is 0.01 and the amounts are fractional. Do not treat `amount` as dollars without a docs sentence.
- **Volume.** Sum of the `volume` field on the first 50 events was 105,439.07, max 21,361.97. **VERIFIED** as a page statistic, not as exchange-wide volume. **UNKNOWN** how that field is windowed.
- **Rules.** Contract objects include `termsAndConditionsUrl` on `assets.gemini.com`. **VERIFIED** present. The rule text was not downloaded.
- **Trading API.** Docs describe authenticated WebSocket order streams (`orders@account` and similar require a key or bearer). **VERIFIED** from https://developer.gemini.com/prediction-markets/websocket/streams. A retail trading key’s availability is **UNKNOWN**.
- **Fees.** **UNKNOWN.** No fee schedule was retrieved.
- **Settlement timing.** Events carry `expiryDate`. The clock from expiry to cash was **not** in the payload. **UNKNOWN.**
- **Fit.** Best data match for the existing book builder. Overlap is sports, crypto thresholds, and weather, which Kalshi also lists. Politics were not on the first page. Whether politics exist deeper in the 1,000 is **UNKNOWN**.

### NADEX / CDNA / OG

- **Regulatory.** North American Derivatives Exchange, Inc. d/b/a crypto.com, designated 18 February 2004. **VERIFIED.** The 1 August 2026 fee schedule, hosted at the Contentful URL cited below, states the company uses two brands, CDNA and OG. The PDF is watermarked “Must not be disclosed outside CDNA” and was still served to an unauthenticated GET. Treat the fee facts as read from that public URL; do not republish the PDF.
- **Retail.** Direct members and customers of executing-broker FCMs are described in the fee schedule. **VERIFIED** as a membership model. Which US states the Crypto.com app currently allows is **UNKNOWN** (the help page was not retrieved).
- **Public data.** Docs: https://data.crypto.com/docs. Anonymous personal non-commercial use, **100 requests/minute and 50,000/day per IP**. Endpoints listed: events, search, event by id, contracts for an event, and `GET /api/v1/predictions/contracts/{symbol}/price`. No order-book or size field is documented. **VERIFIED.**
- **Live call.** `GET https://data-api.crypto.com/api/v1/predictions/events` returned active events and `has_more: true`. Kinds on the first pages included `ELECT`, `CRYPT`, `CUL`, `EC`, `CFB`, `TENNIS`, `UCL`. **VERIFIED.** A full catalog count was **not** completed.
- **Quote shape.** For symbol `ELECT-USPRESPRTY28-Democratic-…`, the price endpoint at `2026-11-12T02:06:23Z` returned a bid, ask, mid and probability (e.g. bid 0.30, ask 0.70, mid 0.50, probability 70). The list payload’s `yes` equalled that ask and its `no` equalled that bid. **VERIFIED.** So the list field named `no` is **not** a NO-contract ask. `payout_per_100` equals `100 / ask`. **INFERRED** that the ask is the price of a contract that pays 100 units, consistent with the $1 event-contract fee row, but the price JSON does not say “pays $1.”
- **Fees.** Effective 1 August 2026, direct and executing-broker members: **$0.02 per side** on $1 event contracts, or a technology-connectivity weighted fee from **$0.000693 to $0.017500** described as `0.07 × C × P × (1−P)`. Settlement fee on $1 event contracts: **$0.00**. $10 crypto event contracts: **$0.15** per side, including at expiration. **VERIFIED** in https://assets.ctfassets.net/8c2uto3zas3h/7yLNV2LvLmnYGLFYS3T7DE/e635d773ae0eab2bc8cdb871f43124c1/2026-08_NADEX_Fee_Schedule.pdf. Which connectivity a given app uses is **UNKNOWN**, so a collector must not assume $0.02.
- **Settlement timing.** **UNKNOWN** beyond “at expiration” in the fee table.
- **Fit.** Real second book for politics, culture, and sports. The 39-cent spread on that 2028 contract is one observation, not the whole complex. Without size, the plan’s quantity walk cannot qualify a trade. Front ends that route here (DraftKings’ Crypto.com connection, FanDuel’s OG connection, and possibly part of Robinhood) are **this** book, not three books.

### ForecastEx LLC (`FEX`)

- **Regulatory.** DCM designated 24 June 2024. **VERIFIED.** The rulebook also describes ForecastEx as the clearinghouse. **VERIFIED** as the rulebook’s own description (Rule 602).
- **Retail path.** Interactive Brokers LLC states it is a CFTC-registered FCM and an affiliate of ForecastEx, and that it provides access to eligible customers. **VERIFIED**, commission-page disclosure. A direct retail membership path besides IBKR was **not** found. **UNKNOWN.**
- **States.** The same page says Forecast Contracts on US election results “are only available to eligible US residents.” A state-by-state list was **not** on that page. **UNKNOWN.**
- **Mechanism.** Rule 401: each market is a Yes contract and a No contract. Both sides **bid**. A trade exists only when the bids sum to **$1.00**. Collateral posted equals the bid. Minimum increment **$0.01**. Bids in the downloaded rulebook are **$0.01 to $0.99**. **VERIFIED**, rulebook PDF from `data.forecastex.com`, retrieved this session. Notice to Members 2025-166 (effective 5 January 2026) describes expanding the bounds to **$0.01 and $1.00**. The rulebook text retrieved tonight still says $0.01–$0.99, while Rule 403(c) still mentions a **$1.01** pair threshold and then price-improves to $1.00. Those three texts disagree. The **pair tape** is unambiguous: 14 sampled rows of `pairs_20261111.csv` had `yes_price + no_price = 1.00` exactly. **VERIFIED.**
- **Economics that the plan does not model.** Rule 612(c): the clearinghouse passes interest earned on collateral, minus fees, as a **monthly coupon**. An FCM may keep part of it. **VERIFIED.** Rule 604: a participant may not hold Yes and No in the same market at once; offsetting executions must be netted by 16:00 CST. Conditional contracts are excepted. **VERIFIED.** Buying Yes on ForecastEx and No on Kalshi is allowed by this rule because they are different exchanges. **INFERRED.**
- **Settlement.** Rule 603: Yes pays **$1.00** and No pays **$0.00**, or the reverse. Conditional outcome “False” returns the consideration paid, minus transaction fees. Settlement of resolved contracts runs at least daily at **13:00 CST** for contracts resolved after 12:00 CST the previous day and before 12:00 CST the current day. **VERIFIED.** That is a real payout bound, unlike an unknown Polymarket US release time.
- **Fees at IBKR.** ForecastEx contracts: **USD 0.00** IBKR commission, plus **USD 0.01** exchange fee per contract. **VERIFIED.** Exchange fee schedule on forecastex.com was **not** found (`/fees` and `/fee-schedule` returned 404). Any fee inside the $1.00 pair beyond that $0.01 is **UNKNOWN**. Rule 603(b)(2)(iv) says collateral left over after paying the $1 settlement of a pair is a fee. Combined with pairing at exactly $1.00, that leftover is zero on the prices in the tape. **INFERRED.**
- **Public data.** Bucket `https://forecastex-public-data.s3.amazonaws.com` with prefixes `daily_summary/`, `pairs/`, `prices/`. **VERIFIED.** `summary_20261111.csv`: **386,304** total pairs across 113 products. By category: Environmental 235,306; Financial Markets 107,473; Economic Indicators 35,195; Elections 8,260; Conditional 50; Technology 20. **VERIFIED.** `pairs/pairs_20261112.csv` was last modified `2026-11-12T02:00:29Z` and was much smaller than the completed 11 Nov file, so the current-day file is rewritten during the session. The marketing page says pairs refresh every 10 minutes. **VERIFIED** that the page says that; the 10-minute claim was not timed. Columns: `pair_id, event_contract, expiration_date, quantity, yes_price, no_price, pair_time`. This is a **trade print**, not resting depth. It cannot feed the plan’s two-second book check.
- **Live book.** IBKR documents event-contract snapshots and websocket quotes on the Client Portal Web API, and notes that ForecastEx “BID” / “ASK” are not ordinary buy/sell. **VERIFIED** at https://www.interactivebrokers.com/campus/ibkr-api-page/event-contracts/ and https://www.interactivebrokers.com/docs/tws-api/doc/ec/market-data. Those calls require an authenticated IBKR session. **INFERRED** from the API’s general model; this pass did not log in.
- **Fit.** Strong for weather and macro **if** an IBKR read session exists. Weak as an unauthenticated collector. Election overlap on 11 Nov was 8,260 pairs, small next to the weather complex.

### Rothera Exchange and Clearing LLC (`ROTHDCM`)

- **Regulatory.** CFTC remark: after a transaction on 20 January 2026, LedgerX LLC (d/b/a MIAX Derivatives Exchange or MIAXdx) was renamed Rothera Exchange and Clearing LLC. Designation date on the row remains 24 June 2019. **VERIFIED.**
- **What is listed.** Unfiltered product query, not the Event filter: college and pro basketball and football win/spread/total templates certified 16–22 October 2026, plus a Tesla EPS contract and a corporate-metric template in late October 2026. Three pages. **VERIFIED.**
- **Observed activity.** `https://publicfiles.rotheramarkets.com/clearing/outbound/mkt_eod_20261111.csv` has **14,413** symbol rows. Sum of `volume` = **36,332,751**. Symbols with volume greater than zero: **1,995**. Settlement prices present ranged from 0 to 1. Largest product codes that day were NBA and college-football games (for example `NBAGAME-26NOV10OKCPOR` volume 4,889,619). **VERIFIED** as sums of that file. The file does not define whether `volume` counts one contract once or both sides. **UNKNOWN.** Open interest summed to 121,207,592. Same caveat.
- **Public data.** The file index at https://publicfiles.rotheramarkets.com/ lists daily trade gzip files and clearing EOD files, plus contract-term PDFs (college basketball outcomes, spreads, totals, company KPIs). **VERIFIED** that those links exist. No REST order book was found. **UNKNOWN** whether members have one.
- **Retail path and states.** **UNKNOWN** from Rothera’s own files. Robinhood is discussed only as an unverified secondary claim above.
- **Fees and settlement.** **UNKNOWN.** `expiration_date` and `contracts_delivered` are columns. They do not state the payout clock.
- **Fit.** Economically the largest independent print observed in this pass, and the symbols overlap sports that Kalshi lists. The data is end-of-day. It cannot qualify a live arb. It can be a replay/research tape later.

### CME Event Contract Swaps

- **Regulatory.** CME designated 21 December 2000. **VERIFIED.** Chapter 22: cash-settled, European-style, premium-style **binary options**. Minimum increment **$0.01**. ITM holder receives the **Settlement Value** from the Event Contract Swaps Table, which is not in the chapter. **VERIFIED**, https://www.cmegroup.com/rulebook/CME/I/22.pdf (curl got 403 from Akamai; the PDF URL was read by a second client).
- **Payout.** Not established as $1. A FanDuel disclosure hosted by CME uses a **$100** notional as an example of full margin, not as the contract spec. **VERIFIED** that the example exists in search text of https://www.cmegroup.com/fcm/files/event-contracts-disclosure.pdf. That PDF itself returned **403** to curl, so the $100 figure is **not** treated as the spec.
- **Where it trades.** Self-certification 26-054, as quoted by search, says the contracts trade on **CME Globex** on a central limit order book and clear as swaps. The PDF was not saved. **INFERRED** pending a direct read. Certified products seen on the CFTC Event filter include mayor, governor, and Senate election swaps dated 25 August 2026. **VERIFIED.**
- **Retail front ends.** IBKR: USD 0.01 commission + USD 0.01 exchange fee per CME event contract. **VERIFIED.** FanDuel Predicts, 9 June 2026: existing contracts through CME, new sports and entertainment through OG; benchmarks (S&P 500, Nasdaq-100, oil, gas, gold, crypto) described as available in all 50 states. **VERIFIED** from the FanDuel release. DraftKings, 19 December 2025: launch exchange is CME; available across **38 states** at that date, with sports called out for California, Florida, Georgia, and Texas. **VERIFIED** as of that release, not as a November 2026 map.
- **Market data.** Direct curl of the rule PDF and the specs PDF returned 403. No unauthenticated quote endpoint was confirmed. **UNKNOWN** for a free book. CME market data is ordinarily licensed. **INFERRED** from longstanding CME practice, not from a page read tonight.
- **Settlement.** Rule 2202.H: if expiration falls on a weekend or holiday, final settlement is the next exchange business day. The intraday clock is in the external table. **VERIFIED** / **UNKNOWN** respectively.
- **Fit.** A genuine third book for macro and, through FanDuel and DraftKings, sports. Blocked for this project until payout size and a lawful market-data feed are known. Do not count FanDuel and DraftKings CME flow as two books.

### Railbird Exchange, LLC (`REX`)

- **Regulatory.** Designated 13 June 2025. **VERIFIED.**
- **Ownership.** DraftKings, 19 December 2025: “planned rollout of recently acquired Railbird Technologies Inc. and its wholly owned subsidiary, Railbird Exchange, LLC.” **VERIFIED.** DraftKings Predictions itself is an introducing broker, GUS III Inc. d/b/a DraftKings Predictions, not the DCM. **VERIFIED**, same release.
- **Listings.** CFTC Event rows dated 6 November 2026 include `NBAOVERTIME`, `NBAPOINTSRACE`, `NBAMULTIPERIODWIN`, and dated symbols such as `NBAOVERTIME-OKCMIN-25DEC2026`. **VERIFIED.** So the exchange was still certifying contracts nine days before this report. Whether those contracts are on the DraftKings app is **UNKNOWN**. The February 2026 release still said integration would happen “in the coming months.”
- **API, fees, states, settlement.** **UNKNOWN** from Railbird itself. No public host responded in the searches that produced an official data URL.
- **Fit.** The right economic object is Railbird, not “DraftKings.” DraftKings-as-app also shows CME and CDNA. Until a book feed exists, Railbird is a filing, not a collector source.

### ProphetX LLC (`PROPHX`)

- **Regulatory.** Designated 11 June 2026. **VERIFIED.**
- **Listings.** Event binaries dated 10 November 2026 include IPO window and calendar-year contracts (Databricks, Anduril, OpenAI are named in the filing titles). **VERIFIED.** Not sports-only, despite the affiliate docs being sports-shaped.
- **API.** Docs: production base `https://cash.api.prophetx.co/partner`, v4 `GET /{version}/affiliate/get_markets` returns `price` and `quantity`. **VERIFIED** from https://docs.prophetx.co/docs/market-data-integration. Live `GET .../affiliate/get_tournaments` returned **401** `Invalid authorization token`. **VERIFIED.**
- **Fees, states, settlement, age.** **UNKNOWN** in this pass.
- **Fit.** Structurally compatible once a read token exists. Ranked behind venues with either a public book or a proven $1 rule.

### Novig and Ludlow Exchange, LLC (`LEX`)

- **Regulatory.** Ludlow Exchange, LLC designated 16 June 2026. Certified products are NFL and NBA props, awards, and “participation protection,” October 2026. **VERIFIED.**
- **Brand link.** https://docs.novig.com describes a sports exchange and never says “Ludlow” in `llms.txt`. Equating the two is **INFERRED** from the product mix and from secondary lists. Do not hard-code that identity until a Novig or CFTC document says it.
- **Book model.** Catalog docs: the book holds **only bids**; the ask on an outcome is `1 −` the other outcome’s bid; `GET /v3/catalog/markets/{id}/book` returns price and qty, depth 1–20. **VERIFIED** at https://docs.novig.com/api/catalog. This matches the plan’s Kalshi complement math.
- **Auth.** `GET https://api.novig.com/v3/types/leagues` returned **401** `novig-key-id header is required`. **VERIFIED.** Docs require Ed25519 or P-256 signatures on every request, including GET. A `trading::read` key is read-only. **VERIFIED**, https://docs.novig.com/api/signing and the odds-screen page.
- **Fees, and a documentation break.** https://docs.novig.com/api/concepts/fees.md says the taker pays `c · P · (1−P) · N · 1¢`, with `c = 0.06`, maker pays nothing, game markets charged only `WHEN_LIVE`, futures `ALWAYS`. The chart caption’s peak of **$1.50 on 10,000 contracts** equals `0.06 × 0.25 × 10,000 × $0.01`. The same page says “A winning contract pays full value, 1¢.” https://docs.novig.com/fees.md instead says the contract pays **$1.00**, that API `qty` is in minimal units, and `N = qty / 100`. Both pages say the live straight-contract coefficient has been 0.06 since mid-October 2026. **VERIFIED** that the pages say these things. They cannot all be implemented. Payout unit is **UNKNOWN** until one fill is reconciled.
- **Retail age and states.** **UNKNOWN** from the docs read here.
- **Fit.** Good math, sports overlap, no unauthenticated feed, unresolved contract unit. Not a first adapter.

### Aristotle Exchange DCM, Inc. (`ARST`)

- **Regulatory.** Designated 5 September 2025. **VERIFIED.**
- **Listings.** Event binaries dated 6 November 2026 include “Will all of \<components\> occur?” and an example that is a same-day two-game parlay, plus player fantasy-point props. **VERIFIED.** Parlays fail the plan’s “standard binary, exceptions must not break complementarity” rule even if prices look cheap.
- **Relation to PredictIt.** The pending **PredictIT Exchange DCM, Inc.** (5 October 2026) is a different CFTC row from Aristotle. **VERIFIED.** Press claims that Underdog bought Aristotle, or that PredictIt became a DCM in January 2026, were **not** confirmed in the CFTC table. **UNKNOWN** / not adopted.
- **API, fees, states.** **UNKNOWN.** No official market-data URL responded in this pass.

### XV Exchange, LLC (`XVE`)

Designated **3 November 2026**. Soccer binaries (EPL, Serie A, Bundesliga spreads, totals, team props) certified **5 November 2026**. **VERIFIED.** No public API, fee, or eligibility page was found. **UNKNOWN** whether anyone can trade yet. Too new to schedule.

### PredictIt (no-action market, not the pending DCM)

- **Regulatory.** CFTC Letter **25-20**, 14 July 2025, amends Letter 14-130. It allows Victoria University of Wellington to transfer the market to **Prediction Market Research Consortium, Inc.**, removes the 5,000-trader cap, and sets the per-participant investment limit in any one contract to the **FECA** individual contribution limit. The letter states the current FECA limit is **$3,500**, adjusted by the FEC. Scope remains political events, and the letter says the separate economic-indicator permission in 14-130 is unchanged. **VERIFIED**, https://www.cftc.gov/csl/25-20/download. PredictIt is **not** a designated DCM. The DCM application of PredictIT Exchange DCM, Inc. is **pending**. **VERIFIED.**
- **Live data.** `GET https://www.predictit.org/api/marketdata/all/` returned **150** open markets and **545** contracts. Contract fields include `lastTradePrice`, `bestBuyYesCost`, `bestBuyNoCost`, `bestSellYesCost`, `bestSellNoCost`. **No size.** Example: JD Vance best buy yes 0.32, best buy no 0.69, best sell yes 0.31, best sell no 0.68. `dateEnd` on that contract was the string `NA`. **VERIFIED.**
- **Fees.** https://www.predictit.org/fees returned HTML that did not contain a fee percentage or a dollar cap (the body is client-rendered). Current trading and withdrawal fees are **UNKNOWN**. Do not reuse a remembered “10% of profit” figure.
- **Fit.** A real US-person political tape with executable-looking top of book and a hard dollar cap. At a 50-cent price the $3,500 cap is thousands of contracts, so the plan’s default quantity of 100 is inside the cap. **INFERRED.** Without size or a verified fee, it cannot be a qualified leg. It is also not $1-guaranteed by the letter; payout convention was not restated in 25-20. **UNKNOWN.**

### Designated, not shown to be trading event contracts

Sporttrade DCM LLC (30 October 2026), Juice Exchange (24 July 2026), Water Street Labs (16 July 2026), Optex Markets (9 July 2026), Xchange Alpha (30 January 2026), Electron Exchange (29 August 2025), and tZERO DCM (10 November 2026) are designated and did not appear as organizations with certified products in the filter used here. Sporttrade’s earlier life as a state-licensed sports exchange was **not** re-documented from a primary page in this pass. **UNKNOWN** whether any of them are open to retail today.

Coinbase Derivatives, LLC is a DCM and had no Event-category products. It is not the prediction-market product in the Coinbase app.

---

## Excluded and not yet tradable

Out of scope, mentioned only so they are not added later by mistake:

- **Global Polymarket** (CLOB, Gamma, crypto wallets). The US venue is QCX / polymarket.us, already in the plan.
- **Betfair, SX Bet, Stake,** and other offshore sports exchanges that restrict US persons.
- **Smarkets** as it operates today. A US DCM application, Smarkets Board of Trade Exchange LLC, is **pending** (3 March 2026). **VERIFIED** pending. The offshore site is out of scope.
- **Limitless Markets US, LLC**, pending 1 May 2026. Not the offshore Limitless exchange.
- **Manifold** and other play-money or non-CFTC sites. Not checked, not candidates.

Pending DCM applications cannot be execution venues. PredictIt’s **operating** no-action market is the exception that is live; its **DCM application** is not.

---

## Implications for implementation

Do not add a third `VenueId` until the adapter can prove three things: the book is not Kalshi and not QCX, the contract pays $1, and the feed has size or is explicitly labeled tape-only.

1. **`venues/registry.py` (or the market model’s venue field).** Store `listing_exchange`, separate from `broker`. Legal values for a third book, in the order worth building: `gemini_titan`, `nadex`, `forecastex`, `rothera`, `cme_event`, `railbird`, `prophetx`. Do **not** add `coinbase`, `webull`, `robinhood`, `fanduel`, or `draftkings` as exchanges. Those are brokers. A broker record may point at one of the exchanges above, or at `kalshi`.

2. **Same-book guard, before any third adapter.** `qualify_pair()` rejects a pair when both legs resolve to the same `listing_exchange` and the same contract identity. Test `test_coinbase_and_webull_are_kalshi`: fixtures whose rules URL host is `kalshi.com`, or whose help text names KalshiEX, map to `listing_exchange=kalshi` and cannot pair with a direct Kalshi market. Test `test_ibkr_kalshi_fee_path_is_not_a_second_book`: IBKR’s published $0.01 + $0.01 schedule is an `access_path` on Kalshi, not a venue.

3. **First third adapter, only if a third venue is actually approved: Gemini.**
   - Config: `GEMINI_REST_BASE=https://api.gemini.com` (no key for reads).
   - `list_events(status=active)` → `GET /v1/prediction-markets/events`.
   - `get_book(instrument_symbol)` → `GET /v1/book/{instrumentSymbol}`.
   - `normalize_book()`: YES ask = `asks[0].price`, YES bid = `bids[0].price`, NO ask = `1 - yes_bid`, NO bid = `1 - yes_ask`. Test against the saved Houston contract: yes ask 0.69, no buy 0.32.
   - Keep `amount` as `Decimal`. Do not convert to dollars. Test that a requested quantity fails closed when the amount unit is still the unverified flag `gemini_amount_unit=unknown`.
   - Fee function returns **unknown**, so assessments stay scenario-only, matching the plan’s missing-fee rule.
   - Poll well under any limit you have not been given. No public rate limit was found. Start at one book request per watched market per second and back off on 429.

4. **Do not point the collector at CDNA for qualified trades yet.** A `NadexTopOfBook` reader may store bid/ask for research. `qualify()` must return `no_depth`. Map list-field `no` to the YES bid, not to a NO ask, using the Democratic symbol checked above. Fee function takes an explicit `connectivity` argument: `flat_0_02` or `weighted_0_07`, never a silent default. Config comment should cite the 1 August 2026 schedule.

5. **ForecastEx, if used, is a tape.** `ForecastexTapeReader` reads `pairs/pairs_YYYYMMDD.csv` from `https://forecastex-public-data.s3.amazonaws.com`. It must set `book_validity=trade_print` and must not satisfy the two-second book-age check. Settlement metadata for a ForecastEx leg can be filled from Rule 603: payout **$1**, release **13:00 America/Chicago** on the settlement day defined in that rule, confidence **rulebook**. Test `test_pair_prices_sum_to_one` on a fixture of the 11 Nov header rows. Coupon income is an optional scenario, default **excluded**, same as rebates in the plan.

6. **Payout unit is a required field.** `Market.payout_usd: Decimal | None`. Qualification requires `payout_usd == Decimal("1")`. CME stays `None` until the Event Contract Swaps Table row for that product is stored. Novig stays `None` until the fee-doc conflict is resolved. A test named `test_cme_chapter_22_does_not_default_payout_to_one` locks this.

7. **Rothera and Railbird.** A downloader for `publicfiles.rotheramarkets.com/clearing/outbound/mkt_eod_YYYYMMDD.csv` is legitimate research input and must be tagged `session=end_of_day`. It is not a collector source for `scan`. No Railbird client until a base URL exists.

8. **Human review copy.** The pair screen should show `listing_exchange` and the rules host, not the broker brand. A DraftKings or FanDuel URL is not sufficient evidence of which book was traded.

9. **Rate and auth budgets.** Gemini public reads worked with no key. Novig and ProphetX will not. Do not block the Kalshi and Polymarket US milestones on obtaining those keys.

---

## Open questions

1. Gemini: is book `amount` contracts or quote currency, what is the fee, which states and ages may trade, and how soon after `expiryDate` is cash credited?
2. CDNA: is there a depth or size endpoint outside the published market-data list, which apps use the $0.02 schedule versus `0.07 × P × (1−P)`, and is OG still the same matching engine as CDNA?
3. ForecastEx: does the rulebook PDF or a later notice control the $1.00 bid, and what does IBKR display as the executable Yes price relative to the resting No bid?
4. Rothera: who can send retail orders, is there a live book API, and does `volume` double-count a pair?
5. CME: what is the Settlement Value on the Event Contract Swaps Table for the contracts FanDuel and IBKR actually list, and is there a non-display market-data entitlement that is still lawful for a local recorder?
6. Railbird: is it reachable inside DraftKings Predictions as of 11 November 2026, or only certified at the CFTC?
7. Novig: which fee page matches production fills, is the payout $1 or 1¢, and does a Novig document name Ludlow Exchange, LLC?
8. Aristotle, XV, Sporttrade, Juice, Optex, Water Street, Xchange Alpha, Electron, tZERO: is any of them open to a US retail order today, and if so under what API?
9. PredictIt: what are the current profit and withdrawal fees, and did the PMRC transfer in Letter 25-20 actually close?
10. Robinhood: the primary disclosure page was not retrieved. Which of Kalshi, ForecastEx, Rothera, and NADEX is attached to a given contract id?
11. State maps change faster than this report. Every venue’s “where a US person may open a position” needs a dated primary page before a pair is approved, not a national assumption.
