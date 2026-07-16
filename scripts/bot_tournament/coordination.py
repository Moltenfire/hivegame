# Coordinates game requests and focused game play for one bot runner.
from __future__ import annotations

import hashlib
import logging
import signal
from dataclasses import dataclass, field
from enum import Enum
from threading import Event
from typing import Any

from .api import (
    AuthSession,
    get_game_requests,
    get_pending_games,
    get_tournament,
    play_move,
    start_game,
)
from .config import BotConfig, TournamentConfig
from .errors import ApiError, UhpError
from .game_data import (
    api_game_id,
    bot_color_and_id,
    color_to_move_from_history,
    focused_tournament_game,
    game_id,
    game_sort_key,
    player_in_game,
    player_name,
    player_names,
    tournament_games,
)
from .openings import (
    next_opening_move,
    opening_complete,
    opening_divergence,
    opening_map_for_tournament,
)
from .uhp import UhpEngine

logger = logging.getLogger(__name__)


class ShutdownMode(Enum):
    RUNNING = "running"
    DRAINING = "draining"
    FORCE_EXIT = "force_exit"


@dataclass
class ShutdownController:
    mode: ShutdownMode = ShutdownMode.RUNNING
    event: Event = field(default_factory=Event)

    @property
    def draining(self) -> bool:
        return self.mode == ShutdownMode.DRAINING

    def request(self) -> None:
        if self.mode == ShutdownMode.RUNNING:
            self.mode = ShutdownMode.DRAINING
            self.event.set()
            return
        self.mode = ShutdownMode.FORCE_EXIT
        self.event.set()
        raise KeyboardInterrupt

    def wait(self, seconds: float) -> None:
        self.event.wait(seconds)
        self.event.clear()


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


def unfinished_games_for(games: list[dict[str, Any]], bot_name: str) -> list[dict[str, Any]]:
    return sorted(
        [
            game
            for game in games
            if isinstance(game, dict)
            and not game.get("finished", False)
            and player_in_game(game, bot_name)
        ],
        key=game_sort_key,
    )


def game_eligible(game: dict[str, Any], games: list[dict[str, Any]]) -> bool:
    if game.get("finished", False) or game.get("game_status") != "NotStarted":
        return False

    white, black = player_names(game)
    if not white or not black:
        return False
    for player in (white, black):
        if in_progress_game_for(games, player):
            return False
    return True


def eligible_games(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        [
            game
            for game in games
            if isinstance(game, dict) and game_eligible(game, games)
        ],
        key=game_sort_key,
    )


def deterministic_jitter(bot_name: str, max_seconds: float) -> float:
    digest = hashlib.sha256(bot_name.encode("utf-8")).digest()
    value = int.from_bytes(digest[:2], "big") / 65535
    return value * max_seconds


def request_game_id(request: dict[str, Any]) -> str:
    value = request.get("game_id")
    if isinstance(value, dict):
        value = value.get("0")
    return str(value) if value else ""


