# Unit tests for pure bot tournament coordinator logic.
from __future__ import annotations

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
    split_opening_link,
)
from bot_tournament.summary import print_summary
from bot_tournament.uhp import (
    bestmove_command_for_game,
    current_player_time_left_seconds,
    duration_seconds,
    game_string_for_opening,
    increment_seconds,
)


def game(gid: str, white: str, black: str, status: str = "NotStarted") -> dict:
    return {
        "game_id": gid,
        "game_status": status,
        "finished": False,
        "white_player": {"username": white, "uid": f"{white}-uid"},
        "black_player": {"username": black, "uid": f"{black}-uid"},
    }


class CoordinationTests(unittest.TestCase):
    def test_eligible_games_exclude_players_already_in_progress(self) -> None:
        games = [
            game("g2", "Bot3", "Bot4"),
            game("g1", "Bot1", "Bot2"),
            game("active", "Bot2", "Bot5", "InProgress"),
        ]
        self.assertEqual(
            [item["game_id"] for item in eligible_games(games)],
            ["g2"],
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
        ):
            result = run_once(
                Mock(),
                tournament_config,
                "Bot1",
                None,
                False,
                None,
                set(),
                None,
            )
        self.assertEqual(result, (None, False, None, True))

    def test_shutdown_controller_drains_then_forces_exit(self) -> None:
        shutdown = ShutdownController()
        shutdown.request()
        self.assertEqual(shutdown.mode, ShutdownMode.DRAINING)
        self.assertTrue(shutdown.event.is_set())
        shutdown.wait(0)
        self.assertFalse(shutdown.event.is_set())
        with self.assertRaises(KeyboardInterrupt):
            shutdown.request()
        self.assertEqual(shutdown.mode, ShutdownMode.FORCE_EXIT)

    def test_run_once_draining_exits_finished_focused_game(self) -> None:
        finished = game("g1", "Bot1", "Bot2", "InProgress")
        finished["finished"] = True
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        engine = Mock()
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": [finished]},
        ):
            result = run_once(
                Mock(),
                tournament_config,
                "Bot1",
                "g1",
                False,
                None,
                set(),
                engine,
                True,
            )
        self.assertEqual(result, (None, False, None, True))
        engine.clear_game.assert_called_once()

    def test_run_once_draining_idle_exits_without_reading_requests(self) -> None:
        pending = game("g1", "Bot1", "Bot2")
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": [pending]},
        ), patch("bot_tournament.coordination.get_game_requests") as requests:
            result = run_once(
                Mock(),
                tournament_config,
                "Bot1",
                None,
                False,
                None,
                set(),
                None,
                True,
            )
        self.assertEqual(result, (None, False, None, True))
        requests.assert_not_called()

    def test_white_opens_request_for_first_eligible_game(self) -> None:
        pending = game("g1", "Bot1", "Bot2")
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": [pending]},
        ), patch(
            "bot_tournament.coordination.get_game_requests", return_value=[]
        ), patch(
            "bot_tournament.coordination.start_game",
            return_value={"ready": True, "started": False},
        ) as start:
            result = run_once(
                Mock(), tournament_config, "Bot1", None, False, None, set(), None
            )
        self.assertEqual(result, (None, False, "g1", False))
        start.assert_called_once_with(unittest.mock.ANY, "g1")

    def test_black_accepts_incoming_request(self) -> None:
        pending = game("g1", "Bot1", "Bot2")
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        request = {
            "game_id": "g1",
            "proposer_username": "Bot1",
            "outgoing": False,
        }
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": [pending]},
        ), patch(
            "bot_tournament.coordination.get_game_requests", return_value=[request]
        ), patch(
            "bot_tournament.coordination.start_game",
            return_value={"ready": False, "started": True},
        ):
            result = run_once(
                Mock(), tournament_config, "Bot2", None, False, None, set(), None
            )
        self.assertEqual(result, ("g1", False, None, False))

    def test_restart_resumes_live_outgoing_request_without_renewing_it(self) -> None:
        pending = game("g1", "Bot1", "Bot2")
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        request = {
            "game_id": "g1",
            "proposer_username": "Bot1",
            "outgoing": True,
        }
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": [pending]},
        ), patch(
            "bot_tournament.coordination.get_game_requests", return_value=[request]
        ), patch("bot_tournament.coordination.start_game") as start:
            result = run_once(
                Mock(), tournament_config, "Bot1", None, False, None, set(), None
            )
        self.assertEqual(result, (None, False, "g1", False))
        start.assert_not_called()

    def test_expired_request_rotates_to_another_opponent(self) -> None:
        games = [game("g1", "Bot1", "Bot2"), game("g2", "Bot1", "Bot3")]
        tournament_config = TournamentConfig(url="http://localhost:3000", tournament_id="T")
        attempted: set[str] = set()
        with patch(
            "bot_tournament.coordination.get_tournament",
            return_value={"games": games},
        ), patch(
            "bot_tournament.coordination.get_game_requests", return_value=[]
        ), patch(
            "bot_tournament.coordination.start_game",
            return_value={"ready": True, "started": False},
        ) as start:
            result = run_once(
                Mock(), tournament_config, "Bot1", None, False, "g1", attempted, None
            )
        self.assertEqual(result, (None, False, "g2", False))
        self.assertEqual(attempted, {"g1"})
        start.assert_called_once_with(unittest.mock.ANY, "g2")


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

    def test_opening_link_unescapes_markdown_backslashes(self) -> None:
        opening = split_opening_link(
            r"[wL;bL wL\\;wM \\wL;bA1 bM\\](/analysis?uhp=x)"
        )
        self.assertEqual(opening.moves, "wL;bL wL\\;wM \\wL;bA1 bM\\")

    def test_opening_link_allows_escaped_closing_bracket_in_text(self) -> None:
        opening = split_opening_link(r"[wA1 \]bQ](/analysis?uhp=x)")
        self.assertEqual(opening.moves, r"wA1 ]bQ")

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

    def test_legacy_chat_timing_fields_are_ignored(self) -> None:
        tournament_path = self.write_json(
            '{"url":"http://localhost:3000","tournament_id":"T",'
            '"heartbeat_seconds":20,"online_seconds":45,"offer_seconds":30}'
        )
        tournament = load_tournament_config(tournament_path)
        self.assertEqual(tournament.tournament_id, "T")
        self.assertFalse(hasattr(tournament, "heartbeat_seconds"))

    def test_rejects_uhp_option_values_with_whitespace(self) -> None:
        bot_path = self.write_json(
            '{"name":"Bot1","email":"bot1@example.com","password":"pw",'
            '"uhp":{"command":"engine uhp","options":{"Bad":"has space"}}}'
        )
        with self.assertRaises(ConfigError):
            load_bot_config(bot_path)


