"""Bounded, read-only public data adapters. Missing data is never zero/healthy.

Network I/O is delegated to the allowlisted GET-only Client. Provider text is
untrusted input; only configured feed domains establish provenance. No scraping
of article pages, transaction calls, exchange credentials, or model inference.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
import math
import os
import re
from statistics import median
from urllib.parse import urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

UTC = timezone.utc
CG = "https://api.coingecko.com/api/v3"
KRAKEN = "https://api.kraken.com/0/public/Trades"
GLASS = "https://open-api-v4.coinglass.com/api"
MAX_FEED_BYTES = 2_000_000
MAX_FEED_ITEMS = 250
MAX_NEWS_AGE = timedelta(hours=72)
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def timestamp(value) -> datetime:
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("invalid timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        result = parsedate_to_datetime(value)
    if result.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return result.astimezone(UTC)


def number(value, *, minimum=None, maximum=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise ValueError("invalid number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("nonfinite number")
    if minimum is not None and result < minimum:
        raise ValueError("number below bound")
    if maximum is not None and result > maximum:
        raise ValueError("number above bound")
    return result


def identifier(value) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("invalid identifier")
    return value


def clean(value, limit=1200) -> str:
    if not isinstance(value, str):
        return ""
    # Feed descriptions often contain HTML. No HTML is rendered or fetched.
    text = re.sub(r"<[^>]*>", " ", value[:100_000])
    return " ".join(unescape(text).split())[:limit]


def trusted_url(value, domains=None) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("invalid URL")
    parsed = urlsplit(value.strip())
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.port not in (None, 443)):
        raise ValueError("HTTPS URL required")
    if domains is not None and parsed.hostname.lower() not in domains:
        raise ValueError("untrusted item domain")
    return urlunsplit(("https", parsed.netloc, parsed.path, parsed.query, ""))


def fresh(value: datetime, now: datetime, max_age: timedelta) -> bool:
    return -timedelta(minutes=5) <= now - value <= max_age


def coverage(snapshot, name: str, status: str, detail: str):
    snapshot["coverage"][name] = {"status": status, "detail": detail}


def failed(snapshot, name: str, exc: Exception):
    # Exception text may contain a provider URL or credential. Never persist it.
    coverage(snapshot, name, "unavailable", f"Request/schema validation failed ({type(exc).__name__}); no inference made.")
    snapshot["errors"].append(f"{name}: {type(exc).__name__}")


def _coin(row, now):
    if not isinstance(row, dict):
        raise ValueError("invalid coin")
    coin_id = identifier(row.get("id"))
    rank = row.get("market_cap_rank")
    if isinstance(rank, bool) or not isinstance(rank, int) or rank <= 0:
        raise ValueError("invalid market cap rank")
    updated = timestamp(row.get("last_updated"))
    if not fresh(updated, now, timedelta(minutes=20)):
        raise ValueError("stale market price")
    symbol, name = clean(row.get("symbol"), 30), clean(row.get("name"), 100)
    if not symbol or not name:
        raise ValueError("coin name/symbol absent")
    change = row.get("price_change_percentage_24h")
    return {"id": coin_id, "symbol": symbol, "name": name,
            "market_cap_rank": rank,
            "current_price": number(row.get("current_price"), minimum=1e-15, maximum=1e15),
            "price_change_percentage_24h": None if change is None else number(change),
            "last_updated": iso(updated)}


def _markets(client, snapshot, now, headers):
    url = CG + "/coins/markets?" + urlencode({"vs_currency": "usd", "order": "market_cap_desc",
              "per_page": 50, "page": 1, "sparkline": "false", "price_change_percentage": "24h"})
    rows = client.get_json(url, headers=headers)
    if not isinstance(rows, list) or not 1 <= len(rows) <= 50:
        raise ValueError("expected bounded market list")
    coins, rejected = {}, 0
    for row in rows:
        try:
            coin = _coin(row, now)
            if coin["id"] in coins or coin["market_cap_rank"] > 50:
                raise ValueError("duplicate/out of universe")
            coins[coin["id"]] = coin
        except (ValueError, TypeError, KeyError, OverflowError):
            rejected += 1
    expected_ranks = {coin["market_cap_rank"] for coin in coins.values()}
    top_complete = expected_ranks == set(range(1, 51)) and len(coins) == 50
    pinned = [coin for coin in ("bitcoin", "ethereum") if coin not in coins]
    if pinned:
        extra = client.get_json(CG + "/coins/markets?" + urlencode({"vs_currency": "usd", "ids": ",".join(pinned),
                                "sparkline": "false"}), headers=headers)
        if not isinstance(extra, list) or len(extra) > 2:
            raise ValueError("invalid BTC/ETH response")
        for row in extra:
            try:
                coin = _coin(row, now)
                if coin["id"] not in pinned:
                    raise ValueError("unexpected pinned asset")
                coins[coin["id"]] = coin
            except (ValueError, TypeError, KeyError, OverflowError):
                rejected += 1
    snapshot["coins"] = sorted(coins.values(), key=lambda row: row["market_cap_rank"])
    complete = top_complete and {"bitcoin", "ethereum"} <= coins.keys()
    coverage(snapshot, "market_universe", "ok" if complete else "degraded",
             f"{len(coins)} fresh assets; dynamic market-cap ranks 1–50 plus BTC/ETH; {rejected} rejected rows. "
             + ("Complete rank set." if complete else "Missing/stale/duplicate ranks; universe is incomplete."))


def _exchanges(client, snapshot, headers):
    # Request a bounded large page then verify actual ranks, never trust API ordering.
    rows = client.get_json(CG + "/exchanges?per_page=250&page=1", headers=headers)
    if not isinstance(rows, list) or len(rows) > 250:
        raise ValueError("invalid exchange list")
    exchanges, ranks = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        rank = row.get("trust_score_rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= 10:
            continue
        try:
            exchange_id = identifier(row.get("id"))
            if exchange_id in exchanges or rank in ranks:
                raise ValueError("duplicate exchange rank")
            exchanges[exchange_id] = {"id": exchange_id, "name": clean(row.get("name"), 100),
                "trust_score_rank": rank,
                "trade_volume_24h_btc": number(row.get("trade_volume_24h_btc"), minimum=0),
                "url": trusted_url(row.get("url"))}
            ranks.add(rank)
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    snapshot["exchanges"] = sorted(exchanges.values(), key=lambda row: row["trust_score_rank"])
    complete = ranks == set(range(1, 11))
    coverage(snapshot, "exchange_universe", "ok" if complete else "degraded",
             f"{len(exchanges)}/10 CoinGecko trust-score ranked exchanges; refreshed this run. "
             "Provider has no per-row timestamp; rank is not a guarantee of safety.")


def _local_tag(element):
    return element.tag.rsplit("}", 1)[-1]


def _child(element, names):
    for child in element:
        if _local_tag(child) in names:
            return "".join(child.itertext()).strip()
    return ""


def _assets(text, coins, configured=None):
    result = set(configured or [])
    lower = text.lower()
    for coin in coins:
        name, symbol = coin["name"].lower(), coin["symbol"].lower()
        # Ambiguous symbols such as LINK/NEAR are only recognized with a $ prefix.
        if (len(name) >= 4 and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", lower)
                or re.search(r"\$" + re.escape(symbol) + r"(?!\w)", lower)
                or symbol in {"btc", "eth"} and re.search(r"(?<!\w)" + symbol + r"(?!\w)", lower)):
            result.add(coin["id"])
    return sorted(result)[:52]


def parse_feed(payload: bytes, feed: dict, coins: list, now: datetime):
    """Return observations + counts; reject DTDs and off-domain/missing-date items."""
    if not isinstance(payload, bytes) or len(payload) > MAX_FEED_BYTES:
        raise ValueError("oversize feed")
    # Decode before inspection: a byte-only DTD check is bypassed by UTF-16/32.
    # Reviewed feeds use UTF-8. Fail closed on unsupported XML encodings.
    try:
        xml = payload.decode("utf-8-sig")
    except UnicodeError:
        raise ValueError("UTF-8 feed required") from None
    upper = xml.upper()
    if "\x00" in xml or "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise ValueError("DTD/entity declarations forbidden")
    root = ET.fromstring(xml)
    if _local_tag(root) not in {"rss", "feed", "RDF"}:
        raise ValueError("RSS/Atom feed required")
    items = [element for element in root.iter() if _local_tag(element) in {"item", "entry"}]
    if len(items) > MAX_FEED_ITEMS:
        items = items[:MAX_FEED_ITEMS]
    observations, rejected, seen = [], 0, set()
    for item in items:
        try:
            link = _child(item, {"link"})
            for child in item:
                if _local_tag(child) == "link" and child.attrib.get("rel", "alternate") == "alternate":
                    link = child.attrib.get("href", link)
                    break
            link = trusted_url(link, feed["item_domains"])
            published = timestamp(_child(item, {"pubDate", "published", "date", "updated"}))
            if not fresh(published, now, MAX_NEWS_AGE):
                continue  # Old articles are normal on infrequently updated feeds.
            title = clean(_child(item, {"title"}), 250)
            if not title:
                raise ValueError("missing title")
            summary = clean(_child(item, {"description", "summary", "content", "encoded"}), 1800)
            key = sha256((feed["name"] + "\n" + link).encode()).hexdigest()[:32]
            if key in seen:
                continue
            seen.add(key)
            observations.append({"id": "news:" + key, "kind": "news", "source": feed["name"],
                "source_url": link, "published_at": iso(published), "title": title, "summary": summary,
                "assets": _assets(title + " " + summary, coins, feed.get("assets")),
                "primary": feed.get("primary") is True,
                "data": {"category": feed.get("category", "regulatory")}})
        except (ValueError, TypeError, KeyError, OverflowError):
            rejected += 1
    return observations, rejected


def _feed(client, snapshot, feed, now):
    name = feed["name"]
    headers = None
    if urlsplit(feed["url"]).hostname == "www.sec.gov":
        agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if agent:
            headers = {"User-Agent": agent}
    payload = client.get_bytes(feed["url"], headers=headers)
    observations, rejected = parse_feed(payload, feed, snapshot["coins"], now)
    snapshot["observations"].extend(observations)
    coverage(snapshot, name, "degraded" if rejected else "ok",
             f"{len(observations)} recent verifiable feed items; {rejected} invalid items rejected. Feed-only coverage.")


def _incidents(client, snapshot, exchange, setting, now):
    response = client.get_json(setting["url"])
    if not isinstance(response, dict) or not isinstance(response.get("incidents"), list):
        raise ValueError("missing incidents list")
    incidents, rejected = response["incidents"], 0
    if len(incidents) > 100:
        raise ValueError("oversize incident list")
    host = urlsplit(setting["url"]).hostname
    for item in incidents:
        try:
            incident_id = identifier(item.get("id"))
            status, impact = item.get("status"), item.get("impact")
            if status not in {"investigating", "identified", "monitoring", "resolved", "postmortem"}:
                raise ValueError("unknown status")
            if impact not in {"none", "minor", "major", "critical"}:
                raise ValueError("unknown impact")
            updated = timestamp(item.get("updated_at"))
            if not fresh(updated, now, MAX_NEWS_AGE):
                continue
            title = clean(item.get("name"), 250)
            if not title:
                raise ValueError("missing incident title")
            updates = item.get("incident_updates")
            if not isinstance(updates, list):
                raise ValueError("missing incident updates")
            summary = clean(updates[0].get("body", "")) if updates and isinstance(updates[0], dict) else ""
            # Do not trust shortened external URLs supplied in incident payloads.
            link = trusted_url(f"https://{host}/incidents/{incident_id}", setting["item_domains"])
            snapshot["observations"].append({"id": f"status:{exchange['id']}:{incident_id}",
                "kind": "exchange_incident", "source": setting["name"], "source_url": link,
                "published_at": iso(updated), "title": title, "summary": summary, "assets": [], "primary": True,
                "data": {"exchange_id": exchange["id"], "incident_id": incident_id, "status": status, "impact": impact}})
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            rejected += 1
    coverage(snapshot, setting["name"], "degraded" if rejected else "ok",
             f"Official incident feed retrieved for {exchange['name']}; {rejected} invalid items. Empty feed does not prove safety.")


def _depeg(client, snapshot, coin, setting, now):
    coin_id = coin["id"]
    if setting.get("peg") != 1.0:
        raise ValueError("only explicit USD 1 pegs supported")
    price_observed_at = timestamp(coin["last_updated"])
    if not fresh(price_observed_at, now, timedelta(minutes=10)):
        raise ValueError("aggregate quote too old for depeg confirmation")
    pair = setting.get("kraken_pair")
    if not pair:
        coverage(snapshot, f"depeg:{coin_id}", "unavailable", "No verified independent Kraken USD pair; strict depeg detection disabled.")
        return
    if not re.fullmatch(r"[A-Z0-9]{5,20}", pair):
        raise ValueError("invalid Kraken pair")
    url = KRAKEN + "?" + urlencode({"pair": pair, "count": 20})
    response = client.get_json(url)
    if not isinstance(response, dict) or response.get("error") != []:
        raise ValueError("Kraken error/missing response")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("invalid Kraken result")
    keys = [key for key in result if key != "last"]
    if len(keys) != 1 or keys[0] not in setting.get("response_pairs", [pair]):
        raise ValueError("unexpected Kraken pair")
    trades = result[keys[0]]
    if not isinstance(trades, list) or len(trades) > 1000:
        raise ValueError("invalid trades")
    prices = []
    for trade in trades[-20:]:
        if not isinstance(trade, list) or len(trade) < 3:
            continue
        try:
            observed = datetime.fromtimestamp(number(trade[2]), UTC)
            price = number(trade[0], minimum=1e-8, maximum=10)
            volume = number(trade[1], minimum=1e-12)
            if fresh(observed, now, timedelta(minutes=10)) and volume > 0:
                prices.append((price, observed))
        except (ValueError, TypeError, OverflowError):
            continue
    if len(prices) < 3:
        raise ValueError("fewer than 3 recent independent trades")
    independent_observed_at = min(when for _, when in prices)
    observed_at = min(price_observed_at, independent_observed_at)
    price = median(price for price, _ in prices)
    snapshot["observations"].append({"id": f"depeg:{coin_id}:{iso(observed_at)}", "kind": "depeg",
        "source": "CoinGecko + Kraken", "source_url": f"https://www.coingecko.com/en/coins/{coin_id}",
        "published_at": iso(observed_at), "title": f"{coin['name']} USD peg observations", "summary": "",
        "assets": [coin_id], "primary": False,
        "data": {"coin_id": coin_id, "price": coin["current_price"], "independent_price": price,
                 "peg": 1.0, "observed_at": iso(observed_at), "independent_source_url": url,
                 "price_observed_at": iso(price_observed_at),
                 "independent_observed_at": iso(independent_observed_at),
                 "independent_trade_count": len(prices)}})
    coverage(snapshot, f"depeg:{coin_id}", "ok", f"Fresh aggregate price plus median of {len(prices)} recent Kraken USD trades; observations only.")


def parse_liquidations(response, symbol, exchanges, now):
    if not isinstance(response, dict) or str(response.get("code")) != "0" or not isinstance(response.get("data"), list):
        raise ValueError("CoinGlass error/missing data")
    if not response["data"] or len(response["data"]) > 1000:
        raise ValueError("missing/oversize liquidation history")
    latest_end = now.replace(minute=0, second=0, microsecond=0)
    start = latest_end - timedelta(hours=1)
    matches = [row for row in response["data"] if isinstance(row, dict)
               and number(row.get("time")) == start.timestamp() * 1000]
    if len(matches) != 1:
        raise ValueError("last completed UTC hour absent/duplicated")
    row = matches[0]
    total = number(row.get("aggregated_long_liquidation_usd"), minimum=0, maximum=1e14)
    total += number(row.get("aggregated_short_liquidation_usd"), minimum=0, maximum=1e14)
    return {"id": f"liquidation:{symbol}:{iso(start)}", "kind": "liquidation", "source": "CoinGlass",
        "source_url": "https://www.coinglass.com/LiquidationData", "published_at": iso(latest_end),
        "title": f"{symbol} completed-hour liquidations", "summary": "Reported exchange liquidations; not global market coverage.",
        "assets": [{"BTC": "bitcoin", "ETH": "ethereum"}.get(symbol, symbol)], "primary": False,
        "data": {"symbol": symbol, "total_usd": total, "window_start": iso(start),
                 "window_end": iso(latest_end), "window_seconds": 3600, "exchange_list": exchanges}}


def _liquidations(client, snapshot, setting, now, key):
    symbols = setting.get("symbols", ["BTC", "ETH"])
    exchanges = setting.get("exchange_list", ["Binance", "OKX", "Bybit"])
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 10:
        raise ValueError("invalid liquidation symbols")
    if not isinstance(exchanges, list) or not 1 <= len(exchanges) <= 20:
        raise ValueError("invalid liquidation exchanges")
    for name in exchanges:
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9. _-]{1,40}", name):
            raise ValueError("invalid exchange name")
    for symbol in symbols:
        if not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{2,15}", symbol):
            raise ValueError("invalid liquidation symbol")
        name = "liquidations:" + symbol
        try:
            end = now.replace(minute=0, second=0, microsecond=0)
            url = GLASS + "/futures/liquidation/aggregated-history?" + urlencode({"exchange_list": ",".join(exchanges),
                "symbol": symbol, "interval": "1h", "limit": 3,
                "start_time": int((end - timedelta(hours=2)).timestamp() * 1000), "end_time": int(end.timestamp() * 1000)})
            response = client.get_json(url, headers={"CG-API-KEY": key})
            snapshot["observations"].append(parse_liquidations(response, symbol, exchanges, now))
            coverage(snapshot, name, "ok", "Last completed UTC hour, explicit venue set: " + ", ".join(exchanges))
        except Exception as exc:
            failed(snapshot, name, exc)


def _etf_flows(client, snapshot, setting, now, key):
    assets = setting.get("assets", ["bitcoin", "ethereum"])
    if (not isinstance(assets, list) or not 1 <= len(assets) <= 2
            or any(coin_id not in {"bitcoin", "ethereum"} for coin_id in assets)
            or len(set(assets)) != len(assets)):
        raise ValueError("unsupported/duplicate ETF assets")
    for coin_id in assets:
        name = "etf_flows:" + coin_id
        try:
            response = client.get_json(GLASS + f"/etf/{coin_id}/flow-history", headers={"CG-API-KEY": key})
            if not isinstance(response, dict) or str(response.get("code")) != "0" or not isinstance(response.get("data"), list):
                raise ValueError("CoinGlass ETF response invalid")
            rows = response["data"]
            if not rows or len(rows) > 10000:
                raise ValueError("ETF history absent/oversize")
            candidates = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                when = datetime.fromtimestamp(number(row.get("timestamp")) / 1000, UTC)
                if fresh(when, now, timedelta(days=7)) and when.date() < now.date():
                    candidates.append((when, row))
            if not candidates:
                raise ValueError("no recent completed-date ETF aggregate")
            when, row = max(candidates, key=lambda entry: entry[0])
            if sum(candidate.date() == when.date() for candidate, _ in candidates) != 1:
                raise ValueError("duplicate ETF date")
            flow = number(row.get("flow_usd"), minimum=-1e13, maximum=1e13)
            parts = row.get("etf_flows")
            if not isinstance(parts, list) or not 1 <= len(parts) <= 100:
                raise ValueError("ETF issuer breakdown absent/oversize")
            missing = 0
            for part in parts:
                try:
                    if not isinstance(part, dict) or not clean(part.get("etf_ticker"), 30):
                        raise ValueError("invalid issuer")
                    number(part.get("flow_usd"), minimum=-1e13, maximum=1e13)
                except (ValueError, TypeError, OverflowError):
                    missing += 1
            item = {"coin_id": coin_id, "date": when.date().isoformat(),
                "net_flow_usd": flow, "source": "CoinGlass", "source_url": f"https://www.coinglass.com/etf/{coin_id}",
                "final": False, "missing_issuer_values": missing,
                "note": "Provider daily aggregate, may be preliminary/revised; no finality flag. Not an intraday or whole-institutional-market measure."}
            snapshot.setdefault("etf_flows", []).append(item)
            # The engine consumes observations; retain the dated snapshot list
            # for inspection while exposing the same data to digest detection.
            snapshot["observations"].append({"id": f"etf:{coin_id}:{item['date']}", "kind": "etf_flow",
                "source": item["source"], "source_url": item["source_url"], "published_at": iso(when),
                "title": f"{coin_id} ETF daily net flow for {item['date']} (provisional)",
                "summary": item["note"], "assets": [coin_id], "primary": False, "data": dict(item)})
            coverage(snapshot, name, "degraded", f"Latest reported date {when.date()}; {missing} missing issuer values; provider does not attest finality. Digest only.")
        except Exception as exc:
            failed(snapshot, name, exc)


def collect(client, config: dict, now: datetime) -> dict:
    """Collect a bounded snapshot while isolating outages by provider."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone aware")
    now = now.astimezone(UTC)
    settings = config.get("sources", {})
    snapshot = {"as_of": iso(now), "coins": [], "exchanges": [], "observations": [],
                "coverage": {}, "errors": [], "etf_flows": []}
    key = os.environ.get("COINGECKO_DEMO_API_KEY", "").strip()
    headers = {"x-cg-demo-api-key": key} if key else None
    for name, call in (("market_universe", lambda: _markets(client, snapshot, now, headers)),
                       ("exchange_universe", lambda: _exchanges(client, snapshot, headers))):
        try:
            call()
        except Exception as exc:
            failed(snapshot, name, exc)
    feeds = settings.get("news_feeds", [])
    if not isinstance(feeds, list) or len(feeds) > 30:
        raise ValueError("at most 30 configured feeds")
    for feed in feeds:
        try:
            _feed(client, snapshot, feed, now)
        except Exception as exc:
            failed(snapshot, clean(feed.get("name"), 100) or "unnamed_feed", exc)
    status_config = settings.get("exchange_status", {})
    gaps = []
    for exchange in snapshot["exchanges"]:
        setting = status_config.get(exchange["id"])
        if not setting:
            gaps.append(exchange["name"])
            coverage(snapshot, f"exchange_status:{exchange['id']}", "unavailable", "No reviewed official incident adapter; announcements/outages not comprehensively monitored.")
            continue
        try:
            _incidents(client, snapshot, exchange, setting, now)
        except Exception as exc:
            failed(snapshot, setting["name"], exc)
    coverage(snapshot, "exchange_status_scope", "degraded", "Status feeds cover mapped current top-10 only; no universal announcement coverage. "
             + ("Unmapped: " + ", ".join(gaps) if gaps else "No unmapped exchanges in the retrieved set; upstream feed coverage remains partial."))
    stablecoins = settings.get("stablecoins", {})
    for coin in snapshot["coins"]:
        if coin["id"] in stablecoins:
            try:
                _depeg(client, snapshot, coin, stablecoins[coin["id"]], now)
            except Exception as exc:
                failed(snapshot, "depeg:" + coin["id"], exc)
    coverage(snapshot, "depeg_scope", "degraded", "Only explicitly mapped USD-pegged assets in the fresh universe with independent Kraken pairs; new/unmapped stablecoins and non-USD pegs are excluded.")
    coverage(snapshot, "project_news_scope", "degraded", "Dynamic top-50 market data does not imply complete project news/security coverage. Configured official RSS/Atom feeds plus secondary digest-only leads are checked; no article crawling or complete project coverage.")
    coverage(snapshot, "institutional_scope", "degraded", "Official feed announcements and optional BTC/ETH ETF daily aggregates only; no complete treasury, OTC, custody or on-chain institutional flow coverage.")
    glass_key = os.environ.get("COINGLASS_API_KEY", "").strip()
    liquidation_setting = settings.get("liquidations", {})
    etf_setting = settings.get("etf_flows", {})
    for name, setting, adapter in (("liquidations", liquidation_setting, _liquidations), ("etf_flows", etf_setting, _etf_flows)):
        if not setting.get("enabled", False) or not glass_key:
            coverage(snapshot, name, "unavailable", "Disabled or COINGLASS_API_KEY absent; no values inferred. Paid CoinGlass access required.")
            continue
        try:
            adapter(client, snapshot, setting, now, glass_key)
        except Exception as exc:
            failed(snapshot, name, exc)
    return snapshot
