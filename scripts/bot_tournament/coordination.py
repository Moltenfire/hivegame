# Coordinates chat heartbeats/offers and focused game play for one bot runner.
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import time
from typing import Any

from .api import (
    AuthSession,
    get_pending_games,
    get_tournament,
    get_tournament_chat,
    play_move,
    post_tournament_chat,
    start_game,
)
from .chat_protocol import (
    coord_message,
    coord_messages,
    fresh_offers,
    latest_heartbeats,
    self_heartbeat,
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
from .time_utils import utc_now

logger = logging.getLogger(__name__)


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


def deterministic_jitter(bot_name: str, max_seconds: float) -> float:
    digest = hashlib.sha256(bot_name.encode("utf-8")).digest()
    value = int.from_bytes(digest[:2], "big") / 65535
    return value * max_seconds


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
        post_tournament_chat(auth, tournament_id, message)
        return False
    if engine is not None:
        try:
            engine.record_accepted_move(gid, move, outcome.get("history"))
        except UhpError as exc:
            message = f"{bot_name} stopped playing {gid}: UHP engine error after {source} move: {exc}"
            logger.warning(message)
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
        logger.warning(message)
        post_tournament_chat(auth, tournament_id, message)
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
            logger.warning(message)
            post_tournament_chat(auth, tournament_id, message)
            return False

        return play_bot_move(auth, tournament_id, bot_name, gid, move, "UHP", engine)

    return True


def run_once(
    auth: AuthSession,
    tournament_config: TournamentConfig,
    bot_name: str,
    last_heartbeat_at: dt.datetime | None,
    focused_game_id: str | None,
    stopped_playing: bool,
    engine: UhpEngine | None,
) -> tuple[dt.datetime | None, str | None, bool, bool]:
    now = utc_now()
    tournament = get_tournament(auth, tournament_config.tournament_id)
    games = tournament_games(tournament)

    if not focused_game_id and not unfinished_games_for(games, bot_name):
        logger.info("%s has completed all tournament games; exiting", bot_name)
        return last_heartbeat_at, None, False, True

    if focused_game_id:
        focused_game = focused_tournament_game(tournament, focused_game_id)
        if not focused_game:
            logger.info("%s no longer found; returning to coordination", focused_game_id)
            if engine:
                engine.clear_game()
            if not unfinished_games_for(games, bot_name):
                logger.info("%s has completed all tournament games; exiting", bot_name)
                return last_heartbeat_at, None, False, True
            post_heartbeat(auth, tournament_config.tournament_id, bot_name, "idle", None)
            return utc_now(), None, False, False
        elif focused_game.get("finished", False):
            logger.info("%s finished; returning to coordination", focused_game_id)
            if engine:
                engine.clear_game()
            if not unfinished_games_for(games, bot_name):
                logger.info("%s has completed all tournament games; exiting", bot_name)
                return last_heartbeat_at, None, False, True
            post_heartbeat(auth, tournament_config.tournament_id, bot_name, "idle", None)
            return utc_now(), None, False, False
        elif stopped_playing:
            return last_heartbeat_at, focused_game_id, stopped_playing, False
        else:
            keep_playing = play_pending_opening_moves(
                auth,
                tournament,
                tournament_config.tournament_id,
                bot_name,
                focused_game_id,
                engine,
            )
            return last_heartbeat_at, focused_game_id, not keep_playing, False

    resumed_game = in_progress_game_for(games, bot_name)
    if resumed_game:
        resumed_game_id = game_id(resumed_game)
        logger.info("resuming %s; leaving chat coordination", resumed_game_id)
        keep_playing = play_pending_opening_moves(
            auth,
            tournament,
            tournament_config.tournament_id,
            bot_name,
            resumed_game_id,
            engine,
        )
        return last_heartbeat_at, resumed_game_id, not keep_playing, False

    chat = get_tournament_chat(auth, tournament_config.tournament_id)
    messages = coord_messages(chat, tournament_config.tournament_id, now)
    offers = fresh_offers(messages, tournament_config.offer_seconds, now)
    state, state_game_id = heartbeat_state(bot_name, games, offers)

    if (
        last_heartbeat_at is None
        or (now - last_heartbeat_at).total_seconds() >= tournament_config.heartbeat_seconds
    ):
        post_heartbeat(
            auth, tournament_config.tournament_id, bot_name, state, state_game_id
        )
        last_heartbeat_at = utc_now()
        messages.append(
            self_heartbeat(
                bot_name,
                tournament_config.tournament_id,
                state,
                state_game_id,
                last_heartbeat_at,
            )
        )

    heartbeats = latest_heartbeats(messages, tournament_config.online_seconds, utc_now())

    if state != "idle":
        return last_heartbeat_at, None, False, False

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
        logger.info("accepting offer for %s from %s", game_id(game), offer.get("bot"))
        post_accept(
            auth,
            tournament_config.tournament_id,
            bot_name,
            str(offer.get("bot")),
            game_id(game),
        )
        outcome = start_game(auth, game_id(game))
        logger.info(
            "start response for %s: ready=%s started=%s",
            game_id(game),
            outcome.get("ready"),
            outcome.get("started"),
        )
        logger.info("focusing on %s; leaving chat coordination", game_id(game))
        return last_heartbeat_at, game_id(game), False, False

    if bot_unavailable(bot_name, games, heartbeats, offers):
        return last_heartbeat_at, None, False, False

    for game in eligible_games(games, heartbeats, offers):
        white, black = player_names(game)
        if white != bot_name:
            continue
        gid = game_id(game)
        logger.info("offering %s to %s", gid, black)
        post_offer(auth, tournament_config.tournament_id, bot_name, black, gid)
        outcome = start_game(auth, gid)
        logger.info(
            "start response for %s: ready=%s started=%s",
            gid,
            outcome.get("ready"),
            outcome.get("started"),
        )
        logger.info("focusing on %s; leaving chat coordination", gid)
        return last_heartbeat_at, gid, False, False

    return last_heartbeat_at, None, False, False


def run_coordinator(
    tournament_config: TournamentConfig, bot_config: BotConfig, engine: UhpEngine | None
) -> int:
    auth = AuthSession(tournament_config.url, bot_config.email, bot_config.password)
    if engine:
        engine.start()
    jitter = deterministic_jitter(bot_config.name, tournament_config.poll_jitter_seconds)
    last_heartbeat_at: dt.datetime | None = None
    focused_game_id: str | None = None
    stopped_playing = False
    completed = False
    logger.info(
        "coordinating tournament %s as %s against %s (poll %gs + %.2fs jitter)",
        tournament_config.tournament_id,
        bot_config.name,
        tournament_config.url,
        tournament_config.poll_seconds,
        jitter,
    )
    try:
        while not completed:
            try:
                (
                    last_heartbeat_at,
                    focused_game_id,
                    stopped_playing,
                    completed,
                ) = run_once(
                    auth,
                    tournament_config,
                    bot_config.name,
                    last_heartbeat_at,
                    focused_game_id,
                    stopped_playing,
                    engine,
                )
            except ApiError as exc:
                logger.warning("%s", exc)
            if completed:
                break
            time.sleep(max(0.5, tournament_config.poll_seconds + jitter))
    except KeyboardInterrupt:
        logger.info("stopped")
        return 0
    finally:
        if engine:
            engine.close()
