# Renders the tournament summary table and assigned openings for a bot.
from __future__ import annotations

from typing import Any

from .game_data import bot_games, game_id, game_result, player_name
from .openings import extract_openings, opening_by_game_id


def render_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [
        max(len(row[index]) for row in [headers, *rows])
        for index in range(len(headers))
    ]

    def render_row(row: list[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    print(render_row(headers))
    print("-+-".join("-" * width for width in widths))
    for row in rows:
        print(render_row(row))


def print_summary(tournament: dict[str, Any], bot_name: str) -> None:
    tournament_name = tournament.get("name", "")
    tournament_id = tournament.get("tournament_id", "")
    title = f"{tournament_name} ({tournament_id})".strip()
    if title:
        print(title)
        print("=" * len(title))
        print()

    print("Openings")
    print("--------")
    openings = extract_openings(str(tournament.get("description") or ""))
    if openings:
        for opening in openings:
            print(f"- {opening}")
    else:
        print("No openings found")
    print()

    print("Games")
    print("-----")
    games = bot_games(tournament, bot_name)
    if not games:
        print(f"No games found for {bot_name}")
        return

    assigned_openings = opening_by_game_id(games, openings)
    rows = []
    for game in games:
        gid = game_id(game)
        rows.append(
            [
                gid,
                player_name(game, "white"),
                player_name(game, "black"),
                game_result(game),
                assigned_openings.get(gid, ""),
            ]
        )

    render_table(["Game ID", "White", "Black", "Result", "Opening"], rows)
