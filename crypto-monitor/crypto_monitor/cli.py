"""Collect → evaluate → checkpoint intent → send once → checkpoint acknowledgement."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .delivery import DeliveryError, post, validate_webhook
from .formatting import message
from .http import Client, FetchError
from .scheduler import digest_slot

UTC = timezone.utc


def iso(now):
    return now.astimezone(UTC).isoformat()


def parse_time(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp must have timezone")
    return result.astimezone(UTC)


def encoded(data):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def write_json(path, data):
    path = Path(path)
    body = encoded(data)
    if len(body) > 8_000_000:
        raise ValueError("state/report size bound exceeded")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_bytes(body)
    temp.replace(path)


def load_json(path):
    raw = Path(path).read_bytes()
    if len(raw) > 8_000_000:
        raise ValueError("JSON file size bound exceeded")
    value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def check_state(state, initialize=False):
    if type(state.get("version")) is not int or state["version"] != 1:
        raise ValueError("state schema mismatch; do not reset automatically")
    if not state.get("initialized") and not initialize:
        raise ValueError("state not initialized; use explicit initialization")
    if state.get("initialized") and initialize:
        raise ValueError("refusing to reinitialize existing state")
    for key in ("engine", "outbox", "pending_events", "digest_slots", "digest_baseline"):
        if key in state and not isinstance(state[key], dict):
            raise ValueError("state structure invalid")
    if state.get("initialized") and state.get("engine", {}).get("initialized") is not True:
        raise ValueError("initialized state has lost detection history")
    for section in ("seen", "incidents", "depegs", "liquidations"):
        if section in state.get("engine", {}) and not isinstance(state["engine"][section], dict):
            raise ValueError("invalid detection history")
    for item in state.get("pending_events", {}).values():
        if not isinstance(item, dict) or not isinstance(item.get("event"), dict):
            raise ValueError("invalid queued event")
        parse_time(item["detected_at"])
    for value in state.get("digest_slots", {}).values():
        parse_time(value)
    for record in state.get("outbox", {}).values():
        if not isinstance(record, dict) or record.get("status") not in {"pending", "attempted", "sent", "uncertain"}:
            raise ValueError("delivery ledger invalid")
        parse_time(record["created_at"])
        if (not isinstance(record.get("payload"), dict) or not isinstance(record.get("event_ids"), list)
                or any(not isinstance(value, str) for value in record["event_ids"])):
            raise ValueError("invalid delivery record")


def hosts(config):
    result = {"api.coingecko.com", "api.kraken.com", "open-api-v4.coinglass.com"}
    # Config is reviewed repository code; never derive destinations from feed contents.
    def visit(value):
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, str) and value.startswith("https://"):
            host = urlsplit(value).hostname
            if host:
                result.add(host)
    visit(config.get("sources", {}))
    return result


def market_baseline(snapshot):
    result = {}
    now = parse_time(snapshot["as_of"])
    for coin in snapshot.get("coins", []):
        price = coin.get("current_price")
        try:
            age = (now - parse_time(coin["last_updated"])).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(price, (int, float)) and math.isfinite(price) and price > 0 and -60 <= age <= 1200:
            result[coin["id"]] = {"price": price, "symbol": coin["symbol"], "rank": coin.get("market_cap_rank")}
    return result


def market_event(snapshot, baseline, slot, config):
    current = market_baseline(snapshot)
    movements = []
    for key, coin in current.items():
        prior = baseline.get(key, {}).get("price")
        if not isinstance(prior, (int, float)) or prior <= 0:
            continue
        change = (coin["price"] / prior - 1) * 100
        threshold = config.get("digest", {}).get("major_move_pct", 5) if key in {"bitcoin", "ethereum"} else config.get("digest", {}).get("other_move_pct", 10)
        if abs(change) >= threshold:
            movements.append((abs(change), f"{coin['symbol'].upper()} {change:+.1f}% since last digest baseline"))
    if not movements:
        return None
    detail = "; ".join(text for _, text in sorted(movements, reverse=True)[:4])
    context = "; ".join(f"{current[key]['symbol'].upper()} ${current[key]['price']:,.2f}" for key in ("bitcoin", "ethereum") if key in current)
    return {"id": "market:" + slot, "category": "market", "title": "Material market-price changes",
            "summary": detail + ". " + context, "assets": ["BTC", "ETH", "dynamic top 50"],
            "impact": "Volatility may affect liquidity. Price moves alone do not establish a major event or liquidation.",
            "sources": [{"name": "CoinGecko", "url": "https://www.coingecko.com/"}],
            "published_at": snapshot["as_of"], "severity": "material", "alert": False}


def prepare(snapshot, state, config, now, *, initialize=False):
    from .engine import evaluate
    check_state(state, initialize)
    updated, events = evaluate(snapshot, copy.deepcopy(state), config, now)
    updated["version"] = 1
    updated["initialized"] = True
    updated["updated_at"] = iso(now)
    outbox = updated.setdefault("outbox", {})
    pending = updated.setdefault("pending_events", {})
    slots = updated.setdefault("digest_slots", {})
    baseline = updated.setdefault("digest_baseline", {})
    fresh_prices = market_baseline(snapshot)
    for coin_id, quote in fresh_prices.items():
        baseline.setdefault(coin_id, quote)
    if len(fresh_prices) >= 50:
        for coin_id in list(baseline):
            if coin_id not in fresh_prices:
                del baseline[coin_id]
    cutoff = now - timedelta(days=30)
    for key in list(outbox):
        if parse_time(outbox[key]["created_at"]) < cutoff:
            del outbox[key]
    for key in list(slots):
        if parse_time(slots[key]) < cutoff:
            del slots[key]
    expired = 0
    for key in list(pending):
        if parse_time(pending[key]["detected_at"]) < now - timedelta(hours=72):
            expired += 1
            del pending[key]
    if initialize:
        updated["digest_baseline"] = market_baseline(snapshot)
        return updated, [], {"bootstrap": True, "new_events": 0, "planned_messages": 0}
    # A later status/recovery at the same source supersedes an unsent old alert.
    superseded = 0
    for event in events:
        for key in event.get("supersedes", []):
            if key in pending:
                del pending[key]
                superseded += 1
        urls = {source.get("url") for source in event.get("sources", []) if source.get("url")}
        for key in list(pending):
            old = pending[key]["event"]
            same_subject = (old.get("category") == event.get("category") and
                            set(old.get("assets", [])) == set(event.get("assets", [])) and
                            urls.intersection(source.get("url") for source in old.get("sources", [])))
            if same_subject and old["id"] != event["id"] and parse_time(old["published_at"]) < parse_time(event["published_at"]):
                del pending[key]
                superseded += 1
        pending.setdefault(event["id"], {"event": event, "detected_at": iso(now)})
    if len(pending) > 1000:
        raise ValueError("event queue bound exceeded; operator review required")
    # An event assigned to ANY durable intent is never automatically sent a second time.
    assigned = {eid for record in outbox.values() for eid in record.get("event_ids", [])}
    for key in list(pending):
        if key in assigned:
            del pending[key]
    schedule = config.get("schedule", {})
    zone = schedule.get("timezone", "Asia/Taipei")
    local_date = now.astimezone(ZoneInfo(zone)).date()
    daily_count = sum(parse_time(r["created_at"]).astimezone(ZoneInfo(zone)).date() == local_date for r in outbox.values())
    safety = config.get("safety", {})
    allowance = max(0, min(safety.get("max_messages_per_run", 5), 5,
                           min(safety.get("max_messages_per_day", 20), 20) - daily_count))
    degraded = [name for name, row in snapshot.get("coverage", {}).items() if row.get("status") != "ok"]
    coverage_note = "gaps: " + ", ".join(degraded[:6]) if degraded else "configured sources checked; coverage is not exhaustive"
    timestamp = now.astimezone(ZoneInfo(zone)).strftime("%Y-%m-%d %H:%M %Z")
    planned = []

    def enqueue(event, heading):
        mid = hashlib.sha256((event["id"] + "|" + heading).encode()).hexdigest()[:32]
        if mid in outbox or len(planned) >= allowance:
            return False
        outbox[mid] = {"created_at": iso(now), "status": "pending", "event_ids": [event["id"]],
                       "payload": message(event, heading, timestamp, coverage_note)}
        planned.append(mid)
        pending.pop(event["id"], None)
        return True

    # Expired queued major observations become dated digest reports, not stale alerts.
    for item in pending.values():
        event = item["event"]
        if event.get("alert") and (now - parse_time(event["published_at"])).total_seconds() > 6 * 3600:
            event["alert"] = False
            event["summary"] = "Delayed report (alert window expired). " + event.get("summary", "")
    # Major alerts take precedence over digest items, with retained overflow.
    for item in sorted(list(pending.values()), key=lambda item: (item["event"].get("severity") != "critical", item["detected_at"])):
        if item["event"].get("alert") and parse_time(item["detected_at"]) >= now - timedelta(hours=24):
            enqueue(item["event"], "Strict major-event alert")
    slot = digest_slot(now, slots, zone, schedule.get("digest_hours", [9, 21]), schedule.get("grace_hours", 3))
    if slot:
        market = market_event(snapshot, baseline, slot, config)
        digest_candidates = [item["event"] for item in pending.values() if not item["event"].get("alert")]
        if market:
            digest_candidates.append(market)
        digest_planned = False
        for event in digest_candidates:
            if enqueue(event, "Market digest · " + slot):
                digest_planned = True
        if digest_planned or not digest_candidates:
            # One bounded digest batch per slot; overflow waits for the next slot.
            slots[slot] = iso(now)  # Empty digest is deliberately silent.
        if market and any(market["id"] in outbox[mid]["event_ids"] for mid in planned):
            updated["digest_baseline"] = market_baseline(snapshot)
        elif not baseline:
            updated["digest_baseline"] = market_baseline(snapshot)
    return updated, planned, {"bootstrap": False, "new_events": len(events), "planned_messages": len(planned),
                               "queued_events": len(pending), "digest_slot": slot,
                               "daily_messages_reserved": daily_count + len(planned),
                               "expired_queue_events": expired, "superseded_queue_events": superseded}


def deliver(state_path, plan_path, *, send=False, webhook=None, poster=post, now=None):
    if not send:
        raise ValueError("delivery requires explicit --send")
    plan = load_json(plan_path)
    if plan.get("dry_run"):
        raise ValueError("cannot deliver a dry-run plan")
    raw = Path(state_path).read_bytes()
    if hashlib.sha256(raw).hexdigest() != plan.get("state_sha256"):
        raise ValueError("state changed since plan; refusing replay")
    now = now or datetime.now(UTC)
    age = (now - parse_time(plan["created_at"])).total_seconds()
    if not -60 <= age <= 1800:
        raise ValueError("delivery plan expired")
    state = load_json(state_path)
    ids = plan.get("message_ids", [])
    if not isinstance(ids, list) or len(ids) > 5 or len(set(ids)) != len(ids):
        raise ValueError("invalid delivery plan")
    if not ids:
        return 0
    validate_webhook(webhook)
    for mid in ids:
        if mid not in state["outbox"] or state["outbox"][mid]["status"] != "pending":
            raise ValueError("delivery intent missing or already attempted")
    for mid in ids:
        record = state["outbox"][mid]
        record["status"] = "attempted"
        write_json(state_path, state)
        try:
            record["discord_message_id"] = poster(webhook, record["payload"])
            record["status"] = "sent"
        except (DeliveryError, FetchError):
            record["status"] = "uncertain"
            write_json(state_path, state)
            raise
        write_json(state_path, state)
    return len(ids)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--config", default="config.json")
    p.add_argument("--state", default="state/state.json")
    p.add_argument("--plan", default="work/plan.json")
    p.add_argument("--report", default="work/report.json")
    p.add_argument("--initialize", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--fixture", help="offline snapshot JSON for tests/review; only accepted in dry-run")
    d = sub.add_parser("deliver")
    d.add_argument("--state", default="state/state.json")
    d.add_argument("--plan", default="work/plan.json")
    d.add_argument("--send", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "deliver":
            count = deliver(args.state, args.plan, send=args.send, webhook=os.environ.get("DISCORD_WEBHOOK_URL"))
            print(f"Discord acknowledgements: {count}")
            return 0
        config = load_json(args.config)
        if args.fixture and not args.dry_run:
            raise ValueError("fixtures are only permitted with --dry-run")
        if Path(args.state).exists():
            state = load_json(args.state)
        elif args.initialize or args.dry_run:
            state = {"version": 1}
        else:
            raise ValueError("state missing; initialize explicitly, never reset automatically")
        initialize = args.initialize or (args.dry_run and not state.get("initialized"))
        check_state(state, initialize)
        now = datetime.now(UTC)
        if args.fixture:
            snapshot = load_json(args.fixture)
            now = parse_time(snapshot["as_of"])
        else:
            from .sources import collect
            snapshot = collect(Client(hosts(config)), config, now)
        # Always leave coverage diagnostics, even when initialization must fail closed.
        write_json(args.report, {"as_of": snapshot["as_of"], "dry_run": args.dry_run,
                                "coverage": snapshot.get("coverage", {}), "errors": snapshot.get("errors", []),
                                "coins": len(snapshot.get("coins", [])), "exchanges": len(snapshot.get("exchanges", [])),
                                "planned_messages": 0, "preview_messages": []})
        ranks = {row.get("market_cap_rank") for row in snapshot.get("coins", [])}
        exchange_ranks = {row.get("trust_score_rank") for row in snapshot.get("exchanges", [])}
        coin_ids = {row.get("id") for row in snapshot.get("coins", [])}
        if initialize and (not set(range(1, 51)) <= ranks or exchange_ranks != set(range(1, 11))
                           or not {"bitcoin", "ethereum"} <= coin_ids):
            raise ValueError("cannot initialize without complete top50/BTC/ETH and top10 inventories")
        updated, ids, summary = prepare(snapshot, state, config, now, initialize=initialize)
        summary["coverage"] = snapshot.get("coverage", {})
        summary["errors"] = snapshot.get("errors", [])
        summary["as_of"] = snapshot["as_of"]
        summary["unresolved_deliveries"] = sum(row["status"] != "sent" for row in updated.get("outbox", {}).values())
        summary["uncertain_deliveries"] = sum(row["status"] == "uncertain" for row in updated.get("outbox", {}).values())
        summary["dry_run"] = args.dry_run
        summary["coins"] = len(snapshot.get("coins", []))
        summary["exchanges"] = len(snapshot.get("exchanges", []))
        summary["preview_messages"] = [updated["outbox"][mid]["payload"] for mid in ids]
        # Reviewed local inventory and coverage report; never contains secrets.
        summary["universe"] = {"coins": snapshot.get("coins", []), "exchanges": snapshot.get("exchanges", [])}
        write_json(args.report, summary)
        plan = {"created_at": iso(now), "dry_run": args.dry_run, "message_ids": ids,
                "state_sha256": hashlib.sha256(encoded(updated)).hexdigest()}
        write_json(args.plan, plan)
        if not args.dry_run:
            write_json(args.state, updated)
        print(json.dumps({key: summary[key] for key in ("as_of", "dry_run", "new_events", "planned_messages", "coins", "exchanges")}, sort_keys=True))
        gaps = sum(row.get("status") != "ok" for row in summary["coverage"].values())
        print(f"Coverage gaps/degraded providers: {gaps}; inspect {args.report}")
        # No outbound health notifications: operator can see run failure when core data is absent.
        return 2 if not snapshot.get("coins") or not snapshot.get("exchanges") else 0
    except (OSError, ValueError, TypeError, KeyError, FetchError, DeliveryError):
        # Avoid leaking environment secrets or provider-supplied text through exception repr.
        print("Monitor stopped safely. Check configuration, state integrity, provider coverage, or delivery ledger. No automatic reset/retry.", file=sys.stderr)
        return 1
