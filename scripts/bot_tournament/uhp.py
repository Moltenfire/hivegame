# Manages a UHP engine subprocess and keeps its local state synced to the API.
from __future__ import annotations

import logging
import shlex
import subprocess
import threading
from typing import Any, TextIO

from .config import UhpConfig, positive_float, positive_int
from .errors import UhpError
from .game_data import api_game_id, moves_from_history

logger = logging.getLogger(__name__)


def format_hhmmss(seconds: float) -> str:
    total = max(1, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def uhp_option_value(value: str | int | float | bool) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def current_player_time_left_seconds(game: dict[str, Any]) -> float | None:
    current = str(game.get("current_player_id") or "")
    white_id = str(game.get("white_id") or "")
    black_id = str(game.get("black_id") or "")
    if current and current == white_id:
        raw = game.get("white_time_left")
    elif current and current == black_id:
        raw = game.get("black_time_left")
    else:
        turn = game.get("turn")
        raw = (
            game.get("white_time_left")
            if isinstance(turn, int) and turn % 2 == 0
            else game.get("black_time_left")
        )

    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw) / 1_000_000_000
    return None


def bestmove_command_for_game(game: dict[str, Any], config: dict[str, Any]) -> str:
    mode = str(config.get("mode", "depth"))
    if mode == "depth":
        depth = positive_int(config.get("depth", 1), "uhp.bestmove.depth")
        return f"bestmove depth {depth}"

    if mode != "time":
        raise UhpError(f"Unsupported uhp.bestmove.mode: {mode}")

    fallback_seconds = positive_float(config.get("seconds", 5), "uhp.bestmove.seconds")
    seconds = fallback_seconds
    if config.get("use_clock", False):
        remaining = current_player_time_left_seconds(game)
        if remaining is not None:
            moves_to_go = positive_int(
                config.get("moves_to_go", 20), "uhp.bestmove.moves_to_go"
            )
            seconds = remaining / moves_to_go
            max_fraction = positive_float(
                config.get("max_clock_fraction", 0.5),
                "uhp.bestmove.max_clock_fraction",
            )
            seconds = min(seconds, remaining * max_fraction)

    min_seconds = positive_float(config.get("min_seconds", 1), "uhp.bestmove.min_seconds")
    max_seconds = positive_float(
        config.get("max_seconds", fallback_seconds), "uhp.bestmove.max_seconds"
    )
    seconds = max(min_seconds, min(seconds, max_seconds))

    protocol = str(config.get("protocol", "time"))
    if protocol == "seconds":
        return f"bestmove seconds {seconds:g}"
    if protocol == "time":
        return f"bestmove time {format_hhmmss(seconds)}"
    raise UhpError(f"Unsupported uhp.bestmove.protocol: {protocol}")


def game_type_for_uhp(value: Any) -> str:
    game_type = str(value or "Base")
    if game_type == "Base" or game_type.startswith("Base+"):
        return game_type
    return f"Base+{game_type}"


class UhpEngine:
    def __init__(
        self, config: UhpConfig, bot_name: str, wire_logger: logging.Logger | None = None
    ) -> None:
        self.command = config.command
        self.bestmove_config = config.bestmove
        self.options = config.options
        self.bot_name = bot_name
        self.wire_logger = wire_logger
        self.proc: subprocess.Popen[str] | None = None
        self.stderr_thread: threading.Thread | None = None
        self.active_game_id: str | None = None
        self.active_game_type: str | None = None
        self.synced_moves: list[str] = []

    def start(self) -> None:
        if self.proc is not None:
            return
        try:
            self.proc = subprocess.Popen(
                shlex.split(self.command),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if self.wire_logger else subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise UhpError(f"Could not start UHP engine for {self.bot_name}: {exc}") from exc

        if self.proc.stderr is not None and self.wire_logger is not None:
            self.stderr_thread = threading.Thread(
                target=self._log_stderr,
                args=(self.proc.stderr,),
                daemon=True,
            )
            self.stderr_thread.start()

        self._read_until_ok()
        self.configure_options()
        logger.info("started UHP engine for %s: %s", self.bot_name, self.command)

    def configure_options(self) -> None:
        for name, value in self.options.items():
            self.command_io(f"options set {name} {uhp_option_value(value)}")
            logger.info("set UHP option for %s: %s=%s", self.bot_name, name, value)

    def close(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    def clear_game(self) -> None:
        self.active_game_id = None
        self.active_game_type = None
        self.synced_moves = []

    def _log_stderr(self, stream: TextIO) -> None:
        assert self.wire_logger is not None
        for line in stream:
            self.wire_logger.info("!!! %s", line.rstrip("\n"))

    def _log_wire(self, direction: str, line: str) -> None:
        if self.wire_logger:
            self.wire_logger.info("%s %s", direction, line)

    def _read_until_ok(self) -> list[str]:
        if self.proc is None or self.proc.stdout is None:
            raise UhpError("UHP engine is not running")
        lines: list[str] = []
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise UhpError("UHP engine closed stdout")
            stripped = line.strip()
            self._log_wire("<<<", stripped)
            if stripped == "ok":
                return lines
            if stripped.startswith("err ") or stripped.startswith("invalidmove "):
                raise UhpError(stripped)
            if stripped:
                lines.append(stripped)

    def command_io(self, command: str) -> list[str]:
        self.start()
        if self.proc is None or self.proc.stdin is None:
            raise UhpError("UHP engine stdin is not available")
        self._log_wire(">>>", command)
        self.proc.stdin.write(f"{command}\n")
        self.proc.stdin.flush()
        return self._read_until_ok()

    def begin_game(self, game: dict[str, Any]) -> None:
        gid = api_game_id(game)
        game_type = game_type_for_uhp(game.get("game_type"))
        self.command_io(f"newgame {game_type}")
        self.active_game_id = gid
        self.active_game_type = game_type
        self.synced_moves = []

    def sync_to_game(self, game: dict[str, Any]) -> None:
        gid = api_game_id(game)
        game_type = game_type_for_uhp(game.get("game_type"))
        if self.active_game_id is None:
            self.begin_game(game)
        elif self.active_game_id != gid:
            raise UhpError(
                f"UHP engine is already tracking {self.active_game_id}, not {gid}"
            )
        elif self.active_game_type != game_type:
            raise UhpError(
                f"UHP game type changed from {self.active_game_type} to {game_type}"
            )

        api_moves = moves_from_history(game.get("history"))
        if api_moves[: len(self.synced_moves)] != self.synced_moves:
            raise UhpError("UHP engine history no longer matches API history")
        for move in api_moves[len(self.synced_moves) :]:
            self.command_io(f"play {move}")
            self.synced_moves.append(move)

    def record_accepted_move(
        self, game_id: str, move: str, authoritative_history: Any = None
    ) -> None:
        if self.active_game_id != game_id:
            return
        self.command_io(f"play {move}")
        api_moves = moves_from_history(authoritative_history)
        if api_moves:
            if len(api_moves) < len(self.synced_moves):
                raise UhpError("API history moved backwards after accepted move")
            if len(api_moves) > len(self.synced_moves) + 1:
                raise UhpError("API history advanced by more than one move after accepted move")
            self.synced_moves = api_moves
        else:
            self.synced_moves.append(move)

    def bestmove(self, game: dict[str, Any]) -> str:
        self.sync_to_game(game)
        output = self.command_io(bestmove_command_for_game(game, self.bestmove_config))
        candidates = [line for line in output if not line.startswith("stats ")]
        if not candidates:
            raise UhpError("UHP engine returned no bestmove")
        return candidates[-1].rstrip(";")
