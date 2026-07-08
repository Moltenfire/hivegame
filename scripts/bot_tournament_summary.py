#!/usr/bin/env python3
import argparse
import base64
import binascii
import datetime as dt
import hashlib
import json
import re
import shlex
import subprocess
import sys
import time
from typing import Any

import requests


COORD_PREFIX = "[bot-coord] "
POLL_SECONDS = 5.0
POLL_JITTER_SECONDS = 1.5
HEARTBEAT_SECONDS = 20.0
ONLINE_SECONDS = 45.0
OFFER_SECONDS = 30.0


class ApiError(Exception):
    pass


class ConfigError(Exception):
    pass


class UhpError(Exception):
    pass


def parse_args() -> argparse.Namespace:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("config", help="JSON config file for this bot.")

    parser = argparse.ArgumentParser(
        description="Print a bot's games and openings for a Hive tournament."
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser(
        "summary",
        parents=[parent],
        help="Print the tournament summary table.",
    )
    run_parser = subparsers.add_parser(
        "run",
        parents=[parent],
        help="Poll tournament chat and coordinate bot game starts.",
    )
    run_parser.add_argument(
        "--poll-seconds",
        type=float,
        default=POLL_SECONDS,
        help=f"Base polling interval. Defaults to {POLL_SECONDS:g} seconds.",
    )

    if len(sys.argv) > 1 and sys.argv[1] not in {"summary", "run", "-h", "--help"}:
        args = parser.parse_args(["summary", *sys.argv[1:]])
        args.command = "summary"
        return args

    return parser.parse_args()


def load_config(path: str) -> dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as file:
            config = json.load(file)
    except OSError as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file {path} is not valid JSON: {exc}") from exc
    if not isinstance(config, dict):
        raise ConfigError("Config file must contain a JSON object")
    return config


def apply_config(args: argparse.Namespace) -> argparse.Namespace:
    config = load_config(args.config)
    args.url = config.get("url")
    args.tournament_id = config.get("tournament_id")
    args.bot_name = config.get("name")
    args.email = config.get("email")
    args.password = config.get("password")
    args.uhp = config.get("uhp")

    required = {
        "url": args.url,
        "tournament_id": args.tournament_id,
        "name": args.bot_name,
        "email": args.email,
        "password": args.password,
    }
    missing = [key for key, value in required.items() if not isinstance(value, str) or not value]
    if missing:
        raise ConfigError(f"Config missing required string field(s): {', '.join(missing)}")
    if args.uhp is not None:
        if not isinstance(args.uhp, dict):
            raise ConfigError("Config field uhp must be an object")
        command = args.uhp.get("command")
        if not isinstance(command, str) or not command:
            raise ConfigError("Config field uhp.command must be a non-empty string")
        try:
            if not shlex.split(command):
                raise ConfigError("Config field uhp.command must include a command")
        except ValueError as exc:
            raise ConfigError(f"Config field uhp.command is not valid shell syntax: {exc}") from exc
        bestmove = args.uhp.get("bestmove", {"mode": "depth", "depth": 1})
        if not isinstance(bestmove, dict):
            raise ConfigError("Config field uhp.bestmove must be an object")
        validate_uhp_bestmove_config(bestmove)
        args.uhp["bestmove"] = bestmove
    return args


def decode_jwt_exp(token: str) -> int | None:
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        claims = json.loads(decoded)
    except (binascii.Error, ValueError, json.JSONDecodeError):
        return None
    exp = claims.get("exp")
    return exp if isinstance(exp, int) else None


def format_hhmmss(seconds: float) -> str:
    total = max(1, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


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


def validate_uhp_bestmove_config(config: dict[str, Any]) -> None:
    mode = str(config.get("mode", "depth"))
    if mode == "depth":
        positive_int(config.get("depth", 1), "uhp.bestmove.depth")
        return

    if mode != "time":
        raise ConfigError(f"Unsupported config field uhp.bestmove.mode: {mode}")

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
    protocol = str(config.get("protocol", "time"))
    if protocol not in {"time", "seconds"}:
        raise ConfigError(f"Unsupported config field uhp.bestmove.protocol: {protocol}")


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


class UhpEngine:
    def __init__(self, config: dict[str, Any], bot_name: str) -> None:
        self.command = str(config["command"])
        self.bestmove_config = config.get("bestmove", {"mode": "depth", "depth": 1})
        self.bot_name = bot_name
        self.proc: subprocess.Popen[str] | None = None
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
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise UhpError(f"Could not start UHP engine for {self.bot_name}: {exc}") from exc

        self._read_until_ok()
        print(f"started UHP engine for {self.bot_name}: {self.command}", flush=True)

    def close(self) -> None:
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None

    def _read_until_ok(self) -> list[str]:
        if self.proc is None or self.proc.stdout is None:
            raise UhpError("UHP engine is not running")
        lines: list[str] = []
        while True:
            line = self.proc.stdout.readline()
            if line == "":
                raise UhpError("UHP engine closed stdout")
            stripped = line.strip()
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
        self, game_id_: str, move: str, authoritative_history: Any = None
    ) -> None:
        if self.active_game_id != game_id_:
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


class AuthSession:
    def __init__(
        self,
        base_url: str,
        email: str,
        password: str,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token: str | None = None
        self.email = email
        self.password = password
        self.token_exp: int | None = None

    def bearer_token(self) -> str:
        if self.token_expiring_soon():
            self.refresh()
        if not self.token:
            self.refresh()
        if not self.token:
            raise ApiError("No bearer token available")
        return self.token

    def token_expiring_soon(self) -> bool:
        if not self.token:
            return True
        if self.token_exp is None:
            return False
        return self.token_exp <= int(time.time()) + 120

    def refresh(self) -> None:
        url = f"{self.base_url}/api/v1/auth/token"
        try:
            response = requests.post(
                url,
                json={"email": self.email, "password": self.password},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ApiError(f"Could not call {url}: {exc}") from exc

        if not response.ok:
            raise ApiError(
                f"HTTP {response.status_code} from {url}: {response.text.strip()}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ApiError(f"Response from {url} was not JSON") from exc

        if not payload.get("success"):
            message = payload.get("data", {}).get("message", "unknown API error")
            raise ApiError(f"API error from {url}: {message}")

        token = payload.get("data", {}).get("token")
        if not isinstance(token, str):
            raise ApiError(f"API response from {url} did not include data.token")

        self.token = token
        self.token_exp = decode_jwt_exp(token)
        if self.token_exp:
            expires_at = dt.datetime.fromtimestamp(self.token_exp, dt.UTC)
            print(f"refreshed token; expires at {expires_at.isoformat()}", flush=True)
        else:
            print("refreshed token", flush=True)


def is_expired_signature(response: requests.Response, payload: dict[str, Any] | None) -> bool:
    if response.status_code == 401:
        return True
    if not payload:
        return False
    data = payload.get("data")
    if not isinstance(data, dict):
        return False
    return data.get("message") == "ExpiredSignature" or data.get("error") == "ExpiredSignature"


def api_request(
    method: str,
    auth: AuthSession,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{auth.base_url}{path}"
    for attempt in range(2):
        try:
            response = requests.request(
                method,
                url,
                headers={"Authorization": f"Bearer {auth.bearer_token()}"},
                json=body,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ApiError(f"Could not call {url}: {exc}") from exc

        payload = None
        try:
            payload = response.json()
        except ValueError:
            pass

        if is_expired_signature(response, payload) and attempt == 0:
            auth.refresh()
            continue

        if not response.ok:
            raise ApiError(
                f"HTTP {response.status_code} from {url}: {response.text.strip()}"
            )

        if payload is None:
            raise ApiError(f"Response from {url} was not JSON")

        if not payload.get("success"):
            data_payload = payload.get("data", {})
            if isinstance(data_payload, dict):
                error = (
                    data_payload.get("error")
                    or data_payload.get("message")
                    or "unknown API error"
                )
            else:
                error = "unknown API error"
            raise ApiError(f"API error from {url}: {error}")

        data = payload.get("data")
        if not isinstance(data, dict):
            raise ApiError(f"API response from {url} did not include data")

        return data

    raise ApiError(f"Could not refresh token for {url}")


def get_tournament(auth: AuthSession, tournament_id: str) -> dict[str, Any]:
    data = api_request(
        "GET",
        auth,
        f"/api/v1/bot/tournament/{tournament_id}",
    )
    tournament = data.get("tournament")
    if not isinstance(tournament, dict):
        raise ApiError("API response did not include data.tournament")

    return tournament


def get_tournament_chat(auth: AuthSession, tournament_id: str) -> list[dict[str, Any]]:
    data = api_request(
        "GET",
        auth,
        f"/api/v1/bot/tournament/{tournament_id}/chat",
    )
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ApiError("API response did not include data.messages as a list")
    return [message for message in messages if isinstance(message, dict)]


def get_pending_games(auth: AuthSession) -> list[dict[str, Any]]:
    data = api_request("GET", auth, "/api/v1/bot/games/pending")
    games = data.get("games")
    if not isinstance(games, list):
        raise ApiError("API response did not include data.games as a list")
    return [game for game in games if isinstance(game, dict)]


def post_tournament_chat(
    auth: AuthSession, tournament_id: str, message: str
) -> None:
    api_request(
        "POST",
        auth,
        f"/api/v1/bot/tournament/{tournament_id}/chat",
        {"message": message},
    )


def start_game(auth: AuthSession, game_id_: str) -> dict[str, Any]:
    return api_request(
        "POST",
        auth,
        "/api/v1/bot/games/control",
        {"game_id": game_id_, "control": "start"},
    )


def play_move(auth: AuthSession, game_id_: str, move: str) -> dict[str, Any]:
    return api_request(
        "POST",
        auth,
        "/api/v1/bot/games/play",
        {"game_id": game_id_, "piece_pos": move},
    )


def opening_heading(line: str) -> bool:
    stripped = line.strip()
    while stripped.startswith("#"):
        stripped = stripped[1:].lstrip()
    return stripped.lower() == "openings"


def is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def extract_openings(description: str) -> list[str]:
    openings: list[str] = []
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
            openings.append(stripped[2:].strip())
            continue
        break

    return openings


def bot_games(tournament: dict[str, Any], bot_name: str) -> list[dict[str, Any]]:
    games = tournament.get("games")
    if not isinstance(games, list):
        raise ApiError("API response did not include tournament.games as a list")

    bot_name_lower = bot_name.lower()
    filtered = []
    for game in games:
        if not isinstance(game, dict):
            continue
        white = game.get("white_player") or {}
        black = game.get("black_player") or {}
        white_name = str(white.get("username", ""))
        black_name = str(black.get("username", ""))
        if white_name.lower() == bot_name_lower or black_name.lower() == bot_name_lower:
            filtered.append(game)

    return sorted(filtered, key=game_sort_key)


def player_name(game: dict[str, Any], color: str) -> str:
    player = game.get(f"{color}_player") or {}
    return str(player.get("username", ""))


def player_uid(game: dict[str, Any], color: str) -> str:
    player = game.get(f"{color}_player") or {}
    value = player.get("uid")
    return str(value) if value else ""


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


def moves_from_opening(opening: str) -> list[str]:
    return moves_from_history(opening)


def game_type_for_uhp(value: Any) -> str:
    game_type = str(value or "Base")
    if game_type == "Base" or game_type.startswith("Base+"):
        return game_type
    return f"Base+{game_type}"


def game_status_for_uhp(value: Any) -> str:
    status = str(value or "NotStarted")
    if status == "Finished(Draw)":
        return "Draw"
    if "Winner(White)" in status:
        return "WhiteWins"
    if "Winner(Black)" in status:
        return "BlackWins"
    return status


def turn_string_for_uhp(game: dict[str, Any]) -> str:
    player_turn = game.get("player_turn")
    if isinstance(player_turn, str) and player_turn:
        return player_turn

    turn = game.get("turn")
    if not isinstance(turn, int):
        turn = len(moves_from_history(game.get("history")))
    color = "White" if turn % 2 == 0 else "Black"
    turn_number = turn // 2 + 1
    return f"{color}[{turn_number}]"


def game_string_for_uhp(game: dict[str, Any]) -> str:
    parts = [
        game_type_for_uhp(game.get("game_type")),
        game_status_for_uhp(game.get("game_status")),
        turn_string_for_uhp(game),
    ]
    history = ";".join(moves_from_history(game.get("history")))
    if history:
        parts.append(history)
    return ";".join(parts)


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


def natural_text_key(value: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", value.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def game_sort_key(game: dict[str, Any]) -> tuple[tuple[Any, ...], tuple[Any, ...], str]:
    return (
        natural_text_key(player_name(game, "white")),
        natural_text_key(player_name(game, "black")),
        game_id(game),
    )


def opening_by_game_id(
    games: list[dict[str, Any]], openings: list[str]
) -> dict[str, str]:
    assigned: dict[str, str] = {}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for game in games:
        key = (player_name(game, "white").casefold(), player_name(game, "black").casefold())
        groups.setdefault(key, []).append(game)

    for group in groups.values():
        for index, game in enumerate(sorted(group, key=lambda item: game_id(item))):
            assigned[game_id(game)] = openings[index] if index < len(openings) else ""

    return assigned


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


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def parse_timestamp(value: Any, fallback: dt.datetime) -> dt.datetime:
    if not isinstance(value, str):
        return fallback
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.UTC)
    except ValueError:
        return fallback


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
    messages: list[dict[str, Any]], now: dt.datetime
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for message in recent_messages(messages, "heartbeat", ONLINE_SECONDS, now):
        bot = message.get("bot")
        timestamp = message.get("_timestamp")
        if not isinstance(bot, str) or not isinstance(timestamp, dt.datetime):
            continue
        previous = latest.get(bot)
        if not previous or timestamp > previous["_timestamp"]:
            latest[bot] = message
    return latest


def fresh_offers(messages: list[dict[str, Any]], now: dt.datetime) -> list[dict[str, Any]]:
    return [
        message
        for message in recent_messages(messages, "offer", OFFER_SECONDS, now)
        if isinstance(message.get("bot"), str)
        and isinstance(message.get("to"), str)
        and isinstance(message.get("game_id"), str)
    ]


def player_names(game: dict[str, Any]) -> tuple[str, str]:
    return player_name(game, "white"), player_name(game, "black")


def bot_color_and_id(game: dict[str, Any], bot_name: str) -> tuple[str, str] | None:
    if player_name(game, "white") == bot_name:
        return "white", player_uid(game, "white")
    if player_name(game, "black") == bot_name:
        return "black", player_uid(game, "black")
    return None


def player_in_game(game: dict[str, Any], bot_name: str) -> bool:
    white, black = player_names(game)
    return bot_name == white or bot_name == black


def in_progress_game_for(
    games: list[dict[str, Any]], bot_name: str
) -> dict[str, Any] | None:
    matches = [
        game
        for game in games
        if (
            isinstance(game, dict)
            and not game.get("finished", False)
            and game.get("game_status") == "InProgress"
            and player_in_game(game, bot_name)
        )
    ]
    if not matches:
        return None
    return sorted(matches, key=game_sort_key)[0]


def offer_players(offer: dict[str, Any]) -> set[str]:
    return {str(offer.get("bot", "")), str(offer.get("to", ""))}


def offer_matches_game(offer: dict[str, Any], game: dict[str, Any]) -> bool:
    white, black = player_names(game)
    return offer.get("game_id") == game_id(game) and offer_players(offer) == {white, black}


def bot_has_fresh_offer(bot_name: str, offers: list[dict[str, Any]]) -> bool:
    return any(bot_name in offer_players(offer) for offer in offers)


def bot_unavailable(
    bot_name: str,
    games: list[dict[str, Any]],
    heartbeats: dict[str, dict[str, Any]],
    offers: list[dict[str, Any]],
    allowed_offer: dict[str, Any] | None = None,
) -> bool:
    if in_progress_game_for(games, bot_name):
        return True

    blocking_offers = [
        offer for offer in offers if allowed_offer is None or offer is not allowed_offer
    ]
    if bot_has_fresh_offer(bot_name, blocking_offers):
        return True

    state = str(heartbeats.get(bot_name, {}).get("state", ""))
    if state == "busy":
        return True
    if state == "offering" and allowed_offer is None:
        return True
    return False


def game_eligible(
    game: dict[str, Any],
    games: list[dict[str, Any]],
    heartbeats: dict[str, dict[str, Any]],
    offers: list[dict[str, Any]],
    allowed_offer: dict[str, Any] | None = None,
) -> bool:
    if game.get("finished", False) or game.get("game_status") != "NotStarted":
        return False

    white, black = player_names(game)
    if not white or not black or white not in heartbeats or black not in heartbeats:
        return False

    if (
        bot_unavailable(white, games, heartbeats, offers, allowed_offer)
        or bot_unavailable(black, games, heartbeats, offers, allowed_offer)
    ):
        return False

    for offer in offers:
        if allowed_offer is not None and offer is allowed_offer:
            continue
        if white in offer_players(offer) or black in offer_players(offer):
            return False

    return True


def eligible_games(
    games: list[dict[str, Any]],
    heartbeats: dict[str, dict[str, Any]],
    offers: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return sorted(
        [
            game
            for game in games
            if isinstance(game, dict)
            and game_eligible(game, games, heartbeats, offers)
        ],
        key=game_sort_key,
    )


def deterministic_jitter(bot_name: str) -> float:
    digest = hashlib.sha256(bot_name.encode("utf-8")).digest()
    value = int.from_bytes(digest[:2], "big") / 65535
    return value * POLL_JITTER_SECONDS


def coord_message(payload: dict[str, Any]) -> str:
    return f"{COORD_PREFIX}{json.dumps(payload, separators=(',', ':'), sort_keys=True)}"


def heartbeat_state(
    bot_name: str, games: list[dict[str, Any]], offers: list[dict[str, Any]]
) -> tuple[str, str | None]:
    game = in_progress_game_for(games, bot_name)
    if game:
        return "busy", game_id(game)
    for offer in offers:
        if offer.get("bot") == bot_name:
            return "offering", str(offer.get("game_id"))
    return "idle", None


def post_heartbeat(
    auth: AuthSession,
    tournament_id: str,
    bot_name: str,
    state: str,
    game_id_: str | None,
) -> None:
    post_tournament_chat(
        auth,
        tournament_id,
        coord_message(
            {
                "v": 1,
                "type": "heartbeat",
                "bot": bot_name,
                "tournament_id": tournament_id,
                "state": state,
                "game_id": game_id_,
            }
        ),
    )


def post_offer(
    auth: AuthSession,
    tournament_id: str,
    bot_name: str,
    opponent: str,
    game_id_: str,
) -> None:
    post_tournament_chat(
        auth,
        tournament_id,
        coord_message(
            {
                "v": 1,
                "type": "offer",
                "bot": bot_name,
                "to": opponent,
                "tournament_id": tournament_id,
                "game_id": game_id_,
            }
        ),
    )


def post_accept(
    auth: AuthSession,
    tournament_id: str,
    bot_name: str,
    opponent: str,
    game_id_: str,
) -> None:
    post_tournament_chat(
        auth,
        tournament_id,
        coord_message(
            {
                "v": 1,
                "type": "accept",
                "bot": bot_name,
                "to": opponent,
                "tournament_id": tournament_id,
                "game_id": game_id_,
            }
        ),
    )


def tournament_games(tournament: dict[str, Any]) -> list[dict[str, Any]]:
    games = tournament.get("games")
    if not isinstance(games, list):
        raise ApiError("API response did not include tournament.games as a list")
    return [game for game in games if isinstance(game, dict)]


def opening_map_for_tournament(tournament: dict[str, Any]) -> dict[str, str]:
    games = sorted(tournament_games(tournament), key=game_sort_key)
    openings = extract_openings(str(tournament.get("description") or ""))
    return opening_by_game_id(games, openings)


def focused_tournament_game(
    tournament: dict[str, Any], focused_game_id: str
) -> dict[str, Any] | None:
    return next(
        (game for game in tournament_games(tournament) if game_id(game) == focused_game_id),
        None,
    )


def color_to_move_from_history(game: dict[str, Any]) -> str:
    return "white" if len(moves_from_history(game.get("history"))) % 2 == 0 else "black"


def play_bot_move(
    auth: AuthSession,
    tournament_id: str,
    bot_name: str,
    gid: str,
    move: str,
    source: str,
    engine: UhpEngine | None,
) -> bool:
    print(f"playing {source} move for {gid}: {move}", flush=True)
    try:
        outcome = play_move(auth, gid, move)
    except ApiError as exc:
        message = f"{bot_name} stopped playing {gid}: {source} move rejected: {exc}"
        print(message, flush=True)
        post_tournament_chat(auth, tournament_id, message)
        return False
    if engine is not None:
        try:
            engine.record_accepted_move(gid, move, outcome.get("history"))
        except UhpError as exc:
            message = f"{bot_name} stopped playing {gid}: UHP engine error after {source} move: {exc}"
            print(message, flush=True)
            post_tournament_chat(auth, tournament_id, message)
            return False
    return True


def play_pending_opening_moves(
    auth: AuthSession,
    tournament: dict[str, Any],
    tournament_id: str,
    bot_name: str,
    focused_game_id: str,
    engine: UhpEngine | None,
) -> bool:
    assigned_openings = opening_map_for_tournament(tournament)
    tournament_game = focused_tournament_game(tournament, focused_game_id)
    if not tournament_game:
        return True
    bot_identity = bot_color_and_id(tournament_game, bot_name)
    if not bot_identity:
        message = f"{bot_name} stopped playing {focused_game_id}: bot is not assigned to this game."
        print(message, flush=True)
        post_tournament_chat(auth, tournament_id, message)
        return False
    bot_color, _bot_user_id = bot_identity

    for game in get_pending_games(auth):
        gid = api_game_id(game)
        if gid != focused_game_id:
            continue
        color_to_move = color_to_move_from_history(game)
        if color_to_move != bot_color:
            print(
                f"skipping {gid}: ply says {color_to_move} to move, not {bot_name} ({bot_color})",
                flush=True,
            )
            return True
        opening = assigned_openings.get(gid)

        if opening:
            divergence = opening_divergence(game, opening)
            if divergence:
                index, expected, actual = divergence
                message = (
                    f"{bot_name} stopped playing {gid}: opening mismatch at move "
                    f"{index + 1}. Expected '{expected}', got '{actual}'."
                )
                print(message, flush=True)
                post_tournament_chat(auth, tournament_id, message)
                return False

            if not opening_complete(game, opening):
                move = next_opening_move(game, opening)
                if not move:
                    return False

                return play_bot_move(
                    auth,
                    tournament_id,
                    bot_name,
                    gid,
                    move,
                    "opening",
                    engine,
                )

        if engine is None:
            return False

        try:
            move = engine.bestmove(game)
        except UhpError as exc:
            message = f"{bot_name} stopped playing {gid}: UHP engine error: {exc}"
            print(message, flush=True)
            post_tournament_chat(auth, tournament_id, message)
            return False

        return play_bot_move(
            auth,
            tournament_id,
            bot_name,
            gid,
            move,
            "UHP",
            engine,
        )

    return True


def self_heartbeat(
    bot_name: str, tournament_id: str, state: str, game_id_: str | None
) -> dict[str, Any]:
    return {
        "v": 1,
        "type": "heartbeat",
        "bot": bot_name,
        "tournament_id": tournament_id,
        "state": state,
        "game_id": game_id_,
        "_timestamp": utc_now(),
    }


def run_once(
    auth: AuthSession,
    tournament_id: str,
    bot_name: str,
    last_heartbeat_at: dt.datetime | None,
    focused_game_id: str | None,
    stopped_playing: bool,
    engine: UhpEngine | None,
) -> tuple[dt.datetime | None, str | None, bool]:
    if focused_game_id and stopped_playing:
        return last_heartbeat_at, focused_game_id, stopped_playing

    now = utc_now()
    tournament = get_tournament(auth, tournament_id)

    if focused_game_id:
        if not stopped_playing:
            keep_playing = play_pending_opening_moves(
                auth,
                tournament,
                tournament_id,
                bot_name,
                focused_game_id,
                engine,
            )
            stopped_playing = not keep_playing
        return last_heartbeat_at, focused_game_id, stopped_playing

    games = tournament_games(tournament)
    resumed_game = in_progress_game_for(games, bot_name)
    if resumed_game:
        resumed_game_id = game_id(resumed_game)
        print(f"resuming {resumed_game_id}; leaving chat coordination", flush=True)
        keep_playing = play_pending_opening_moves(
            auth,
            tournament,
            tournament_id,
            bot_name,
            resumed_game_id,
            engine,
        )
        return last_heartbeat_at, resumed_game_id, not keep_playing

    chat = get_tournament_chat(auth, tournament_id)
    messages = coord_messages(chat, tournament_id, now)
    offers = fresh_offers(messages, now)
    state, state_game_id = heartbeat_state(bot_name, games, offers)

    if (
        last_heartbeat_at is None
        or (now - last_heartbeat_at).total_seconds() >= HEARTBEAT_SECONDS
    ):
        post_heartbeat(auth, tournament_id, bot_name, state, state_game_id)
        last_heartbeat_at = utc_now()
        messages.append(self_heartbeat(bot_name, tournament_id, state, state_game_id))

    heartbeats = latest_heartbeats(messages, utc_now())

    if state != "idle":
        return last_heartbeat_at, None, False

    incoming = sorted(
        [offer for offer in offers if offer.get("to") == bot_name],
        key=lambda offer: (
            next(
                (
                    game_sort_key(game)
                    for game in games
                    if game_id(game) == offer.get("game_id")
                ),
                ((), (), str(offer.get("game_id", ""))),
            ),
            offer.get("_timestamp", now),
        ),
    )
    for offer in incoming:
        game = next(
            (candidate for candidate in games if game_id(candidate) == offer.get("game_id")),
            None,
        )
        if (
            not game
            or player_name(game, "white") != offer.get("bot")
            or player_name(game, "black") != bot_name
            or not offer_matches_game(offer, game)
            or not game_eligible(game, games, heartbeats, offers, offer)
        ):
            continue
        print(
            f"accepting offer for {game_id(game)} from {offer.get('bot')}",
            flush=True,
        )
        post_accept(
            auth,
            tournament_id,
            bot_name,
            str(offer.get("bot")),
            game_id(game),
        )
        outcome = start_game(auth, game_id(game))
        print(
            f"start response for {game_id(game)}: "
            f"ready={outcome.get('ready')} started={outcome.get('started')}",
            flush=True,
        )
        print(f"focusing on {game_id(game)}; leaving chat coordination", flush=True)
        return last_heartbeat_at, game_id(game), False

    if bot_unavailable(bot_name, games, heartbeats, offers):
        return last_heartbeat_at, None, False

    for game in eligible_games(games, heartbeats, offers):
        white, black = player_names(game)
        if white != bot_name:
            continue
        gid = game_id(game)
        print(f"offering {gid} to {black}", flush=True)
        post_offer(auth, tournament_id, bot_name, black, gid)
        outcome = start_game(auth, gid)
        print(
            f"start response for {gid}: "
            f"ready={outcome.get('ready')} started={outcome.get('started')}",
            flush=True,
        )
        print(f"focusing on {gid}; leaving chat coordination", flush=True)
        return last_heartbeat_at, gid, False

    return last_heartbeat_at, None, False


def run_coordinator(args: argparse.Namespace) -> int:
    auth = AuthSession(args.url, args.email, args.password)
    engine = UhpEngine(args.uhp, args.bot_name) if args.uhp else None
    if engine:
        engine.start()
    jitter = deterministic_jitter(args.bot_name)
    last_heartbeat_at: dt.datetime | None = None
    focused_game_id: str | None = None
    stopped_playing = False
    print(
        f"coordinating tournament {args.tournament_id} as {args.bot_name} "
        f"against {args.url.rstrip('/')} (poll {args.poll_seconds:g}s + {jitter:.2f}s jitter)",
        flush=True,
    )
    try:
        while True:
            try:
                last_heartbeat_at, focused_game_id, stopped_playing = run_once(
                    auth,
                    args.tournament_id,
                    args.bot_name,
                    last_heartbeat_at,
                    focused_game_id,
                    stopped_playing,
                    engine,
                )
            except ApiError as exc:
                print(f"warning: {exc}", file=sys.stderr, flush=True)
            time.sleep(max(0.5, args.poll_seconds + jitter))
    except KeyboardInterrupt:
        print("stopped", flush=True)
        return 0
    finally:
        if engine:
            engine.close()


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


def main() -> int:
    try:
        args = apply_config(parse_args())
        if args.command is None:
            print("error: command required: summary or run", file=sys.stderr)
            return 2
        if args.command == "run":
            return run_coordinator(args)

        auth = AuthSession(args.url, args.email, args.password)
        tournament = get_tournament(auth, args.tournament_id)
        print_summary(tournament, args.bot_name)
    except (ApiError, ConfigError, UhpError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
