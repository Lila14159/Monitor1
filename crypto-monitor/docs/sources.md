# Sources, keys, and coverage

This repository uses public data GET requests, configured official feeds, and a clearly separated secondary news feed for digest leads. Source identity, timestamps, fresh data, and materiality are checked separately. A successful request is not proof that the source covers all relevant events. The run summary displays an explicit coverage entry for missing, rejected, disabled, or unsupported data.

Provider documentation was checked on 25 October 2026. Plans, quotas, URLs, and schemas can change; your provider dashboard and the linked official documentation take precedence.

## Market and exchange universe

| Source | Use | Credentials and bounds |
|---|---|---|
| CoinGecko `/coins/markets` | Dynamic market-cap ranks 1–50, USD prices, with BTC/ETH requested separately if absent | Recommended `COINGECKO_DEMO_API_KEY`; no private exchange credentials |
| CoinGecko `/exchanges` | Retrieve a bounded page of up to 250 and select verified trust-score ranks 1–10 | Same Demo key; ranking is by trust score, not trading volume |
| Kraken public `/0/public/Trades` | Independent USD trade observations for explicitly mapped stablecoins currently in the universe | No key; at most 20 requested recent trades per configured pair |

Coins are identified by CoinGecko IDs rather than ticker alone. The initial run requires all top-50 rank positions, BTC and ETH, and all top-10 exchange rank positions. Later missing/stale rows are flagged instead of being invented. Exchange rankings have no per-row publication timestamp, so the summary states when they were fetched. Rank does not establish an exchange's safety. [CoinGecko market endpoint](https://docs.coingecko.com/reference/coins-markets), [exchange fields](https://docs.coingecko.com/reference/exchanges), [Kraken recent trades](https://docs.kraken.com/api/docs/rest-api/get-recent-trades/).

