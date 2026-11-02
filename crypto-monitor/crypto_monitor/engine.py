"""Conservative, pure event classification and episode state.

Feed text is untrusted. Only configured official publishers can produce headline
alerts; financial headlines remain digest leads unless an explicit final action
meets a rule. This module performs no network access or delivery.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import math
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

UTC = timezone.utc
DEFAULTS = {
    "depeg_fraction": 0.01,
    "depeg_critical_fraction": 0.05,
    "depeg_recovery_fraction": 0.005,
    "confirmation_min_minutes": 30,
    "confirmation_max_minutes": 120,
    "security_material_usd": 10_000_000,
    "regulatory_material_usd": 100_000_000,
    "institutional_material_usd": 1_000_000_000,
    "etf_flow_btc_usd": 100_000_000,
    "etf_flow_eth_usd": 50_000_000,
    "liquidation_btc_usd": 100_000_000,
    "liquidation_eth_usd": 50_000_000,
    "liquidation_critical_multiplier": 3,
    "news_max_age_hours": 48,
    "news_alert_max_age_hours": 6,
    "snapshot_max_age_minutes": 20,
    "quote_max_age_minutes": 10,
    "state_retention_days": 30,
    "max_seen_items": 5000,
    "max_incidents": 500,
}
_UNCERTAIN = re.compile(
    r"\b(rumou?rs?|unconfirmed|alleged|allegedly|speculat\w*|possibly|possibly|"
    r"potential(?:ly)?|possible|suspected|likely|investigating|might|could|may|would|plans?|propos\w*|consider\w*|seeks?|"
    r"applications?|filings?|will|expects?|investigating reports|if|denies?|denied|"
    r"no evidence|not (?:been )?(?:hacked|compromised|exploited)|false reports?|"
    r"fake|simulation|exercise|no funds? (?:were )?(?:lost|stolen)|funds? (?:are|remain) safe|how to|explainer|anniversary|last year|last month|"
    r"previously|historical|revisits?|recap|year in review)\b", re.I,
)
_CRYPTO = re.compile(r"\b(crypto\w*|digital assets?|virtual assets?|bitcoin|ethereum|ether|btc|eth|stablecoins?|token\w*|blockchain|defi|mainnet|on.chain|validators?)\b", re.I)
_SECURITY = re.compile(r"\b(hack(?:ed)?|exploit(?:ed)?|security breach|compromis(?:ed|e)|unauthorized (?:access|transaction|withdrawal)s?|stolen|theft)\b", re.I)
_SERVICE_HALT = re.compile(r"\b(?:halt(?:ed|s)?|suspend(?:ed|s)?|paus(?:ed|es?))\b.{0,65}\b(?:withdrawals?|trading|network|block production)\b|\b(?:withdrawals?|trading|network|block production)\b.{0,65}\b(?:halted|suspended|paused)\b", re.I)
_MONEY = re.compile(r"(?:US\$|USD\s*|\$)\s*([\d,]+(?:\.\d+)?)\s*(billion|million|bn|[bm])?\b|([\d,]+(?:\.\d+)?)\s*(billion|million|bn|[bm])?\s*(?:US dollars|USD)\b", re.I)


def _time(value):
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else None
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(UTC) if result.tzinfo else None
    except (ValueError, OverflowError):
        return None


def _iso(value):
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _hash(*values):
    return hashlib.sha256("|".join(str(v) for v in values).encode()).hexdigest()[:32]


def canonical_url(value):
    """Remove tracking only, retaining query values that identify the article."""
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
            return None
        if parts.port not in (None, 443):
            return None
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in {"fbclid", "gclid", "ref", "source"}]
        return urlunsplit(("https", parts.hostname.lower(), parts.path.rstrip("/") or "/", urlencode(sorted(query)), ""))
    except ValueError:
        return None


def _domain(value, allowed):
    url = canonical_url(value)
    if not url:
        return False
    hostname = urlsplit(url).hostname
    # Exact configured hosts only; unlisted subdomains are not implicitly trusted.
    return hostname in {str(domain).lower() for domain in allowed}


def _fresh(value, now, minutes):
    timestamp = _time(value)
    return timestamp is not None and -120 <= (now - timestamp).total_seconds() <= minutes * 60


def _config(config):
    result = dict(DEFAULTS)
    supplied = config.get("thresholds", {})
    for key, default in result.items():
        value = _number(supplied.get(key, default))
        if value is not None and value > 0:
            result[key] = value
    # Configuration may tighten the rules but cannot remove their safety floors.
    for key in ("depeg_fraction", "depeg_critical_fraction", "security_material_usd",
                "regulatory_material_usd", "institutional_material_usd", "etf_flow_btc_usd", "etf_flow_eth_usd", "liquidation_btc_usd",
                "liquidation_eth_usd", "liquidation_critical_multiplier", "confirmation_min_minutes"):
        result[key] = max(DEFAULTS[key], result[key])
    for key in ("depeg_recovery_fraction", "news_max_age_hours", "news_alert_max_age_hours",
                "snapshot_max_age_minutes", "quote_max_age_minutes", "state_retention_days", "max_seen_items", "max_incidents"):
        result[key] = min(DEFAULTS[key], result[key])
    result["confirmation_min_minutes"] = min(90, result["confirmation_min_minutes"])
    result["confirmation_max_minutes"] = max(result["confirmation_min_minutes"] + 1, min(120, result["confirmation_max_minutes"]))
    result["depeg_critical_fraction"] = max(result["depeg_fraction"], result["depeg_critical_fraction"])
    return result


def _universe(snapshot, engine):
    coins = [c for c in snapshot.get("coins", []) if isinstance(c, dict) and
             isinstance(c.get("id"), str) and 0 < (_number(c.get("market_cap_rank")) or 9999) <= 50]
    exchanges = [e for e in snapshot.get("exchanges", []) if isinstance(e, dict) and
                 isinstance(e.get("id"), str) and 0 < (_number(e.get("trust_score_rank")) or 9999) <= 10]
    if coins:
        engine["coins"] = coins[:50]
    if exchanges:
        engine["exchanges"] = exchanges[:10]
    coins = engine.get("coins", [])
    exchanges = engine.get("exchanges", [])
    aliases = {"bitcoin", "btc", "ethereum", "ether", "eth"}
    ids = {"bitcoin", "btc", "ethereum", "eth"}
    for coin in coins:
        ids.add(coin["id"].lower())
        ids.add(str(coin.get("symbol", "")).lower())
        symbol = str(coin.get("symbol", "")).lower()
        if symbol:
            aliases.add("$" + symbol)
        for key in ("id", "name"):
            value = str(coin.get(key, "")).lower()
            if len(value) >= 3 and value not in {"link", "near", "one", "flow", "gas", "core", "world", "us", "on", "virtual", "pump"}:
                aliases.add(value)
    exchange_ids = set()
    for exchange in exchanges:
        exchange_ids.add(exchange["id"])
        aliases.add(exchange["id"].lower())
        aliases.add(str(exchange.get("name", "")).lower())
    return ids, aliases - {""}, exchange_ids


def _category(obs, feed=None):
    category = obs.get("data", {}).get("category") or (feed or {}).get("category")
    if category in {"regulatory", "etf", "institutional", "security"}:
        return category
    title = (str(obs.get("title", "")) + " " + str(obs.get("summary", "")))[:6000]
    if _SECURITY.search(title):
        return "security"
    if re.search(r"\b(etfs?|exchange.traded funds?)\b", title, re.I):
        return "etf"
    if re.search(r"\b(regulat\w*|sec|cftc|court|laws?|legislation)\b", title, re.I):
        return "regulatory"
    if re.search(r"\b(institution\w*|fund flows?|inflows?|outflows?|treasury|purchased|acquisition)\b", title, re.I):
        return "institutional"
    if _SERVICE_HALT.search(title) or re.search(r"\b(?:exchange outage|insolvency|bankruptcy|funds frozen|withdrawal delays?)\b", title, re.I):
        return "exchange"
    return "market"


def _relevant(obs, ids, aliases, category):
    if any(str(asset).lower() in ids for asset in obs.get("assets", [])):
        return True
    text = (str(obs.get("title", "")) + " " + str(obs.get("summary", "")))[:6000]
    if any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", text, re.I) for alias in aliases):
        return True
    return category in {"regulatory", "etf", "institutional"} and bool(_CRYPTO.search(text))


def _money(text):
    amounts = []
    for match in _MONEY.finditer(text):
        raw, unit = (match.group(1), match.group(2)) if match.group(1) else (match.group(3), match.group(4))
        amount = _number(raw.replace(",", ""))
        if amount is not None:
            factor = 1e9 if (unit or "").lower() in {"b", "bn", "billion"} else 1e6 if unit else 1
            amounts.append(amount * factor)
    return max(amounts, default=0)


def _strict_news(obs, feed, category, cfg, now):
    if not feed or not feed.get("primary") or not obs.get("primary"):
        return False
    if not _fresh(obs.get("published_at"), now, cfg["news_alert_max_age_hours"] * 60):
        return False
    title = str(obs.get("title", ""))[:1000]
    summary = str(obs.get("summary", ""))[:5000]
    text = title + " " + summary
    if "?" in title or _UNCERTAIN.search(text) or any(int(year) < now.year for year in re.findall(r"\b(20\d{2})\b", title)):
        return False
    # A headline amount must describe the reported action. A large AUM/market
    # cap figure elsewhere in a release is not a transaction or incident loss.
    amount = _money(title) if not re.search(r"\b(?:market cap|valuation|assets under management|total value locked|tvl)\b", title, re.I) else 0
    publisher_category = feed.get("category")
    if category == "regulatory" and publisher_category == "regulatory":
        final_rule = bool(re.search(r"\b(?:adopts?|adopted|enacts?|enacted|issues?|issued)\b.{0,80}\bfinal (?:rules?|regulations?)\b|\bfinal (?:rules?|regulations?)\b.{0,80}\b(?:adopted|enacted|issued|effective)\b", title, re.I))
        ban = bool(re.search(r"\b(?:bans?|banned|prohibits?|prohibited)\b.{0,60}\b(?:crypto\w*|digital assets?|virtual assets?|bitcoin|ethereum|stablecoins?)\b", title, re.I))
        enforcement = bool(re.search(r"\b(?:charges?|charged|settles?|settled|final judgment|orders?|ordered)\b", title, re.I)) and amount >= cfg["regulatory_material_usd"]
        etf_action = bool(re.search(r"\b(?:approves?|approved|rejects?|rejected|disapproves?|disapproved)\b", title, re.I) and re.search(r"\b(?:bitcoin|ethereum|ether|btc|eth)\b", text, re.I) and re.search(r"\b(?:etfs?|exchange.traded funds?)\b", text, re.I))
        return bool((final_rule or ban or enforcement or etf_action) and _CRYPTO.search(text))
    if category == "etf" and publisher_category == "regulatory":
        return bool(re.search(r"\b(?:approves?|approved|rejects?|rejected|disapproves?|disapproved)\b", title, re.I) and re.search(r"\b(?:bitcoin|ethereum|ether|btc|eth)\b", text, re.I) and re.search(r"\b(?:etfs?|exchange.traded funds?)\b", text, re.I))
    if category == "security" and publisher_category in {"security", "exchange", "protocol"}:
        loss_amount = max((_money(sentence) for sentence in re.split(r"[.!?;](?:\s|$)", title + ". " + summary)
                           if re.search(r"\b(?:stolen|lost|loss(?:es)?|drains?|drained|theft|exploited for)\b", sentence, re.I)
                           and not re.search(r"\b(?:market cap|valuation|total value locked|tvl|bounty|rewards?|funding)\b", sentence, re.I)), default=0)
        return bool(_SECURITY.search(title) and (loss_amount >= cfg["security_material_usd"] or _SERVICE_HALT.search(text)))
    if category == "institutional" and publisher_category in {"institutional", "etf"}:
        return bool(re.search(r"\b(?:buys|bought|purchases|purchased|acquires|acquired)\b", title, re.I) and
                    re.search(r"\b(?:buys|bought|purchases|purchased|acquires|acquired)\b[^.;!?]{0,70}(?:US\$|USD|\$)", title, re.I) and
                    re.search(r"\b(?:bitcoin|ethereum|ether|btc|eth)\b", title, re.I) and amount >= cfg["institutional_material_usd"])
    return False


def _event(obs, category, event_id, alert, severity="material", sources=None):
    return {
        "id": event_id, "category": category, "title": str(obs.get("title", ""))[:300],
        "summary": str(obs.get("summary", ""))[:1200], "assets": list(obs.get("assets", []))[:50],
        "impact": {
            "regulatory": "A confirmed policy or enforcement change may affect access, compliance costs, or market participation; direction and magnitude are uncertain.",
            "etf": "ETF developments may change investment access or reported demand; they do not establish future price direction.",
            "institutional": "Reported institutional activity may affect liquidity or demand; coverage is partial and causality is unproven.",
            "security": "A security incident may put affected assets or services at risk; broader market effects are uncertain.",
            "exchange": "An exchange incident may disrupt access, withdrawals, or trading on the named venue; broader contagion is unconfirmed.",
            "depeg": "A persistent peg deviation may affect redemption confidence and related liquidity; two-source prices do not establish issuer insolvency.",
            "liquidation": "Measured forced closures indicate derivatives stress on covered venues; subsequent price direction is uncertain.",
        }.get(category, "A source-reported market development; significance and price effects require further assessment."), "sources": sources or [{"name": str(obs.get("source", ""))[:100], "url": canonical_url(obs.get("source_url"))}],
        "published_at": obs.get("published_at"), "severity": severity, "alert": alert,
    }


def _news(obs, engine, config, cfg, now, ids, aliases):
    feeds = config.get("sources", {}).get("news_feeds", [])
    feed = next((f for f in feeds if f.get("name") == obs.get("source") and
                 _domain(obs.get("source_url"), f.get("item_domains", []))), None)
    # Even digest links must belong to an explicitly configured publisher.
    if not feed or not _fresh(obs.get("published_at"), now, cfg["news_max_age_hours"] * 60):
        return None
    category = _category(obs, feed)
    if not _relevant(obs, ids, aliases, category):
        return None
    if not feed.get("primary"):
        text = str(obs.get("title", "")) + " " + str(obs.get("summary", ""))
        # Secondary publishers provide leads in the user's priority categories,
        # never a stream of generic price predictions, promotions or tutorials.
        if category == "market" or re.search(r"\b(?:how to|explainer|opinion|sponsored|press release|price prediction|price forecast|giveaway|best coins? to buy|top coins? to buy)\b", text, re.I):
            return None
    title = re.sub(r"^(?:breaking|update|confirmed)\s*[:\-]\s*", "", str(obs.get("title", "")).lower())
    title = " ".join(re.findall(r"\w+", title))
    if len(title) < 10:
        return None
    url = canonical_url(obs["source_url"])
    keys = [_hash("url", url), _hash("title", title)]
    incident_id = obs.get("data", {}).get("incident_id")
    if incident_id:
        keys.append(_hash("news-incident", obs["source"], incident_id))
    alert = _strict_news(obs, feed, category, cfg, now)
    rank = 1 if alert else 0
    old = [engine["seen"][key] for key in keys if key in engine["seen"]]
    previous_rank = max((record.get("rank", 0) for record in old), default=-1)
    event_id = old[0].get("base_id", keys[0]) if old else keys[0]
    for key in keys:
        engine["seen"][key] = {"updated_at": _iso(now), "rank": max(rank, previous_rank), "base_id": event_id}
    if rank <= previous_rank:
        return None
    event = _event(obs, category, _hash(event_id, rank), alert, "major" if alert else "material")
    if previous_rank >= 0:
        event["supersedes"] = [_hash(event_id, previous_rank)]
    if not feed.get("primary"):
        event["summary"] = ("Secondary-source report; official confirmation not established by this monitor. " + event["summary"])[:1200]
    return event


def _incident(obs, engine, config, cfg, now, exchange_ids):
    data = obs.get("data", {})
    exchange_id = data.get("exchange_id")
    configured = config.get("sources", {}).get("exchange_status", {}).get(exchange_id, {})
    if exchange_id not in exchange_ids or configured.get("name") != obs.get("source"):
        return None
    if not _domain(obs.get("source_url"), configured.get("item_domains", [])) or not obs.get("primary"):
        return None
    if not _fresh(obs.get("published_at"), now, cfg["news_max_age_hours"] * 60):
        return None
    status, impact = data.get("status"), data.get("impact")
    if status not in {"investigating", "identified", "monitoring", "resolved", "postmortem"} or impact not in {"none", "minor", "major", "critical"}:
        return None
    if not data.get("incident_id"):
        return None
    key = _hash(exchange_id, data["incident_id"])
    old = engine["incidents"].get(key)
    record = {"status": status, "impact": impact, "updated_at": _iso(now), "episode": old.get("episode", 0) if old else 0}
    active = status in {"investigating", "identified"}
    previous_active = old and old["status"] in {"investigating", "identified"}
    if old and active and not previous_active:
        record["episode"] += 1
    engine["incidents"][key] = record
    if old and old["status"] == status and old["impact"] == impact:
        return None
    ranks = {"none": 0, "minor": 1, "major": 2, "critical": 3}
    alert = active and ranks[impact] >= 2 and (not previous_active or ranks[impact] > ranks[old["impact"]])
    alert = bool(alert and _fresh(obs.get("published_at"), now, cfg["news_alert_max_age_hours"] * 60))
    event = _event(obs, "exchange", _hash(key, status, impact, record["episode"]), alert, impact if impact in {"major", "critical"} else "material")
    event["summary"] = f"Official status: {status}; provider impact: {impact}. " + event["summary"][:1000]
    return event


def _confirmed(record, key, direction, tier, observed, cfg):
    """Advance confirmation using distinct source timestamps, never poll count alone."""
    pending = record.get(key)
    stamp = _iso(observed)
    if not pending or pending.get("direction") != direction or pending.get("tier") != tier:
        record[key] = {"direction": direction, "tier": tier, "first": stamp, "last": stamp}
        return False
    first, last = _time(pending.get("first")), _time(pending.get("last"))
    if first is None or last is None or observed <= last:
        return False
    interval = (observed - first).total_seconds() / 60
    if interval > cfg["confirmation_max_minutes"]:
        record[key] = {"direction": direction, "tier": tier, "first": stamp, "last": stamp}
        return False
    pending["last"] = stamp
    return interval >= cfg["confirmation_min_minutes"]


def _depeg(obs, engine, config, cfg, now, ids):
    data = obs.get("data", {})
    coin_id = data.get("coin_id")
    stablecoin = config.get("sources", {}).get("stablecoins", {}).get(coin_id)
    if not stablecoin or coin_id not in ids or stablecoin.get("peg") != 1 or not stablecoin.get("kraken_pair"):
        return None
    if obs.get("source") != "CoinGecko + Kraken" or not _domain(obs.get("source_url"), ["www.coingecko.com", "coingecko.com"]):
        return None
    if not _domain(data.get("independent_source_url"), ["api.kraken.com"]):
        return None
    if not all(_fresh(data.get(field), now, cfg["quote_max_age_minutes"])
               for field in ("observed_at", "price_observed_at", "independent_observed_at")):
        return None
    price, independent, peg = _number(data.get("price")), _number(data.get("independent_price")), _number(data.get("peg"))
    if price is None or independent is None or price <= 0 or independent <= 0 or peg != 1:
        return None
    observed = _time(data["observed_at"])
    if observed != min(_time(data["price_observed_at"]), _time(data["independent_observed_at"])):
        return None
    record = engine["depegs"].setdefault(coin_id, {})
    prior_observed = _time(record.get("last_observed"))
    if prior_observed and observed <= prior_observed:
        return None
    record["last_observed"] = _iso(observed)
    record["updated_at"] = _iso(now)
    a, b = price - peg, independent - peg
    recovered = max(abs(a), abs(b)) <= cfg["depeg_recovery_fraction"]
    active = record.get("active")
    if recovered:
        record.pop("pending", None)
        if active and _confirmed(record, "recovery", 0, 0, observed, cfg):
            record.pop("active", None)
            record.pop("recovery", None)
            event = _event(obs, "depeg", _hash(coin_id, "recovered", _iso(observed)), False)
            event["title"] = f"{coin_id}: peg recovery confirmed by two price sources"
            event["summary"] = f"Both observed prices are within {cfg['depeg_recovery_fraction']:.1%} of USD 1 across two distinct observations."
            return event
        return None
    record.pop("recovery", None)
    same_direction = a * b > 0
    deviation = min(abs(a), abs(b))
    if not same_direction or deviation + 1e-12 < cfg["depeg_fraction"]:
        record.pop("pending", None)
        return None
    direction = -1 if a < 0 else 1
    tier = 2 if deviation + 1e-12 >= cfg["depeg_critical_fraction"] else 1
    if active and active["direction"] == direction and tier <= active["tier"]:
        record.pop("pending", None)
        return None
    if not _confirmed(record, "pending", direction, tier, observed, cfg):
        return None
    record["active"] = {"direction": direction, "tier": tier, "started_at": _iso(observed)}
    record.pop("pending", None)
    sources = [{"name": "CoinGecko", "url": canonical_url(obs["source_url"])},
               {"name": "Kraken", "url": canonical_url(data["independent_source_url"])}]
    event = _event(obs, "depeg", _hash(coin_id, direction, tier, _iso(observed)), True, "critical" if tier == 2 else "major", sources)
    event["title"] = f"{coin_id}: persistent USD peg deviation confirmed"
    event["summary"] = f"CoinGecko USD {price:.6f}; Kraken USD {independent:.6f}. Both are {deviation:.2%} or more {'below' if direction < 0 else 'above'} USD 1 in two distinct observations at least {cfg['confirmation_min_minutes']:g} minutes apart."
    return event


def _liquidation(obs, engine, cfg, now):
    data = obs.get("data", {})
    symbol = str(data.get("symbol", "")).upper()
    if symbol not in {"BTC", "ETH"} or obs.get("source") != "CoinGlass" or not _domain(obs.get("source_url"), ["www.coinglass.com", "coinglass.com"]):
        return None
    start, end = _time(data.get("window_start")), _time(data.get("window_end"))
    amount = _number(data.get("total_usd"))
    # The API reports completed UTC buckets. Their end can legitimately be 59
    # minutes old; use the last completed hour rather than snapshot age, which
    # would incorrectly reject the normal :17 schedule after a short delay.
    if (start is None or end is None or data.get("window_seconds") != 3600 or
            (end - start).total_seconds() != 3600 or amount is None or not 0 <= amount <= 2e14 or
            end != now.replace(minute=0, second=0, microsecond=0)):
        return None
    record = engine["liquidations"].setdefault(symbol, {})
    previous = _time(record.get("last_window_end"))
    if previous and end <= previous:
        return None
    record["last_window_end"] = _iso(end)
    record["updated_at"] = _iso(now)
    threshold = cfg["liquidation_btc_usd" if symbol == "BTC" else "liquidation_eth_usd"]
    tier = 2 if amount >= threshold * cfg["liquidation_critical_multiplier"] else 1 if amount >= threshold else 0
    active_tier = record.get("active_tier", 0)
    if amount <= threshold * 0.5:
        if active_tier and _confirmed(record, "recovery", 0, 0, end, cfg):
            record["active_tier"] = 0
            record.pop("recovery", None)
            event = _event(obs, "liquidation", _hash(symbol, "recovered", _iso(end)), False)
            event["title"] = f"{symbol}: liquidation episode subsided"
            event["summary"] = "Measured one-hour liquidation totals stayed below half the alert threshold across two distinct observations."
            return event
        return None
    record.pop("recovery", None)
    if tier <= active_tier:
        return None
    record["active_tier"] = tier
    event = _event(obs, "liquidation", _hash(symbol, tier, _iso(end)), True, "critical" if tier == 2 else "major")
    event["title"] = f"{symbol}: USD {amount:,.0f} measured liquidations over one hour"
    event["summary"] = f"CoinGlass measured USD {amount:,.0f} in liquidations for {_iso(start)}–{_iso(end)}. Coverage is limited to exchanges included by the provider."
    return event



def _etf_flow(obs, engine, config, cfg, now):
    data = obs.get("data", {})
    coin_id = data.get("coin_id")
    if not config.get("sources", {}).get("etf_flows", {}).get("enabled", False):
        return None
    if coin_id not in {"bitcoin", "ethereum"} or obs.get("source") != "CoinGlass":
        return None
    if not _domain(obs.get("source_url"), ["www.coinglass.com", "coinglass.com"]) or data.get("final") is not False:
        return None
    date = _time(str(data.get("date", "")) + "T00:00:00Z")
    flow = _number(data.get("net_flow_usd"))
    missing = data.get("missing_issuer_values")
    if date is None or date.date() >= now.date() or now - date > timedelta(days=7) or flow is None or abs(flow) > 1e13:
        return None
    if not isinstance(missing, int) or isinstance(missing, bool) or not 0 <= missing <= 100:
        return None
    key = _hash("etf-flow", coin_id, data["date"])
    old = engine["seen"].get(key)
    threshold = cfg["etf_flow_btc_usd" if coin_id == "bitcoin" else "etf_flow_eth_usd"]
    previous_flow = _number(old.get("emitted_flow")) if old else None
    # Compare with the last material report, not the last poll: a sequence of
    # small provider revisions can eventually become a meaningful change.
    material = abs(flow) >= threshold if previous_flow is None else abs(flow - previous_flow) >= threshold
    revision_number = (old.get("revision", 0) if old else 0) + int(material)
    engine["seen"][key] = {"updated_at": _iso(now), "emitted_flow": flow if material else previous_flow,
                           "fingerprint": _hash(flow, missing), "revision": revision_number, "rank": 0}
    if not material:
        return None
    fingerprint = _hash(flow)
    event = _event(obs, "etf", _hash(key, fingerprint, revision_number), False)
    label = "revised" if previous_flow is not None else "reported"
    event["title"] = f"{coin_id}: {label} ETF daily net flow for {data['date']} (provisional)"
    event["published_at"] = _iso(date)
    revision = f" Changed by USD {flow - previous_flow:,.0f} since the last material report." if previous_flow is not None else ""
    event["summary"] = f"CoinGlass reports USD {flow:,.0f} net flow for {data['date']}; {missing} missing issuer values.{revision} The provider has no finality flag and may revise this daily aggregate. This is not an intraday or whole-institutional-market measure."
    return event

def _prune(engine, cfg, now):
    cutoff = now - timedelta(days=cfg["state_retention_days"])
    for section, limit in (("seen", int(cfg["max_seen_items"])), ("incidents", int(cfg["max_incidents"]))):
        records = engine[section]
        recent = [(key, value) for key, value in records.items()
                  if (_time(value.get("updated_at")) or datetime.min.replace(tzinfo=UTC)) >= cutoff]
        engine[section] = dict(sorted(recent, key=lambda pair: pair[1]["updated_at"], reverse=True)[:limit])
    # Numerical active episodes deliberately survive outages and the news TTL.
    for section in ("depegs", "liquidations"):
        records = engine[section]
        engine[section] = dict(sorted(records.items(), key=lambda pair: pair[1].get("updated_at", ""), reverse=True)[:100])


def evaluate(snapshot, state, config, now):
    """Return (new_state, new_events), without mutating arguments or marking delivery.

    A first valid snapshot seeds all observations quietly. Missing/stale snapshots
    never initialize the monitor or clear active episodes. Source outages preserve
    the previous universe, incident statuses, and numerical alert episodes.
    """
    now = _time(now)
    if now is None:
        raise ValueError("now must be timezone-aware")
    result = deepcopy(state)
    cfg = _config(config)
    if not _fresh(snapshot.get("as_of"), now, cfg["snapshot_max_age_minutes"]):
        return result, []
    bootstrap = not result.get("engine", {}).get("initialized")
    engine = result.setdefault("engine", {})
    for section in ("seen", "incidents", "depegs", "liquidations"):
        engine.setdefault(section, {})
    ids, aliases, exchange_ids = _universe(snapshot, engine)
    events = []
    observations = snapshot.get("observations", [])
    if not isinstance(observations, list):
        observations = []
    # Bound untrusted input work even if an adapter accidentally returns too much.
    # Process configured primary feeds first so one snapshot containing a copied
    # headline and its official source produces a single best-evidence event.
    primary_names = {feed.get("name") for feed in config.get("sources", {}).get("news_feeds", []) if feed.get("primary")}
    observations = sorted(observations[:2000], key=lambda obs: 0 if isinstance(obs, dict) and obs.get("source") in primary_names else 1)
    for obs in observations:
        if not isinstance(obs, dict) or not isinstance(obs.get("data", {}), dict):
            continue
        kind = obs.get("kind")
        if kind == "news":
            event = _news(obs, engine, config, cfg, now, ids, aliases)
        elif kind == "exchange_incident":
            event = _incident(obs, engine, config, cfg, now, exchange_ids)
        elif kind == "depeg":
            event = _depeg(obs, engine, config, cfg, now, ids)
        elif kind == "liquidation":
            event = _liquidation(obs, engine, cfg, now)
        elif kind == "etf_flow":
            event = _etf_flow(obs, engine, config, cfg, now)
        else:
            event = None
        if event is not None:
            events.append(event)
    # An entirely failed initial fetch must not convert the next real fetch into
    # "new" news. Partial useful snapshots may establish a quiet baseline.
    if observations or snapshot.get("coins") or snapshot.get("exchanges"):
        engine["initialized"] = True
    _prune(engine, cfg, now)
    if bootstrap:
        events = []
    return result, events
