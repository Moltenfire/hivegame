# Encodes, parses, and filters tournament chat coordination protocol messages.
from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .time_utils import parse_timestamp


COORD_PREFIX = "[bot-coord] "


def coord_json(message: dict[str, Any], now: dt.datetime) -> dict[str, Any] | None:
    chat_message = message.get("message")
    if not isinstance(chat_message, dict):
        return None
    text = chat_message.get("message")
    if not isinstance(text, str) or not text.startswith(COORD_PREFIX):
        return None
    try:
        payload = json.loads(text[len(COORD_PREFIX) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    username = chat_message.get("username")
    bot = payload.get("bot")
    if not isinstance(username, str) or not isinstance(bot, str) or username != bot:
        return None

    payload["_timestamp"] = parse_timestamp(chat_message.get("timestamp"), now)
    return payload


def coord_messages(
    chat_messages: list[dict[str, Any]], tournament_id: str, now: dt.datetime
) -> list[dict[str, Any]]:
    parsed = []
    for message in chat_messages:
        payload = coord_json(message, now)
        if (
            payload
            and payload.get("v") == 1
            and payload.get("tournament_id") == tournament_id
        ):
            parsed.append(payload)
    return parsed


def recent_messages(
    messages: list[dict[str, Any]], message_type: str, max_age: float, now: dt.datetime
) -> list[dict[str, Any]]:
    fresh = []
    for message in messages:
        timestamp = message.get("_timestamp")
        if (
            message.get("type") == message_type
            and isinstance(timestamp, dt.datetime)
            and (now - timestamp).total_seconds() <= max_age
        ):
            fresh.append(message)
    return fresh


def latest_heartbeats(
    messages: list[dict[str, Any]], online_seconds: float, now: dt.datetime
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for message in recent_messages(messages, "heartbeat", online_seconds, now):
        bot = message.get("bot")
        timestamp = message.get("_timestamp")
        if not isinstance(bot, str) or not isinstance(timestamp, dt.datetime):
            continue
        previous = latest.get(bot)
        if not previous or timestamp > previous["_timestamp"]:
            latest[bot] = message
    return latest


def fresh_offers(
    messages: list[dict[str, Any]], offer_seconds: float, now: dt.datetime
) -> list[dict[str, Any]]:
    return [
        message
        for message in recent_messages(messages, "offer", offer_seconds, now)
        if isinstance(message.get("bot"), str)
        and isinstance(message.get("to"), str)
        and isinstance(message.get("game_id"), str)
    ]


def coord_message(payload: dict[str, Any]) -> str:
    return f"{COORD_PREFIX}{json.dumps(payload, separators=(',', ':'), sort_keys=True)}"


def self_heartbeat(
    bot_name: str, tournament_id: str, state: str, game_id: str | None, now: dt.datetime
) -> dict[str, Any]:
    return {
        "v": 1,
        "type": "heartbeat",
        "bot": bot_name,
        "tournament_id": tournament_id,
        "state": state,
        "game_id": game_id,
        "_timestamp": now,
    }
