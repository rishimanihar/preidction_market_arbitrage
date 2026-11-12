# Short-term prediction-market arbitrage research plan

**Intended repository file:** `plan.md`  
**Documentation reviewed:** November 11, 2026  
**Selected venues:** Kalshi and **Polymarket US**, as confirmed in this conversation.

## 1. Recommendation and verified findings

Build a local Python application that continuously records both venues’ order books, evaluates human-approved market pairs, and supports deterministic replay. Use **direct exchange APIs initially**, with live order submission absent from the observation and simulation applications.

The strategy is conditional on equivalent settlement and successful execution of equal contract quantities. A displayed price discrepancy alone does not establish a profitable trade.

### Existing project and references

The local repository contains only `.git`; there is no application code or project convention to reuse.

The reference repository uses JavaScript and `pmxtjs`. Its calculation considers both directions, but its execution code sizes each leg from an equal dollar allocation, producing unequal contract counts when prices differ. It also treats order submission success as trade success. Reuse the functional idea, not its execution implementation. [Calculation source](https://raw.githubusercontent.com/realfishsam/prediction-market-arbitrage-bot/main/src/arbitrage.js), [execution source](https://raw.githubusercontent.com/realfishsam/prediction-market-arbitrage-bot/main/src/bot.js).

The Predictefy product page returned **403 Forbidden**. Its scanner documentation was accessible and describes checks for executable prices, depth, fees, freshness, and settlement equivalence. It also distinguishes similar wording from verified rules. Its arbitrage feed requires Pro or above. These are useful product requirements; its qualification verdict will not replace our human review. [Predictefy scanner documentation](https://docs.predictefy.com/guides/cookbook/arbitrage-scanner/).

### Direct APIs versus PMXT

| Option | Verified capabilities and constraints | Decision |
|---|---|---|
| Direct Kalshi | Public REST market data; authenticated order-book streams; authenticated orders; native market rules and fee metadata. | Use initially. |
| Direct Polymarket US | Public REST discovery and books; authenticated market streams; official Python SDK; authenticated order management. This is a different API from global Polymarket. | Use initially. |
| PMXT self-hosted | Python/TypeScript interfaces and a local `pmxt-core` server. Published venue targets include both Kalshi and `polymarket_us`, but method support varies. The local SDK is advertised as free and MIT licensed. | Reconsider after a capability comparison against native adapters. |
| PMXT hosted | Published catalog ingestion includes Kalshi but omits Polymarket US. Hosted trading documentation lists Polymarket global, Opinion, and Limitless as supported execution venues; it does not establish hosted execution for our pair. | Do not make it a first-build dependency. |

Sources: [Kalshi books](https://docs.kalshi.com/getting_started/orderbook_responses.md), [Polymarket US API](https://docs.polymarket.us/api-reference/introduction), [PMXT venues](https://www.pmxt.dev/docs/concepts/venues), [PMXT hosted execution](https://www.pmxt.dev/docs/concepts/hosted-trading), [PMXT pricing](https://www.pmxt.dev/pricing).

PMXT’s documented hosted plans are Free, Starter at $29.99/month, and Pro at $99.99/month, with respective monthly allowances of 25,000, 250,000, and 1,000,000 credits. REST calls cost one credit and WebSocket messages 0.1 credits. Continuous recording therefore needs a measured usage estimate. [PMXT limits](https://www.pmxt.dev/docs/rate-limits).

No separate data subscription price was established from the direct API pages reviewed. Budget for operating costs and trading fees; do not interpret that documentation gap as a guarantee of unrestricted free access.

## 2. Architecture and smallest implementation milestone

### Application structure

Use Python 3.12, `asyncio`, `httpx`, `websockets`, `cryptography`, Pydantic, SQLite, and Streamlit. Pin dependencies during implementation.

Run two local processes:

- **Collector:** discovery, metadata refresh, authenticated streams, book reconstruction, qualification, calculations, and recording.
- **Review application:** Streamlit pages for pair approval, ranked observations, historical opportunity episodes, and collector health.

Closing the review application must not stop collection. Bind the UI to localhost.

Use SQLite with WAL and a single writer for structured records. Store compressed, append-only raw messages separately, with periodic book checkpoints for replay. Keep runtime data outside the OneDrive-synchronized checkout; configure its location explicitly. Exclude credentials and runtime data from Git.

Provide three application commands: `scan`, `review`, and, in the second milestone, `replay`. Observation and replay must not construct an order-submission client.

The shared internal interfaces should represent:

- **Market:** venue identifiers, outcome meaning, status, native price conventions, rules, links, timestamps, and fee metadata.
- **Book:** normalized YES/NO acquisition ladders, native levels, source timestamps, receipt timestamps, connection generation, and validity.
- **Pair review:** both markets, outcome mapping, rule comparison, approval, reviewer, and approved content hashes.
- **Assessment:** direction, requested quantity, fill ladder, fees, profit, capital, qualification, and rejection reasons.
- **Episode and simulated execution:** linked observations, timing, fills, exposure, and accounting.

Use `Decimal` for prices, quantities, fees, and money. Preserve raw responses alongside normalized values.

### Data sources and price normalization

**Kalshi**

Use REST markets/events/series for discovery, rules, timing, and fees; use native order-book streams for depth. REST books expose YES and NO bids. Derive executable YES asks from `1 − NO bid`, and NO asks from `1 − YES bid`, retaining corresponding sizes. These are executable complementary offers, not complements of midpoint prices. [Kalshi order books](https://docs.kalshi.com/getting_started/orderbook_responses.md).

Explicitly set the stream price convention. Prefer `use_yes_price=true`, then normalize that convention separately from legacy REST bid arrays. Never apply the complement twice. [Kalshi price conventions](https://docs.kalshi.com/getting_started/order_direction).

**Polymarket US**

Use public REST discovery and market-detail endpoints, and `/v1/markets/{slug}/book` for snapshots. The documented public gateway is `https://gateway.polymarket.us`; authenticated services use `https://api.polymarket.us`. [Market endpoints](https://docs.polymarket.us/api-reference/sdks/python/markets), [book endpoint](https://docs.polymarket.us/api-reference/markets/get-market-book).

Polymarket US has one YES instrument. Buying NO economically means opening a collateralized short of YES:

- YES acquisition uses native YES offers.
- NO acquisition uses `1 − native YES bid`, with the bid’s available quantity.

Track the native short, sale proceeds, locked collateral, and net buying-power consumption separately. Do not use global Polymarket token IDs, wallets, Gamma, or CLOB authentication. [US order model](https://docs.polymarket.us/concepts/orders.md), [collateral mechanics](https://docs.polymarket.us/market-structure/collateral-and-margin.md).

### Smallest end-to-end milestone

Start with **one manually supplied pair**, then support up to ten watched pairs:

1. Resolve both market identifiers and fetch their rules and metadata.
2. Present a side-by-side review and record explicit human approval.
3. Collect both books continuously and evaluate both directions at the configured quantity.
4. Persist raw data, assessments, rejection reasons, and opportunity episodes.
5. Show a ranked live table and searchable history with market links.
6. Recover after a forced disconnect and process restart.
7. Run for 24 hours without submitting orders.

Success means trustworthy collection and reproducible calculations. Finding no profitable opportunities is a valid result.

### Streaming, authentication, and recovery

Kalshi WebSocket handshakes require signed authentication. Current documentation supports Ed25519 and RSA-PSS keys; use the selected key’s documented signing scheme. Polymarket US streams require API-key authentication with Ed25519 signatures over timestamp, method, and path. Use least-privilege keys where available, without assuming either venue provides a suitable read-only scope. [Kalshi authentication](https://docs.kalshi.com/getting_started/api_keys.md), [Kalshi streaming](https://docs.kalshi.com/getting_started/quick_start_websockets.md), [US authentication](https://docs.polymarket.us/api-reference/authentication).

For Kalshi, consume the initial snapshot and subsequent deltas in subscription sequence order. A sequence gap invalidates affected books. Request fresh snapshots for the subscription or reconnect and resubscribe; replace local books before applying subsequent deltas. A repeated subscription alone does not resynchronize existing markets. [Kalshi recovery semantics](https://docs.kalshi.com/websockets/orderbook-updates.md).

For Polymarket US, subscribe to full market data, not the lightweight price feed. The documentation describes full-book responses, so replace the local book on each complete response. After reconnecting, wait for a fresh complete response before qualification. Do not splice old state into a new connection. [US market stream](https://docs.polymarket.us/api-reference/websocket/markets).

The US overview and channel examples differ in field casing and enum representation. During the initial adapter check, verify the pinned official SDK and captured wire messages; reject unsupported schemas. Public documentation reviewed does not establish resumable replay or a sequence-gap mechanism for this stream. Record that limitation rather than inventing one. Follow documented heartbeat handling. [US WebSocket overview](https://docs.polymarket.us/api-reference/websocket/overview).

For both adapters:

- Reconnect with jittered exponential backoff, starting at one second and capped at 30 seconds.
- Mark books invalid immediately on disconnect, parser failure, impossible depth, or recording backpressure.
- Track source time, local UTC receipt time, monotonic receipt time, last complete snapshot, and transport health separately.
- A heartbeat does not refresh a book’s observation timestamp.
- Default maximum book age: **two seconds**; maximum difference between the two book observation times: **one second**.
- Recheck freshness on a timer even when neither market updates.
- Use REST snapshots as an explicitly labeled polling fallback. Never merge them into a delta stream without a documented ordering boundary.

Use venue-specific request budgets. Kalshi’s current Basic read budget is 200 tokens/second, with most requests costing ten tokens; obtain account limits and endpoint costs when authenticated. Polymarket US documents a general retail limit of 25 requests/second shared by source IP and Cloudflare location. Start well below these limits, honor throttling, and reserve capacity for recovery. [Kalshi limits](https://docs.kalshi.com/getting_started/rate_limits.md), [US limits](https://docs.polymarket.us/api-reference/rate-limits).

## 3. Matching, timing, calculation, and review

### Human-approved market matching

Accept market URLs or identifiers first. Add discovery suggestions using event identity, participant, date, market type, threshold, and outcome polarity. Text similarity can suggest a pair; it cannot approve one.

Require review of:

| Dimension | Examples of differences to flag |
|---|---|
| Event identity | Different game, publication, election stage, or observation period |
| Outcome definition | YES polarity, strict versus inclusive thresholds, rounding |
| Resolution source | Different index, publisher, station, league, or backup source |
| Timing | Timezone, cutoff, averaging window, early closure |
| Exceptions | Postponement, cancellation, abandonment, tie, push, void |
| Finality | Corrections, certification, revisions, alternative payouts |

Store the complete available rules, referenced contract terms, review notes, and content hashes. Approval applies to those versions. Changed rules or outcome definitions revoke qualification until reviewed again.

Start with standard binary contracts paying $1/$0. Exclude combos, scalar contracts, partial settlement structures, and unresolved exception mismatches.

A pair’s review must include a payout table for ordinary YES/NO outcomes and specified exceptional outcomes. If an exception can break the complementary payoff, classify the pair as unqualified rather than merely discounting its profit.

### Resolution and payout timing

Store separately for each venue:

- Trading cutoff.
- Expected event resolution time.
- Expected payout or buying-power release time.
- Latest stated resolution bound, if supplied.
- Actual resolution and settlement observations.
- Estimate provenance and confidence.

Kalshi exposes expected/latest expiration fields and settlement timing metadata. Polymarket US exposes market end dates, while its settlement documentation says balances update automatically after finalized resolution; this does not establish an exact payout deadline. [Kalshi market metadata](https://docs.kalshi.com/api-reference/market/get-market.md), [US market metadata](https://docs.polymarket.us/api-reference/markets/get-market-by-slug), [US settlement](https://docs.polymarket.us/learn/markets/contract-settlement).

Default the discovery horizon to **seven days based on expected resolution**, using the later estimate across both legs. Unknown resolution timing remains outside qualified short-term results.

Display payout timing independently. An unknown payout time must remain visibly unknown; never substitute event end time. Such observations may qualify on price and rules but cannot establish a seven-day capital-recycling claim. Provide an optional strict filter requiring a known payout estimate within the horizon.

### Executable calculations

Evaluate both directions independently:

- **A:** Kalshi YES plus Polymarket US NO.
- **B:** Polymarket US YES plus Kalshi NO.

For equal quantity \(q\), walk each acquisition ladder from cheapest to most expensive:

\[
C_v(q)=\sum_i p_{v,i}x_{v,i},\qquad \sum_i x_{v,i}=q
\]

For approved complementary $1 contracts:

\[
\begin{aligned}
Capital(q)&=C_K(q)+C_P(q)+F_K(q)+F_P(q)+B_{\text{entry}}\\
NetProfit(q)&=q-Capital(q)-B_{\text{settlement}}\\
ROC(q)&=NetProfit(q)/Capital(q)
\end{aligned}
\]

Fees must be calculated from the actual level prices and applicable fill-rounding rules, not simply the volume-weighted average price.

`B` represents explicitly configured additional costs. Report trading-fee net profit separately from any stressed cost estimate. Unknown required costs must not silently become zero.

Report both requested-size profitability and the largest profitable size supported by observed depth, bounded by the configured scan limit. Insufficient depth at the requested size remains a failed assessment even if a smaller size would work.

### Current fee treatment

Kalshi’s standard taker model is `0.07 × multiplier × quantity × p × (1−p)`, with exceptions by series. Retrieve fee type, multiplier, waivers, and effective changes. Current API documentation distinguishes direct-member balance precision from non-direct-member precision and describes an order-level rounding accumulator. Resolve the applicable account model before qualification. [Fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf), [series metadata](https://docs.kalshi.com/api-reference/market/get-series.md), [rounding](https://docs.kalshi.com/getting_started/fee_rounding.md), [fee changes](https://docs.kalshi.com/api-reference/exchange/get-series-fee-changes.md).

Polymarket US standard taker fees use `theta × quantity × p × (1−p)`. Published coefficients range from 0.095 to 0.0695 by tier, with onboarding exceptions. Fees use half-even cent rounding and a cumulative order cap across fills. Verify the account coefficient and effective time; exclude anticipated rebates from baseline profit. [US fee schedule](https://docs.polymarket.us/fees.md).

Store fee source, retrieval time, effective period, account assumptions, and model version with each assessment. Unknown account fees produce an indicative scenario, not a qualified opportunity. Where aggregated depth conceals fill fragmentation, use a documented conservative fee bound.

### Worked example

Illustrative prices, not a discovered opportunity. Assume 100 contracts available on both legs, equivalent rules, standard fees, Kalshi direct-member rounding, and a verified US coefficient of 0.095.

| Item | Calculation | Amount |
|---|---|---:|
| Kalshi YES | 100 × $0.45 | $45.00 |
| Polymarket US NO | Short YES at $0.50; net collateral consumption 100 × $0.50 | $50.00 |
| Kalshi taker fee | 0.07 × 100 × 0.45 × 0.55 | $1.7325 |
| US taker fee | Half-even rounding of 0.095 × 100 × 0.50 × 0.50 | $2.38 |
| Capital consumed | Prices plus fees | **$99.1125** |
| Complementary terminal value | 100 × $1 | **$100.00** |
| Estimated net profit | $100 − $99.1125 | **$0.8875** |
| Return on capital | $0.8875 ÷ $99.1125 | **0.8954%** |

The apparent five-cent gross edge becomes less than one cent per paired contract after fees. Additional costs reduce it further. Short-position collateral release is represented economically in terminal value; it must not be counted again as separate profit.

Do not present annualized returns as expected performance. Also report portfolio return against all allocated cash, including idle balances on both venues.

### Ranked view and recorded episodes

Rank qualified observations by net dollars at the requested size, then ROC. Keep unqualified observations in a separate filterable view with all reasons shown.

Record pair, direction, quantity, available size, per-leg levels and average prices, fees, profit, capital, ROC, book ages, resolution/payout estimates, rule versions, and links.

An episode is a continuous observed qualifying interval for one pair, direction, quantity, and model version. Record first seen, last seen, observed duration, peak profit, size range, and termination reason. Data gaps terminate episodes; do not count downtime as persistence. Label polling durations as sampled observations.

## 4. Observation, simulation, and the live-trading gate

### Evidence collection

After the 24-hour reliability milestone, collect for **at least 30 calendar days**, retaining all assessments—including negative and unqualified results—for watched pairs.

Record every received book update, connection transition, metadata change, and periodic checkpoint. Continue tracking watched events through settlement even after they leave the discovery horizon.

Measure:

- Eligible pair count and human-review rejection reasons.
- Collection uptime, missing intervals, source age, inter-venue skew, and recovery time.
- Qualified episode frequency, duration, net edge, and profitable depth.
- Opportunity survival after increasing execution delays.
- Expected versus actual resolution and payout timing.
- Capital use, concentration, operational costs, and dependence on individual events.

### Replay milestone

Replay recorded messages through the same book builder and calculator. Decisions may use only information received by simulated decision time.

Test per-leg delays of **100 ms, 250 ms, 500 ms, one second, and two seconds**, including asymmetric delays and jitter. At each simulated arrival, consume the reconstructed book available at that point, subject to price limits.

Simulate both FOK and IOC policies, including rejection, timeout, partial fill, and delayed cancellation. Maintain separate venue cash balances and reserve capital for outstanding orders. Do not repeatedly spend the same balance or reuse the same visible liquidity across overlapping simulated orders.

Report:

- Triggers that disappear before arrival.
- Both-leg fills, neither-leg fills, partial fills, and one-sided fills.
- Unmatched contracts, exposure duration, and maximum cash at risk.
- Hedge or unwind costs and fees.
- Realized settlement P&L and unresolved positions separately.
- Results under visible-depth assumptions and depth haircuts of 25% and 50%.

Aggregated public books cannot prove queue priority, competing taker behavior, hidden liquidity, internal exchange latency, or live fill certainty. Replay establishes sensitivity and plausible outcomes under explicit assumptions. Feed gaps make affected trials inconclusive; they are not successful fills.

### Decision to proceed

Advance only if all of the following hold:

- Acceptance checks pass and the collector completes seven consecutive stable days.
- At least 30 days of observation and 30 distinct resolved approved events are available; otherwise label evidence insufficient.
- Net simulated results remain positive after fees, measured operating costs, and one-sided-fill losses under the chosen conservative scenario.
- Results are not driven by one event or by duplicated observations of the same episode.
- Capital and payout delays are acceptable using actual observed settlement experience.
- Out-of-sample results support the same conclusion after thresholds are fixed.
- A user review approves the evidence and a separate live risk budget.

Report uncertainty using event-level summaries or resampling, not thousands of correlated book updates as independent trades. These criteria justify considering a small live pilot; they do not establish guaranteed profitability.

### Later automatic execution

Implement execution as a separate, explicitly enabled component after the evidence review.

Both venues document authenticated limit orders with IOC/FOK controls. Kalshi V2 quotes orders on the YES scale; the US adapter must explicitly map long/short intent and native price conventions. [Kalshi orders](https://docs.kalshi.com/api-reference/orders/create-order-v2), [US orders](https://docs.polymarket.us/api-reference/orders/create-order).

Require:

- Funded accounts, verified permissions, fee settings, and fresh approved pairs.
- Maximum contracts, per-market exposure, total exposure, daily loss, and outstanding-order limits.
- Per-leg limit prices and a combined worst-case cost ceiling.
- Durable order intents, venue IDs, fill deduplication, and crash recovery.
- Private order/fill streams with REST reconciliation.
- No assumption that an acknowledgement means a fill.
- No blind retry after an ambiguous timeout.
- Kill switch, startup reconciliation, and automatic suspension after inconsistent state.

There is no cross-venue atomic order. Even two FOK orders can leave one leg filled.

On unequal fills, stop new entries, determine actual fills, and cancel remaining orders. Attempt to hedge only the unmatched quantity within a separately approved emergency price and loss cap. If unavailable, attempt a bounded unwind. If neither is possible, retain and prominently report the exposure, suspend trading, and request operator action. Never chase prices without limits or describe the position as hedged.

Live limits default to zero until explicitly configured. Observation and replay remain unable to submit orders.

## 5. Acceptance checks and chosen defaults

### Acceptance checks

| Scenario | Required result |
|---|---|
| Stale book or excessive observation skew | Qualification expires automatically with a specific reason. |
| Healthy heartbeat but old book | Transport health does not make the quote fresh. |
| Kalshi sequence gap | Stop qualification, rebuild from fresh snapshots, and reproduce correct depth without duplicate deltas. |
| US reconnect | Discard previous connection state and wait for a complete fresh book. |
| Thin book | Fail the requested quantity; show any smaller valid quantity separately. |
| Missing or expired fees | Net profit is unknown or explicitly scenario-only; never assume zero. |
| Fee rounding and multiple fills | Match documented account precision and cumulative rounding behavior. |
| Settlement mismatch | Block qualification even if titles and prices look favorable. |
| Approved rules change | Invalidate approval and require review of the new version. |
| NO price normalization | Native YES bids produce correct NO costs and quantities on each venue. |
| One-sided or partial fill | Report unmatched exposure and include bounded recovery costs in P&L. |
| Order timeout or duplicate update | Reconcile without duplicate orders or duplicate fills. |
| Recorder failure or restart | Mark the evidence gap and terminate active episodes. |
| Delayed payout | Keep capital locked in simulation until release; do not recycle it at event end. |
| Observation/replay safety | Assert that no order submission endpoint can be called. |

Use fixture-based adapter tests, arithmetic tests, deterministic replay tests, and forced-disconnection integration checks. Authenticate only for read operations during the first two milestones.

### Defaults and remaining decisions

| Setting | Initial choice |
|---|---|
| Venues | Kalshi and Polymarket US |
| Deployment | Local Python collector plus localhost Streamlit review |
| Initial universe | Manual watchlist; one pair first, up to ten |
| Matching | Human approval tied to current rule hashes |
| Horizon | Seven days to expected resolution; payout timing displayed separately |
| Contract type | Standard binary $1/$0 only |
| Requested scan size | 100 contracts; additional size views at 1 and 10 |
| Qualification | Positive net profit plus all data, rules, and fee checks |
| Paper entry threshold | At least $0.50 net profit and 0.5% ROC at the chosen size |
| Book freshness / skew | Two seconds / one second |
| Paper capital | $500 per venue, tracked separately |
| Fee assumptions | Verified account settings; unknown settings remain unqualified |
| Rebates | Excluded from baseline |
| Live execution | Disabled; position limits zero |
| Retention | 30 days raw books; retain review, episode, and simulation summaries |
| Time handling | UTC storage; America/Chicago display with timezone labels |

No further product decision is needed for the first build. Development must verify streaming credentials, actual account fee settings, and US wire-format behavior. If credentials are unavailable, deliver the public REST scanner with a visible polling limitation. Choosing live capital, acceptable emergency losses, or broader market coverage is deferred until the evidence review.
