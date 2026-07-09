# Unit tests for pure bot tournament coordinator logic.
from __future__ import annotations

import datetime as dt
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

SCRIPT_DIR = os.path.dirname(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from bot_tournament.chat_protocol import coord_json, coord_message, latest_heartbeats
from bot_tournament.config import (
    ConfigError,
    TournamentConfig,
    load_bot_config,
    load_tournament_config,
)
from bot_tournament.coordination import (
    ShutdownController,
    ShutdownMode,
    eligible_games,
    run_once,
    unfinished_games_for,
)
from bot_tournament.errors import UhpError
from bot_tournament.openings import (
    extract_opening_entries,
    extract_openings,
    next_opening_move,
    opening_divergence,
)
from bot_tournament.summary import print_summary
from bot_tournament.uhp import bestmove_command_for_game, game_string_for_opening


def game(gid: str, white: str, black: str, status: str = "NotStarted") -> dict:
    return {
        "game_id": gid,
        "game_status": status,
        "finished": False,
        "white_player": {"username": white, "uid": f"{white}-uid"},
        "black_player": {"username": black, "uid": f"{black}-uid"},
    }


class ChatProtocolTests(unittest.TestCase):
    def test_rejects_payload_when_chat_sender_does_not_match_bot(self) -> None:
        now = dt.datetime(2026, 7, 8, tzinfo=dt.UTC)
        text = coord_message(
            {
                "v": 1,
                "type": "heartbeat",
                "bot": "Bot2",
                "tournament_id": "T",
                "state": "idle",
                "game_id": None,
            }
        )
        message = {
            "message": {
                "username": "Bot1",
                "message": text,
                "timestamp": "2026-07-08T10:00:00+00:00",
            }
        }
        self.assertIsNone(coord_json(message, now))

    def test_latest_heartbeats_keeps_newest_online_message(self) -> None:
        now = dt.datetime(2026, 7, 8, 10, 1, tzinfo=dt.UTC)
        messages = [
            {"type": "heartbeat", "bot": "Bot1", "_timestamp": now - dt.timedelta(seconds=10)},
            {"type": "heartbeat", "bot": "Bot1", "_timestamp": now - dt.timedelta(seconds=5)},
            {"type": "heartbeat", "bot": "Bot2", "_timestamp": now - dt.timedelta(seconds=50)},
        ]
        heartbeats = latest_heartbeats(messages, 45, now)
        self.assertEqual(heartbeats["Bot1"]["_timestamp"], now - dt.timedelta(seconds=5))
        self.assertNotIn("Bot2", heartbeats)


class CoordinationTests(unittest.TestCase):
    def test_eligible_games_require_online_idle_players(self) -> None:
        games = [game("g2", "Bot3", "Bot4"), game("g1", "Bot1", "Bot2")]
        now = dt.datetime(2026, 7, 8, tzinfo=dt.UTC)
        heartbeats = {
            name: {"state": "idle", "_timestamp": now}
            for name in ("Bot1", "Bot2", "Bot3")
        }
        self.assertEqual(
            [item["game_id"] for item in eligible_games(games, heartbeats, [])],
            ["g1"],
        )

    def test_unfinished_games_for_only_counts_assigned_unfinished_games(self) -> None:
        finished = game("g1", "Bot1", "Bot2")
        finished["finished"] = True
        unfinished = game("g2", "Bot1", "Bot3")
        other = game("g3", "Bot4", "Bot5")
        self.assertEqual(
            [
                item["game_id"]
                for item in unfinished_games_for([finished, unfinished, other], "Bot1")
            ],
            ["g2"],
        )

    def test_run_once_exits_when_all_assigned_games_are_finished(self) -> None:
        finished = game("g1", "Bot1", "Bot2")
        finished["finished"] = True
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": [finished]},
        ), patch("bot_tournament.coordination.get_tournament_chat") as chat:
            result = run_once(
                Mock(),
                tournament_config,
                "Bot1",
                None,
                None,
                False,
                None,
            )
        self.assertEqual(result, (None, None, False, True))
        chat.assert_not_called()

    def test_shutdown_controller_drains_then_forces_exit(self) -> None:
        shutdown = ShutdownController()
        shutdown.request()
        self.assertEqual(shutdown.mode, ShutdownMode.DRAINING)
        self.assertTrue(shutdown.event.is_set())
        with self.assertRaises(KeyboardInterrupt):
            shutdown.request()
        self.assertEqual(shutdown.mode, ShutdownMode.FORCE_EXIT)

    def test_run_once_draining_exits_finished_focused_game_without_heartbeat(self) -> None:
        finished = game("g1", "Bot1", "Bot2", "InProgress")
        finished["finished"] = True
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        engine = Mock()
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": [finished]},
        ), patch("bot_tournament.coordination.post_heartbeat") as heartbeat:
            result = run_once(
                Mock(),
                tournament_config,
                "Bot1",
                None,
                "g1",
                False,
                engine,
                True,
            )
        self.assertEqual(result, (None, None, False, True))
        engine.clear_game.assert_called_once()
        heartbeat.assert_not_called()

    def test_run_once_draining_idle_exits_without_reading_chat(self) -> None:
        pending = game("g1", "Bot1", "Bot2")
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": [pending]},
        ), patch("bot_tournament.coordination.get_tournament_chat") as chat:
            result = run_once(
                Mock(),
                tournament_config,
                "Bot1",
                None,
                None,
                False,
                None,
                True,
            )
        self.assertEqual(result, (None, None, False, True))
        chat.assert_not_called()


