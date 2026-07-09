# Command-line parsing and dispatch for summary and bot runner modes.
from __future__ import annotations

import argparse
import sys

from .api import AuthSession, get_tournament
from .config import load_bot_config, load_tournament_config
from .coordination import run_coordinator
from .errors import ApiError, ConfigError, UhpError
from .logging_setup import configure_logging, create_uhp_logger
from .summary import print_summary
from .uhp import UhpEngine


def parse_args() -> argparse.Namespace:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("tournament_config", help="Shared tournament JSON config file.")
    parent.add_argument("bot_config", help="JSON config file for this bot.")

    parser = argparse.ArgumentParser(
        description="Summarize or coordinate Hive bot tournament games."
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "summary",
        parents=[parent],
        help="Print the tournament summary table.",
    )
    subparsers.add_parser(
        "run",
        parents=[parent],
        help="Poll tournament chat and coordinate bot game starts.",
    )

    if len(sys.argv) > 1 and sys.argv[1] not in {"summary", "run", "-h", "--help"}:
        args = parser.parse_args(["summary", *sys.argv[1:]])
        args.command = "summary"
        return args

    return parser.parse_args()


def main() -> int:
    try:
        args = parse_args()
        if args.command is None:
            print("error: command required: summary or run", file=sys.stderr)
            return 2

        tournament_config = load_tournament_config(args.tournament_config)
        bot_config = load_bot_config(args.bot_config)

        if args.command == "run":
            paths = configure_logging(bot_config.name, bot_config.logging)
            uhp_logger = create_uhp_logger(bot_config.name, paths.uhp_log)
            engine = (
                UhpEngine(bot_config.uhp, bot_config.name, uhp_logger)
                if bot_config.uhp
                else None
            )
            return run_coordinator(tournament_config, bot_config, engine)

        auth = AuthSession(tournament_config.url, bot_config.email, bot_config.password)
        tournament = get_tournament(auth, tournament_config.tournament_id)
        engine = UhpEngine(bot_config.uhp, bot_config.name) if bot_config.uhp else None
        try:
            print_summary(tournament, bot_config.name, engine)
        finally:
            if engine:
                engine.close()
    except (ApiError, ConfigError, UhpError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0
