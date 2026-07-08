# Unit tests for pure bot tournament coordinator logic.
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from bot_tournament.chat_protocol import coord_json, coord_message, latest_heartbeats
from bot_tournament.config import ConfigError, load_bot_config, load_tournament_config
from bot_tournament.coordination import eligible_games
from bot_tournament.openings import extract_openings, next_opening_move, opening_divergence
from bot_tournament.uhp import bestmove_command_for_game


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


class OpeningTests(unittest.TestCase):
    def test_extracts_openings_until_next_heading(self) -> None:
        description = "# Title\n\n## Openings\n- wS1;bS1 -wS1\n- wP;bP\n\n## Other\n- ignored"
        self.assertEqual(extract_openings(description), ["wS1;bS1 -wS1", "wP;bP"])

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


if __name__ == "__main__":
    unittest.main()
