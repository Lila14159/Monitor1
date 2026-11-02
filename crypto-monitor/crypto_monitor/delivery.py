"""The sole application-level external write: an explicitly enabled Discord POST."""
import json
import re
import urllib.error
import urllib.request
from .http import NoRedirect, public_host


class DeliveryError(RuntimeError):
    pass


def validate_webhook(webhook):
    if not re.fullmatch(r"https://discord\.com/api/webhooks/[0-9]{17,22}/[A-Za-z0-9_.-]{30,200}", webhook or ""):
        raise DeliveryError("DISCORD_WEBHOOK_URL must be a standard discord.com webhook URL")


def post(webhook, payload, *, opener=None, resolve=True):
    validate_webhook(webhook)
    if (not isinstance(payload, dict) or set(payload) != {"content", "allowed_mentions", "flags"}
            or not isinstance(payload["content"], str) or not 0 < len(payload["content"]) <= 1900
            or payload["allowed_mentions"] != {"parse": []} or payload["flags"] != 4):
        raise DeliveryError("Discord payload outside safety bounds")
    if resolve:
        public_host("discord.com")
    req = urllib.request.Request(webhook + "?wait=true", method="POST",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "crypto-monitor/1.0"})
    try:
        with (opener or urllib.request.build_opener(NoRedirect())).open(req, timeout=15) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise DeliveryError("Discord acknowledgement too large; delivery uncertain")
            result = json.loads(raw)
            if not isinstance(result, dict) or not re.fullmatch(r"[0-9]{17,22}", str(result.get("id", ""))):
                raise DeliveryError("Discord acknowledgement invalid; delivery uncertain")
            return str(result["id"])
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code == 429:
            raise DeliveryError("Discord rate limited; no automatic retry; inspect delivery ledger") from None
        raise DeliveryError(f"Discord HTTP {exc.code}; no automatic retry; inspect delivery ledger") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise DeliveryError("Discord delivery uncertain; no automatic retry; inspect delivery ledger") from None