def play_bot_move(
    auth: AuthSession,
    tournament_id: str,
    bot_name: str,
    gid: str,
    move: str,
    source: str,
    engine: UhpEngine | None,
) -> bool:
    logger.info("playing %s move for %s: %s", source, gid, move)
    try:
        outcome = play_move(auth, gid, move)
    except ApiError as exc:
        message = f"{bot_name} stopped playing {gid}: {source} move rejected: {exc}"
        logger.warning(message)
        return False
    if engine is not None:
        try:
            engine.record_accepted_move(gid, move, outcome.get("history"))
        except UhpError as exc:
            message = f"{bot_name} stopped playing {gid}: UHP engine error after {source} move: {exc}"
            logger.warning(message)
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
        logger.warning(message)
        return False
    bot_color, _bot_user_id = bot_identity

    for game in get_pending_games(auth):
        gid = api_game_id(game)
        if gid != focused_game_id:
            continue
        color_to_move = color_to_move_from_history(game)
        if color_to_move != bot_color:
            logger.info(
                "skipping %s: ply says %s to move, not %s (%s)",
                gid,
                color_to_move,
                bot_name,
                bot_color,
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
                logger.warning(message)
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
            logger.warning(message)
            return False

        return play_bot_move(auth, tournament_id, bot_name, gid, move, "UHP", engine)

    return True


def run_once(
    auth: AuthSession,
    tournament_config: TournamentConfig,
    bot_name: str,
    focused_game_id: str | None,
    stopped_playing: bool,
    requested_game_id: str | None,
    attempted_game_ids: set[str],
    engine: UhpEngine | None,
    draining: bool = False,
) -> tuple[str | None, bool, str | None, bool]:
    tournament = get_tournament(auth, tournament_config.tournament_id)
    games = tournament_games(tournament)

    if not focused_game_id and not unfinished_games_for(games, bot_name):
        logger.info("%s has completed all tournament games; exiting", bot_name)
        return None, False, None, True

    if focused_game_id:
        focused_game = focused_tournament_game(tournament, focused_game_id)
        if not focused_game:
            if engine:
                engine.clear_game()
            if draining:
                logger.info("%s no longer found; exiting after shutdown request", focused_game_id)
                return None, False, None, True
            logger.info("%s no longer found; returning to coordination", focused_game_id)
            if not unfinished_games_for(games, bot_name):
                logger.info("%s has completed all tournament games; exiting", bot_name)
                return None, False, None, True
            return None, False, None, False
        elif focused_game.get("finished", False):
            if engine:
                engine.clear_game()
            if draining:
                logger.info("%s finished; exiting after shutdown request", focused_game_id)
                return None, False, None, True
            logger.info("%s finished; returning to coordination", focused_game_id)
            if not unfinished_games_for(games, bot_name):
                logger.info("%s has completed all tournament games; exiting", bot_name)
                return None, False, None, True
            return None, False, None, False
        elif stopped_playing:
            if draining:
                logger.info(
                    "shutdown requested but %s is no longer being played; exiting",
                    focused_game_id,
                )
                return None, stopped_playing, None, True
            return focused_game_id, stopped_playing, None, False
        else:
            keep_playing = play_pending_opening_moves(
                auth,
                tournament,
                tournament_config.tournament_id,
                bot_name,
                focused_game_id,
                engine,
            )
            return focused_game_id, not keep_playing, None, False

    resumed_game = in_progress_game_for(games, bot_name)
    if resumed_game:
        resumed_game_id = game_id(resumed_game)
        logger.info("resuming %s; leaving game-request coordination", resumed_game_id)
        keep_playing = play_pending_opening_moves(
            auth,
            tournament,
            tournament_config.tournament_id,
            bot_name,
            resumed_game_id,
            engine,
        )
        return resumed_game_id, not keep_playing, None, False

    if draining:
        logger.info("shutdown requested while idle; exiting")
        return None, False, None, True

    requests = get_game_requests(auth)

    if not requested_game_id:
        outgoing = next(
            (request for request in requests if request.get("outgoing") is True),
            None,
        )
        if outgoing:
            requested_game_id = request_game_id(outgoing)
            logger.info("resuming pending request for %s", requested_game_id)

    if requested_game_id:
        outgoing_is_live = any(
            request.get("outgoing") is True
            and request_game_id(request) == requested_game_id
            for request in requests
        )
        if outgoing_is_live:
            return None, False, requested_game_id, False
        logger.info("request for %s expired; trying another opponent", requested_game_id)
        attempted_game_ids.add(requested_game_id)
        requested_game_id = None

    incoming = sorted(
        [request for request in requests if request.get("outgoing") is False],
        key=lambda request: next(
            (
                game_sort_key(game)
                for game in games
                if game_id(game) == request_game_id(request)
            ),
            ((), (), request_game_id(request)),
        ),
    )
    for request in incoming:
        requested_id = request_game_id(request)
        game = next(
            (candidate for candidate in games if game_id(candidate) == requested_id),
            None,
        )
        if (
            not game
            or player_name(game, "white") != request.get("proposer_username")
            or player_name(game, "black") != bot_name
            or not game_eligible(game, games)
        ):
            continue
        logger.info(
            "accepting request for %s from %s",
            game_id(game),
            request.get("proposer_username"),
        )
        outcome = start_game(auth, game_id(game))
        logger.info(
            "start response for %s: ready=%s started=%s",
            game_id(game),
            outcome.get("ready"),
            outcome.get("started"),
        )
        if outcome.get("started"):
            logger.info("focusing on %s; leaving game-request coordination", game_id(game))
            return game_id(game), False, None, False

    candidates = []
    for game in eligible_games(games):
        white, black = player_names(game)
        gid = game_id(game)
        if white != bot_name or gid in attempted_game_ids:
            continue
        candidates.append((game, black))

    if not candidates:
        if attempted_game_ids:
            logger.info("all available opponents tried; starting a new request cycle")
            attempted_game_ids.clear()
        return None, False, None, False

    for game, opponent in candidates:
        gid = game_id(game)
        logger.info("requesting %s against %s", gid, opponent)
        try:
            outcome = start_game(auth, gid)
        except ApiError as exc:
            logger.info("could not request %s: %s", gid, exc)
            attempted_game_ids.add(gid)
            continue
        logger.info(
            "start response for %s: ready=%s started=%s",
            gid,
            outcome.get("ready"),
            outcome.get("started"),
        )
        if outcome.get("started"):
            logger.info("focusing on %s; leaving game-request coordination", gid)
            return gid, False, None, False
        if outcome.get("ready"):
            return None, False, gid, False
        attempted_game_ids.add(gid)

    return None, False, None, False


def run_coordinator(
    tournament_config: TournamentConfig, bot_config: BotConfig, engine: UhpEngine | None
) -> int:
    auth = AuthSession(tournament_config.url, bot_config.email, bot_config.password)
    shutdown = ShutdownController()

    def handle_sigint(_signum: int, _frame: Any) -> None:
        shutdown.request()

    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handle_sigint)
    jitter = deterministic_jitter(bot_config.name, tournament_config.poll_jitter_seconds)
    focused_game_id: str | None = None
    stopped_playing = False
    requested_game_id: str | None = None
    attempted_game_ids: set[str] = set()
    completed = False
    announced_drain = False
    logger.info(
        "coordinating tournament %s as %s against %s (poll %gs + %.2fs jitter)",
        tournament_config.tournament_id,
        bot_config.name,
        tournament_config.url,
        tournament_config.poll_seconds,
        jitter,
    )
    try:
        if engine:
            engine.start()
        while not completed:
            try:
                (
                    focused_game_id,
                    stopped_playing,
                    requested_game_id,
                    completed,
                ) = run_once(
                    auth,
                    tournament_config,
                    bot_config.name,
                    focused_game_id,
                    stopped_playing,
                    requested_game_id,
                    attempted_game_ids,
                    engine,
                    shutdown.draining,
                )
            except ApiError as exc:
                logger.warning("%s", exc)
            if completed:
                break
            if shutdown.draining and not focused_game_id:
                logger.info("shutdown requested while idle; exiting")
                break
            if shutdown.draining and stopped_playing:
                logger.info("shutdown requested but current game is stopped; exiting")
                break
            if shutdown.draining and not announced_drain:
                logger.info(
                    "shutdown requested; finishing %s then exiting",
                    focused_game_id,
                )
                announced_drain = True
            shutdown.wait(max(0.5, tournament_config.poll_seconds + jitter))
    except KeyboardInterrupt:
        logger.info("second shutdown request received; exiting immediately")
        return 0
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        if engine:
            engine.close()
    return 0