Use a **Demo** key for `api.coingecko.com`. A Pro key uses a different hostname and is not interchangeable with this configuration. CoinGecko currently lists **100 calls/minute and 10,000 monthly call credits** for its Demo plan. Your account dashboard and current terms control. Keyless use is IP limited, which is less predictable on shared GitHub runners; failed requests also count against the per-minute rate limit. [CoinGecko plan limits](https://www.coingecko.com/en/api/pricing), [authentication and rate-limit behavior](https://docs.coingecko.com/docs/errors-and-rate-limits).

With the default schedule, there are 26 planned runs/day. Normal market collection uses two CoinGecko requests/run, or approximately **1,612 requests in a 31-day month** before retries, manual runs, or an extra BTC/ETH request. This estimate is derived from this repository's call pattern, not a provider allowance.

## Official announcement coverage

The included feeds are [SEC press releases](https://www.sec.gov/news/pressreleases.rss), [CFTC press releases](https://www.cftc.gov/RSS/RSSGP/rssgp.xml), [FCA news](https://www.fca.org.uk/news/rss.xml), [Ethereum Foundation](https://blog.ethereum.org/en/feed.xml), and [Bitcoin Core](https://bitcoincore.org/en/rss.xml). Only configured exact item domains are accepted; feed contents never determine a destination to fetch. Articles themselves are not crawled. Missing dates or malformed items are rejected and reported.

These feeds provide **partial** regulatory, ETF, project-security, and institutional-announcement coverage. They do not cover every jurisdiction, SEC filing, issuer release, top-50 project's security event, corporate treasury purchase, custodian, OTC transaction, or on-chain transfer. The default configuration contains no dedicated corporate-institutional issuer feed; the institutional classification is available for reviewed official feeds added later. An item labeled security still needs explicit impact evidence to cause a strict alert.

The configured [Decrypt RSS feed](https://decrypt.co/feed) supplies secondary leads for the same priority categories. It is explicitly `primary: false`: it can supply dated digest items but cannot establish a strict news alert, regardless of apparent severity. Generic price predictions, opinion, tutorials, and promotional items are suppressed. Secondary reporting does not fill the missing official-source coverage for a project or exchange.

Official status mappings are included for Coinbase, Kraken, Crypto.com, and Gemini, and are fetched only when that exchange appears in the current top 10. Every unmapped top-10 exchange is shown as unavailable. This is a current top-10 **inventory plus partial official incident coverage**, not a promise of complete incident coverage for all ten. These [Coinbase](https://status.coinbase.com/), [Kraken](https://status.kraken.com/), [Crypto.com](https://status.crypto.com/), and [Gemini](https://status.gemini.com/) status services may themselves omit events or classify impact conservatively.

RSS/status endpoints need no API key but can rate limit, redirect, or deny automated requests. Requests are bounded and redirects are rejected. If an official feed moves, review the new HTTPS domain and update both its fetch URL and allowed item domains; never replace it with an unverified mirror just to remove an error.

The optional `SEC_USER_AGENT` Actions secret supplies your application identity and real contact email for the SEC request only. It is not authentication and does not guarantee access. Supply your own identifying details; a 403 can persist even with this header, including on shared runner IPs. The feed remains explicitly unavailable if denied. [SEC automated-access guidance](https://www.sec.gov/about/webmaster-frequently-asked-questions).

## USD stablecoin depegs

Only the explicit USD-1 peg mappings in `config.json` are eligible, and only while the asset is in the fresh universe. CoinGecko supplies the aggregate price; a separate Kraken response supplies the median of at least three fresh USD trades. The adapters validate the exact configured Kraken response-pair name and trade timestamps. A missing/delisted/thin pair disables that asset's strict depeg detection.

Both prices must deviate by at least 1% in the same direction on two distinct observations, at least 30 and no more than 120 minutes apart. A confirmed 5% deviation is critical. Recovery requires repeated observations within 0.5%. Snapshot age is at most 20 minutes and quote age at most 10 minutes. The 09:00 and 09:17 runs cannot by themselves satisfy the 30-minute confirmation interval. A brief depeg between polls can be missed.

New/unmapped stablecoins and non-USD pegs are not guessed. CoinGecko aggregates can include Kraken data, so the two feeds are separately retrieved observations, not a guarantee of statistically independent pricing.

## Optional CoinGlass liquidation and ETF data

Both adapters are disabled by default. Add `COINGLASS_API_KEY` as a GitHub Actions secret and enable the relevant setting in `config.json`. The key is sent only to `open-api-v4.coinglass.com` in the `CG-API-KEY` header. Entitlement/rate-limit failures appear as unavailable; no proxy estimates replace missing values. [CoinGlass authentication](https://docs.coinglass.com/reference/getting-started-with-your-api), [response and rate-limit documentation](https://docs.coinglass.com/reference/responses-error-codes).

| Adapter | Endpoint and scope | Required access |
|---|---|---|
| Measured liquidations | `/api/futures/liquidation/aggregated-history`; BTC and ETH; last completed UTC hour; configured venues default to Binance, OKX, Bybit | Currently Startup or higher for `interval=1h`; Hobbyist's minimum is 4h |
| Daily ETF flows | `/api/etf/bitcoin/flow-history` and `/api/etf/ethereum/flow-history`; latest prior UTC date no older than seven days | Currently Hobbyist or higher, subject to current account entitlement |

The liquidation adapter sums reported long and short liquidation USD values for exactly one completed hour. A missing, duplicate, stale, or malformed interval is rejected. Strict thresholds default to BTC $100m / ETH $50m; three times the respective threshold is critical. It does not treat a price drop, trading volume, open interest, or heatmap as realized liquidations. Scope is the configured venue set, not the entire dynamic top 10 or global market. Only the last completed UTC hour is eligible; its end may be up to 59 minutes old when a delayed run starts. Older or partial buckets are rejected. [CoinGlass liquidation schema and interval limits](https://docs.coinglass.com/reference/aggregated-liquidation-history).

ETF aggregates are always **provisional and digest-only** because the provider response does not establish finality. Messages include the dated aggregate and count of missing issuer values. Initial materiality defaults to absolute BTC daily flow of at least $100m or ETH $50m; material revisions can produce dated updates. Tiny revisions or unchanged totals do not produce notifications. These are daily fund flows, not intraday real-time institutional flows, and are not inferred from assets under management. [Bitcoin ETF schema](https://docs.coinglass.com/reference/etf-flows-history), [Ethereum ETF schema](https://docs.coinglass.com/reference/ethereum-etf-flows-history).

Each enabled adapter makes two calls/run with the default BTC/ETH settings: about **1,612 monthly requests per adapter**, or **3,224 for both** in a 31-day month before retries/manual runs. This repository sets its own run budget; it does not override provider quotas or purchase API access.

## Extending coverage

Add only official, timestamped sources with a reviewed publisher/domain mapping, documented schema, bounded request count, and offline fixtures. Validate how each source reports corrections and incident resolution. Keep unavailable coverage visible until the new adapter is tested. Adding an issuer RSS feed can improve institutional-announcement coverage but does not create comprehensive institutional flow data.