class OpeningTests(unittest.TestCase):
    def test_extracts_openings_until_next_heading(self) -> None:
        description = "# Title\n\n## Openings\n- wS1;bS1 -wS1\n- wP;bP\n\n## Other\n- ignored"
        self.assertEqual(extract_openings(description), ["wS1;bS1 -wS1", "wP;bP"])

    def test_extracts_opening_links_after_moves(self) -> None:
        description = (
            "## Openings\n"
            "- [wS1;bS1 -wS1](https://example.test/a)\n"
            "- [wP;bP](/analysis?uhp=Base%3BwP%3BbP)\n"
        )
        openings = extract_opening_entries(description)
        self.assertEqual([opening.moves for opening in openings], ["wS1;bS1 -wS1", "wP;bP"])
        self.assertEqual(
            [opening.link for opening in openings],
            ["https://example.test/a", "/analysis?uhp=Base%3BwP%3BbP"],
        )

    def test_detects_next_move_and_divergence(self) -> None:
        pending = {"history": "wS1"}
        self.assertEqual(next_opening_move(pending, "wS1;bS1 -wS1"), "bS1 -wS1")
        diverged = {"history": "wS1;bG1 -wS1"}
        self.assertEqual(opening_divergence(diverged, "wS1;bS1 -wS1"), (1, "bS1 -wS1", "bG1 -wS1"))


class ConfigTests(unittest.TestCase):
    def write_json(self, content: str) -> str:
        handle = tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8")
        self.addCleanup(lambda: os.path.exists(handle.name) and os.unlink(handle.name))
        with handle:
            handle.write(content)
        return handle.name

    def test_loads_split_configs(self) -> None:
        tournament_path = self.write_json('{"url":"http://localhost:3000","tournament_id":"T"}')
        bot_path = self.write_json(
            '{"name":"Bot1","email":"bot1@example.com","password":"pw",'
            '"logging":{"directory":"logs"},'
            '"uhp":{"command":"engine uhp","options":{"NumThreads":1}}}'
        )
        self.assertEqual(load_tournament_config(tournament_path).tournament_id, "T")
        bot = load_bot_config(bot_path)
        self.assertEqual(bot.name, "Bot1")
        self.assertEqual(bot.uhp.options["NumThreads"], 1)

    def test_rejects_uhp_option_values_with_whitespace(self) -> None:
        bot_path = self.write_json(
            '{"name":"Bot1","email":"bot1@example.com","password":"pw",'
            '"uhp":{"command":"engine uhp","options":{"Bad":"has space"}}}'
        )
        with self.assertRaises(ConfigError):
            load_bot_config(bot_path)


class SummaryTests(unittest.TestCase):
    def test_summary_validates_assigned_openings_and_prints_links(self) -> None:
        engine = Mock()
        tournament = {
            "name": "T",
            "tournament_id": "tid",
            "description": "## Openings\n- [wS1;bS1 -wS1](https://example.test/a)",
            "games": [game("g1", "Bot1", "Bot2")],
        }
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            print_summary(tournament, "Bot1", engine)
        engine.validate_opening.assert_called_once_with(None, "wS1;bS1 -wS1")
        self.assertIn("[wS1;bS1 -wS1](https://example.test/a)", stdout.getvalue())
        self.assertNotIn("ok", stdout.getvalue())
        self.assertNotIn("not validated", stdout.getvalue())

    def test_summary_reports_invalid_opening_in_openings_section(self) -> None:
        engine = Mock()
        engine.validate_opening.side_effect = UhpError("invalidmove bad")
        tournament = {
            "description": "## Openings\n- wS1;bS1 -wS1",
            "games": [game("g1", "Bot1", "Bot2")],
        }
        stdout = io.StringIO()
        with self.assertRaises(UhpError):
            with redirect_stdout(stdout):
                print_summary(tournament, "Bot1", engine)
        self.assertIn("- wS1;bS1 -wS1 [invalid: invalidmove bad]", stdout.getvalue())


class UhpTests(unittest.TestCase):
    def test_bestmove_depth_command(self) -> None:
        self.assertEqual(bestmove_command_for_game({}, {"mode": "depth", "depth": 2}), "bestmove depth 2")

    def test_bestmove_time_uses_clock_budget(self) -> None:
        game_state = {
            "current_player_id": "w",
            "white_id": "w",
            "white_time_left": 100_000_000_000,
        }
        command = bestmove_command_for_game(
            game_state,
            {
                "mode": "time",
                "use_clock": True,
                "moves_to_go": 20,
                "min_seconds": 1,
                "max_seconds": 10,
                "max_clock_fraction": 0.5,
                "protocol": "time",
            },
        )
        self.assertEqual(command, "bestmove time 00:00:05")

    def test_game_string_for_opening_uses_turn_after_moves(self) -> None:
        self.assertEqual(
            game_string_for_opening("MLP", "wS1;bS1 -wS1;wQ wS1/"),
            "Base+MLP;InProgress;Black[2];wS1;bS1 -wS1;wQ wS1/",
        )


if __name__ == "__main__":
    unittest.main()