class SummaryTests(unittest.TestCase):
    def test_summary_validates_assigned_openings_and_prints_moves(self) -> None:
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
        self.assertIn("- wS1;bS1 -wS1\n", stdout.getvalue())
        self.assertNotIn("https://example.test/a", stdout.getvalue())
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

    def test_bestmove_clock_uses_remaining_time_and_increment(self) -> None:
        game_state = {
            "current_player_id": "w",
            "white_id": "w",
            "white_time_left": 100_000_000_000,
            "time_increment": 5,
        }
        command = bestmove_command_for_game(
            game_state,
            {
                "mode": "time",
                "use_clock": True,
                "protocol": "clock",
            },
        )
        self.assertEqual(command, "bestmove clock 100 5")

    def test_bestmove_clock_uses_black_clock_when_black_to_move(self) -> None:
        game_state = {
            "current_player_id": "b",
            "white_id": "w",
            "black_id": "b",
            "white_time_left": 100_000_000_000,
            "black_time_left": 40_000_000_000,
        }
        command = bestmove_command_for_game(
            game_state,
            {
                "mode": "time",
                "use_clock": True,
                "protocol": "clock",
            },
        )
        self.assertEqual(command, "bestmove clock 40")

    def test_bestmove_clock_requires_current_player_clock(self) -> None:
        with self.assertRaises(UhpError):
            bestmove_command_for_game(
                {"current_player_id": "w", "white_id": "w"},
                {"mode": "time", "use_clock": True, "protocol": "clock"},
            )

    def test_bestmove_time_protocol_still_clamps_to_max_seconds(self) -> None:
        game_state = {
            "current_player_id": "w",
            "white_id": "w",
            "white_time_left": 1_000_000_000_000,
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
        self.assertEqual(command, "bestmove time 00:00:10")

    def test_bestmove_seconds_protocol_still_clamps_to_clock_fraction(self) -> None:
        game_state = {
            "current_player_id": "w",
            "white_id": "w",
            "white_time_left": 3_000_000_000,
        }
        command = bestmove_command_for_game(
            game_state,
            {
                "mode": "time",
                "use_clock": True,
                "moves_to_go": 1,
                "min_seconds": 1,
                "max_seconds": 10,
                "max_clock_fraction": 0.5,
                "protocol": "seconds",
            },
        )
        self.assertEqual(command, "bestmove seconds 1.5")

    def test_current_player_time_falls_back_to_ply_parity(self) -> None:
        self.assertEqual(
            current_player_time_left_seconds(
                {
                    "turn": 2,
                    "white_time_left": 30_000_000_000,
                    "black_time_left": 40_000_000_000,
                }
            ),
            30,
        )
        self.assertEqual(
            current_player_time_left_seconds(
                {
                    "turn": 3,
                    "white_time_left": 30_000_000_000,
                    "black_time_left": 40_000_000_000,
                }
            ),
            40,
        )

    def test_duration_seconds_accepts_structured_duration_json(self) -> None:
        self.assertEqual(duration_seconds({"secs": 2, "nanos": 500_000_000}), 2.5)
        self.assertEqual(
            duration_seconds({"seconds": 3, "nanoseconds": 250_000_000}), 3.25
        )

    def test_increment_seconds_accepts_seconds_or_duration_json(self) -> None:
        self.assertEqual(increment_seconds({"time_increment": 7}), 7)
        self.assertEqual(
            increment_seconds({"time_increment": {"secs": 1, "nanos": 500_000_000}}),
            1.5,
        )

    def test_game_string_for_opening_uses_turn_after_moves(self) -> None:
        self.assertEqual(
            game_string_for_opening("MLP", "wS1;bS1 -wS1;wQ wS1/"),
            "Base+MLP;InProgress;Black[2];wS1;bS1 -wS1;wQ wS1/",
        )


if __name__ == "__main__":
    unittest.main()
