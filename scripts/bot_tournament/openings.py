# Extracts assigned tournament openings and checks game histories against them.
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .game_data import game_id, game_sort_key, moves_from_history, player_name, tournament_games


@dataclass(frozen=True)
class Opening:
    moves: str
    link: str | None = None


def opening_heading(line: str) -> bool:
    stripped = line.strip()
    while stripped.startswith("#"):
        stripped = stripped[1:].lstrip()
    return stripped.lower() == "openings"


def is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def split_opening_link(value: str) -> Opening:
    stripped = value.strip()
    markdown = re.match(r"^\[(?P<moves>[^\]]+)\]\((?P<link>[^)]+)\)\s*$", stripped)
    if markdown:
        return Opening(markdown.group("moves").strip(), markdown.group("link").strip())

    return Opening(stripped, None)


def extract_opening_entries(description: str) -> list[Opening]:
    openings: list[Opening] = []
    in_openings = False

    for line in description.splitlines():
        stripped = line.strip()
        if not in_openings:
            if opening_heading(stripped):
                in_openings = True
            continue

        if not stripped:
            continue
        if is_heading(stripped):
            break
        if stripped.startswith("- "):
            openings.append(split_opening_link(stripped[2:].strip()))
            continue
        break

    return openings


def extract_openings(description: str) -> list[str]:
    return [opening.moves for opening in extract_opening_entries(description)]


def moves_from_opening(opening: str) -> list[str]:
    return moves_from_history(opening)


def next_opening_move(game: dict[str, Any], opening: str) -> str | None:
    opening_moves = moves_from_opening(opening)
    if not opening_moves:
        return None

    history_moves = moves_from_history(game.get("history"))
    if len(history_moves) >= len(opening_moves):
        return None
    if history_moves != opening_moves[: len(history_moves)]:
        return None
    return opening_moves[len(history_moves)]


def opening_divergence(
    game: dict[str, Any], opening: str
) -> tuple[int, str | None, str | None] | None:
    opening_moves = moves_from_opening(opening)
    history_moves = moves_from_history(game.get("history"))
    if not opening_moves:
        return None

    for index, actual in enumerate(history_moves):
        if index >= len(opening_moves):
            return None
        expected = opening_moves[index]
        if actual != expected:
            return index, expected, actual
    return None


def opening_complete(game: dict[str, Any], opening: str) -> bool:
    opening_moves = moves_from_opening(opening)
    history_moves = moves_from_history(game.get("history"))
    return bool(opening_moves) and history_moves[: len(opening_moves)] == opening_moves


def opening_by_game_id(
    games: list[dict[str, Any]], openings: list[str]
) -> dict[str, str]:
    return {
        gid: opening.moves
        for gid, opening in opening_entries_by_game_id(
            games, [Opening(moves) for moves in openings]
        ).items()
    }


def opening_entries_by_game_id(
    games: list[dict[str, Any]], openings: list[Opening]
) -> dict[str, Opening]:
    assigned: dict[str, Opening] = {}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for game in games:
        key = (player_name(game, "white").casefold(), player_name(game, "black").casefold())
        groups.setdefault(key, []).append(game)

    for group in groups.values():
        for index, game in enumerate(sorted(group, key=lambda item: game_id(item))):
            assigned[game_id(game)] = openings[index] if index < len(openings) else Opening("")

    return assigned


def opening_map_for_tournament(tournament: dict[str, Any]) -> dict[str, str]:
    games = sorted(tournament_games(tournament), key=game_sort_key)
    openings = extract_openings(str(tournament.get("description") or ""))
    return opening_by_game_id(games, openings)
