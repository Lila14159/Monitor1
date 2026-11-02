"""Use civil-time slots, never host-local time or a manually adjusted UTC offset."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def digest_slot(now: datetime, completed: dict, timezone_name="Asia/Taipei", hours=(9, 21),
                grace_hours=3):
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    zone = ZoneInfo(timezone_name)
    local = now.astimezone(zone)
    candidates = []
    for date in (local.date() - timedelta(days=1), local.date()):
        for hour in hours:
            wall = datetime(date.year, date.month, date.day, hour, tzinfo=zone)
            # Round trip maps nonexistent spring-forward time to next valid local time.
            target = wall.astimezone(timezone.utc).astimezone(zone)
            slot = f"{date.isoformat()}T{hour:02d}:00@{timezone_name}"
            age = (now.astimezone(timezone.utc) - target.astimezone(timezone.utc)).total_seconds()
            if 0 <= age <= grace_hours * 3600 and slot not in completed:
                candidates.append((target.timestamp(), slot))
    return max(candidates)[1] if candidates else None
