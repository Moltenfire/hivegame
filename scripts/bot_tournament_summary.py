#!/usr/bin/env python3
# Thin executable entrypoint for the bot tournament coordinator package.
from bot_tournament.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
