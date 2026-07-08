# Configures application logs and file-only UHP wire logs for each bot run.
from __future__ import annotations

import datetime as dt
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import LoggingConfig


@dataclass(frozen=True)
class LoggingPaths:
    app_log: Path | None
    uhp_log: Path | None


def sanitize_filename(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return sanitized or "bot"


def configure_logging(bot_name: str, config: LoggingConfig) -> LoggingPaths:
    level = getattr(logging, config.level.upper(), None)
    if not isinstance(level, int):
        level = logging.INFO

    directory = Path(config.directory)
    directory.mkdir(parents=True, exist_ok=True)
    run_id = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = f"{sanitize_filename(bot_name)}-{run_id}"
    app_log = directory / f"{prefix}.log" if config.app_file else None
    uhp_log = directory / f"{prefix}.uhp.log" if config.uhp_file else None

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    if app_log:
        file_handler = logging.FileHandler(app_log, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    return LoggingPaths(app_log=app_log, uhp_log=uhp_log)


def create_uhp_logger(bot_name: str, path: Path | None) -> logging.Logger | None:
    if path is None:
        return None
    logger = logging.getLogger(f"bot_tournament.uhp_wire.{sanitize_filename(bot_name)}")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    logger.addHandler(handler)
    return logger
