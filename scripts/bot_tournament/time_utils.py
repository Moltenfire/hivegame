# Time helpers for coordination freshness and log timestamps.
from __future__ import annotations

import datetime as dt
from typing import Any


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_timestamp(value: Any, fallback: dt.datetime) -> dt.datetime:
    if not isinstance(value, str):
        return fallback
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError:
        return fallback
