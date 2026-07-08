# Helpers for reading tournament game dictionaries returned by the bot API.
from __future__ import annotations

import re
from typing import Any

from .errors import ApiError


def player_name(game: dict[str, Any], color: str) -> str:
    player = game.get(f"{color}_player") or {}
    return str(player.get("username", ""))


def player_uid(game: dict[str, Any], color: str) -> str:
    player = game.get(f"{color}_player") or {}
    value = player.get("uid")
    return str(value) if value else ""


def player_names(game: dict[str, Any]) -> tuple[str, str]:
    return player_name(game, "white"), player_name(game, "black")


def game_id(game: dict[str, Any]) -> str:
    return str(game.get("game_id", ""))


def api_game_id(game: dict[str, Any]) -> str:
    for key in ("game_id", "nanoid"):
        value = game.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def moves_from_history(history: Any) -> list[str]:
    if not isinstance(history, str) or not history.strip():
        return []
    return [move.strip() for move in history.replace(" ;", ";").split(";") if move.strip()]


def natural_text_key(value: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", value.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def game_sort_key(game: dict[str, Any]) -> tuple[tuple[Any, ...], tuple[Any, ...], str]:
    return (
        natural_text_key(player_name(game, "white")),
        natural_text_key(player_name(game, "black")),
        game_id(game),
    )


def bot_games(tournament: dict[str, Any], bot_name: str) -> list[dict[str, Any]]:
    games = tournament_games(tournament)
    filtered = []
    for game in games:
        white, black = player_names(game)
        if white.casefold() == bot_name.casefold() or black.casefold() == bot_name.casefold():
            filtered.append(game)
    return sorted(filtered, key=game_sort_key)


def tournament_games(tournament: dict[str, Any]) -> list[dict[str, Any]]:
    games = tournament.get("games")
    if not isinstance(games, list):
        raise ApiError("API response did not include tournament.games as a list")
    return [game for game in games if isinstance(game, dict)]


def game_result(game: dict[str, Any]) -> str:
    result = game.get("tournament_game_result") or ""
    if result == "Unknown":
        return ""
    if result == "Draw":
        return "1/2-1/2"
    if result == "DoubeForfeit":
        return "0-0"
    if isinstance(result, dict) and set(result.keys()) == {"Winner"}:
        winner = result["Winner"]
        if winner == "White":
            return "1-0"
        if winner == "Black":
            return "0-1"
        return f"Winner: {winner}"
    return str(result)


def bot_color_and_id(game: dict[str, Any], bot_name: str) -> tuple[str, str] | None:
    if player_name(game, "white") == bot_name:
        return "white", player_uid(game, "white")
    if player_name(game, "black") == bot_name:
        return "black", player_uid(game, "black")
    return None


def player_in_game(game: dict[str, Any], bot_name: str) -> bool:
    white, black = player_names(game)
    return bot_name == white or bot_name == black


def focused_tournament_game(
    tournament: dict[str, Any], focused_game_id: str
) -> dict[str, Any] | None:
    return next(
        (game for game in tournament_games(tournament) if game_id(game) == focused_game_id),
        None,
    )


def color_to_move_from_history(game: dict[str, Any]) -> str:
    return "white" if len(moves_from_history(game.get("history"))) % 2 == 0 else "black"
