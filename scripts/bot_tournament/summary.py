# Renders the tournament summary table and assigned openings for a bot.
from __future__ import annotations

from typing import Any

from .errors import UhpError
from .game_data import bot_games, game_id, game_result, player_name
from .openings import extract_opening_entries, opening_entries_by_game_id
from .uhp import UhpEngine


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


def opening_validation_status(
    engine: UhpEngine | None, game: dict[str, Any], opening: str
) -> str | None:
    if not opening:
        return None
    if engine is None:
        return None
    try:
        engine.validate_opening(game.get("game_type"), opening)
    except UhpError as exc:
        return f"invalid: {exc}"
    return None


def opening_display(moves: str, link: str | None, validation: str | None) -> str:
    value = f"[{moves}]({link})" if link else moves
    if validation:
        value = f"{value} [{validation}]"
    return value


def print_summary(
    tournament: dict[str, Any], bot_name: str, engine: UhpEngine | None = None
) -> None:
    tournament_name = tournament.get("name", "")
    tournament_id = tournament.get("tournament_id", "")
    title = f"{tournament_name} ({tournament_id})".strip()
    if title:
        print(title)
        print("=" * len(title))
        print()

    openings = extract_opening_entries(str(tournament.get("description") or ""))
    games = bot_games(tournament, bot_name)
    assigned_openings = opening_entries_by_game_id(games, openings)
    invalid_by_opening = {}
    invalid_openings = []
    for game in games:
        gid = game_id(game)
        opening = assigned_openings.get(gid)
        if not opening:
            continue
        validation = opening_validation_status(engine, game, opening.moves)
        if validation:
            invalid_by_opening[opening] = validation
            invalid_openings.append((gid, validation))

    print("Openings")
    print("--------")
    if openings:
        for opening in openings:
            print(
                f"- {opening_display(opening.moves, opening.link, invalid_by_opening.get(opening))}"
            )
    else:
        print("No openings found")
    print()

    print("Games")
    print("-----")
    if not games:
        print(f"No games found for {bot_name}")
        return

    rows = []
    for game in games:
        gid = game_id(game)
        opening = assigned_openings.get(gid)
        moves = opening.moves if opening else ""
        rows.append(
            [
                gid,
                player_name(game, "white"),
                player_name(game, "black"),
                game_result(game),
                moves,
            ]
        )

    render_table(["Game ID", "White", "Black", "Result", "Opening"], rows)

    if invalid_openings:
        failed = ", ".join(f"{gid}: {status}" for gid, status in invalid_openings)
        raise UhpError(f"Invalid opening assignment(s): {failed}")
