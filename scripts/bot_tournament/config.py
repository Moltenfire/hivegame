# Loads and validates shared tournament config and per-bot runner config.
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError


DEFAULT_POLL_SECONDS = 5.0
DEFAULT_POLL_JITTER_SECONDS = 1.5


@dataclass(frozen=True)
class TournamentConfig:
    url: str
    tournament_id: str
    poll_seconds: float = DEFAULT_POLL_SECONDS
    poll_jitter_seconds: float = DEFAULT_POLL_JITTER_SECONDS


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    directory: str = "logs"
    app_file: bool = True
    uhp_file: bool = True


@dataclass(frozen=True)
class UhpConfig:
    command: str
    options: dict[str, str | int | float | bool] = field(default_factory=dict)
    bestmove: dict[str, Any] = field(default_factory=lambda: {"mode": "depth", "depth": 1})


@dataclass(frozen=True)
class BotConfig:
    name: str
    email: str
    password: str
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    uhp: UhpConfig | None = None


def load_json(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            config = json.load(file)
    except OSError as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError(f"Config file {path} must contain a JSON object")
    return config


def required_string(config: dict[str, Any], field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value:
        raise ConfigError(f"Config missing required string field: {field}")
    return value


def positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Config field {field} must be a positive integer") from exc
    if parsed <= 0:
        raise ConfigError(f"Config field {field} must be a positive integer")
    return parsed


def positive_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Config field {field} must be a positive number") from exc
    if parsed <= 0:
        raise ConfigError(f"Config field {field} must be a positive number")
    return parsed


def bool_field(config: dict[str, Any], field_name: str, default: bool) -> bool:
    value = config.get(field_name, default)
    if not isinstance(value, bool):
        raise ConfigError(f"Config field logging.{field_name} must be a boolean")
    return value


def validate_uhp_bestmove_config(config: dict[str, Any]) -> None:
    mode = str(config.get("mode", "depth"))
    if mode == "depth":
        positive_int(config.get("depth", 1), "uhp.bestmove.depth")
        return

    if mode != "time":
        raise ConfigError(f"Unsupported config field uhp.bestmove.mode: {mode}")

    protocol = str(config.get("protocol", "time"))
    if protocol not in {"time", "seconds", "clock"}:
        raise ConfigError(f"Unsupported config field uhp.bestmove.protocol: {protocol}")
    if protocol == "clock":
        return

    positive_float(config.get("seconds", 5), "uhp.bestmove.seconds")
    positive_float(config.get("min_seconds", 1), "uhp.bestmove.min_seconds")
    positive_float(
        config.get("max_seconds", config.get("seconds", 5)),
        "uhp.bestmove.max_seconds",
    )
    positive_int(config.get("moves_to_go", 20), "uhp.bestmove.moves_to_go")
    positive_float(
        config.get("max_clock_fraction", 0.5),
        "uhp.bestmove.max_clock_fraction",
    )


def load_tournament_config(path: str) -> TournamentConfig:
    config = load_json(path)
    return TournamentConfig(
        url=required_string(config, "url").rstrip("/"),
        tournament_id=required_string(config, "tournament_id"),
        poll_seconds=positive_float(
            config.get("poll_seconds", DEFAULT_POLL_SECONDS), "poll_seconds"
        ),
        poll_jitter_seconds=positive_float(
            config.get("poll_jitter_seconds", DEFAULT_POLL_JITTER_SECONDS),
            "poll_jitter_seconds",
        ),
    )


def load_logging_config(config: dict[str, Any]) -> LoggingConfig:
    raw = config.get("logging", {})
    if not isinstance(raw, dict):
        raise ConfigError("Config field logging must be an object")
    level = str(raw.get("level", "INFO")).upper()
    directory = raw.get("directory", "logs")
    if not isinstance(directory, str) or not directory:
        raise ConfigError("Config field logging.directory must be a non-empty string")
    return LoggingConfig(
        level=level,
        directory=directory,
        app_file=bool_field(raw, "app_file", True),
        uhp_file=bool_field(raw, "uhp_file", True),
    )


def load_uhp_config(config: dict[str, Any]) -> UhpConfig | None:
    raw = config.get("uhp")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("Config field uhp must be an object")

    command = raw.get("command")
    if not isinstance(command, str) or not command:
        raise ConfigError("Config field uhp.command must be a non-empty string")
    try:
        if not shlex.split(command):
            raise ConfigError("Config field uhp.command must include a command")
    except ValueError as exc:
        raise ConfigError(f"Config field uhp.command is not valid shell syntax: {exc}") from exc

    bestmove = raw.get("bestmove", {"mode": "depth", "depth": 1})
    if not isinstance(bestmove, dict):
        raise ConfigError("Config field uhp.bestmove must be an object")
    validate_uhp_bestmove_config(bestmove)

    options = raw.get("options", {})
    if not isinstance(options, dict):
        raise ConfigError("Config field uhp.options must be an object")
    for name, value in options.items():
        if not isinstance(name, str) or not name or " " in name or ";" in name:
            raise ConfigError("Config field uhp.options keys must be option names")
        if not isinstance(value, (str, int, float, bool)):
            raise ConfigError(f"Config field uhp.options.{name} must be a scalar value")
        if isinstance(value, str) and any(char.isspace() for char in value):
            raise ConfigError(f"Config field uhp.options.{name} must not contain whitespace")

    return UhpConfig(command=command, options=options, bestmove=bestmove)


def load_bot_config(path: str) -> BotConfig:
    config = load_json(path)
    return BotConfig(
        name=required_string(config, "name"),
        email=required_string(config, "email"),
        password=required_string(config, "password"),
        logging=load_logging_config(config),
        uhp=load_uhp_config(config),
    )


def resolve_config_path(base_file: str, configured_path: str) -> str:
    path = Path(configured_path)
    if path.is_absolute():
        return str(path)
    return str(Path(base_file).resolve().parent / path)
