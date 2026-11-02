"""Bounded, attributed messages. Source prose is data, never instructions."""
import re
from urllib.parse import urlsplit


def clean(value, limit=400):
    value = re.sub(r"<[^>]*>", " ", str(value or ""))
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = value.replace("@", "＠").replace("`", "'")
    value = re.sub(r"([*_~|>\\])", r"\\\1", value)
    value = " ".join(value.split())
    return value[:limit] + ("…" if len(value) > limit else "")


def safe_link(value):
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname and not url.username and not url.password
                and url.port in (None, 443) and not any(ord(c) < 32 for c in value)):
            return value.replace("<", "%3C").replace(">", "%3E")
    except (TypeError, ValueError):
        pass
    return ""


def source_lines(event):
    sources = []
    for source in event.get("sources", [])[:2]:
        url = safe_link(source.get("url", ""))
        if url:
            sources.append(f"{clean(source.get('name', 'Source'), 50)}: <{url}>")
    if not sources:
        raise ValueError("notification requires a verifiable HTTPS source")
    lines = "\n".join(sources)
    if len(lines) > 850:
        raise ValueError("source links exceed message safety bound; operator review required")
    return lines


def event_text(event):
    return "\n".join(filter(None, [
        f"**{clean(event.get('title'), 180)}**",
        f"Category: {clean(event.get('category'), 40)} | Assets: {clean(', '.join(event.get('assets', [])) or 'market-wide', 110)}",
        f"Potential impact: {clean(event.get('impact', 'Requires review; direction is uncertain.'), 180)}",
        clean(event.get("summary"), 300),
    ]))


def message(event, heading, timestamp, coverage_note=""):
    prefix = f"**{clean(heading, 110)}** · {clean(timestamp, 45)}\n"
    footer = f"\nSource time: {clean(event.get('published_at'), 40)}\n" + source_lines(event)
    if coverage_note:
        footer += "\nCoverage: " + clean(coverage_note, 180)
    # Truncate prose only. Source URLs remain complete and independently checkable.
    budget = 1890 - len(prefix) - len(footer)
    if budget < 550:
        raise ValueError("notification metadata exceeds message bound")
    body = event_text(event)
    if len(body) > budget:
        body = body[:budget - 1] + "…"
    return {"content": prefix + body + footer, "allowed_mentions": {"parse": []}, "flags": 4}
