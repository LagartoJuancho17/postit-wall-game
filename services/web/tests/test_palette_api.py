"""Gate tests: picked colors, their persistence, and the live preview."""

from __future__ import annotations

import json
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path

from contracts import ROLES
from services.vision import DEFAULT_PALETTE
from services.web.backend.eval import _frame, sampled_hex
from services.web.backend.server import GameController, RequestError

RECOLORED = {"start": 105, "goal": 176, "bumper": 28, "marker": 140}


class PaletteActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.palette_path = self.root / "palette.json"
        self.log_path = self.root / "events.jsonl"
        self.game = GameController(log_path=self.log_path, palette_path=self.palette_path)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def events(self) -> list[dict]:
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]

    def test_snapshot_starts_with_the_default_palette(self) -> None:
        palette = self.game.snapshot()["palette"]
        self.assertFalse(palette["custom"])
        self.assertEqual(set(palette["roles"]), set(ROLES))
        for role in ROLES:
            expected = DEFAULT_PALETTE.rule(role)
            self.assertEqual(palette["roles"][role]["hex"], expected.hex)
            self.assertEqual(palette["roles"][role]["hue_low"], expected.hue_low)
            self.assertFalse(palette["roles"][role]["custom"])

    def test_picked_color_is_applied_saved_logged_and_restored(self) -> None:
        state = self.game.action({"action": "palette_set", "role": "start", "hex": "#3A6DF0"})
        start = state["palette"]["roles"]["start"]
        self.assertEqual(start["hex"], "#3a6df0")
        self.assertTrue(start["custom"])
        self.assertTrue(state["palette"]["custom"])
        self.assertEqual(state["message"], "Color de inicio actualizado.")
        saved = json.loads(self.palette_path.read_text())
        self.assertEqual(saved["choices"]["start"], "#3a6df0")
        self.assertIsNone(saved["choices"]["goal"])
        self.assertEqual(self.events()[-1]["event"], "palette_changed")

        restarted = GameController(log_path=None, palette_path=self.palette_path)
        self.assertEqual(restarted.snapshot()["palette"], state["palette"])

    def test_refused_colors_leave_everything_unchanged(self) -> None:
        before = self.game.snapshot()
        for payload, words in (
            ({"role": "start", "hex": "blue"}, "#rrggbb"),
            ({"role": "start", "hex": "#c8c4c6"}, "casi gris"),
            ({"role": "start", "hex": DEFAULT_PALETTE.goal.hex}, "llegada"),
            ({"role": "ball", "hex": "#ff0000"}, "desconocido"),
            ({"role": "start"}, "#rrggbb"),
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(RequestError) as caught:
                    self.game.action({"action": "palette_set", **payload})
                self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
                self.assertIn(words, str(caught.exception))
        self.assertEqual(self.game.snapshot(), before)
        self.assertFalse(self.palette_path.exists())

    def test_refusal_names_the_role_being_changed(self) -> None:
        self.game.action({"action": "palette_set", "role": "start", "hex": "#3a6df0"})
        with self.assertRaises(RequestError) as caught:
            self.game.action({"action": "palette_set", "role": "bumper", "hex": "#3d6ef2"})
        self.assertTrue(str(caught.exception).startswith("El color de rebotes"), str(caught.exception))

    def test_reset_restores_defaults_and_overwrites_the_file(self) -> None:
        self.game.action({"action": "palette_set", "role": "marker", "hex": "#e0607a"})
        state = self.game.action({"action": "palette_reset"})
        self.assertFalse(state["palette"]["custom"])
        self.assertEqual(state["palette"]["roles"]["marker"]["hex"], DEFAULT_PALETTE.marker.hex)
        self.assertEqual(json.loads(self.palette_path.read_text())["choices"],
                         {role: None for role in ROLES})

    def test_damaged_palette_file_falls_back_to_defaults(self) -> None:
        for content in ("not json", '{"choices": {"start": "blue"}}',
                        '{"choices": {"ball": "#ff0000"}}', '{"choices": []}', "{}"):
            with self.subTest(content=content):
                self.palette_path.write_text(content)
                game = GameController(log_path=self.log_path, palette_path=self.palette_path)
                self.assertFalse(game.snapshot()["palette"]["custom"])
                self.assertEqual(self.events()[-1]["event"], "palette_ignored")

    def test_palette_change_clears_ready_board_until_next_live_preview(self) -> None:
        self.game.action({"action": "projection_open", "width": 1280, "height": 800})
        self.game.scan_jpeg(_frame(430))
        state = self.game.action({"action": "palette_set", "role": "bumper", "hex": "#2ec4b6"})
        self.assertEqual(state["message"], "Color de rebotes actualizado.")
        self.assertIsNone(state["board"])
        self.assertIsNone(state["live_detection"])


class PreviewTests(unittest.TestCase):
    def test_preview_counts_every_role_and_publishes_the_live_board(self) -> None:
        game = GameController(log_path=None)
        preview = game.preview_jpeg(_frame(430))
        self.assertEqual((preview["width"], preview["height"]), (960, 720))
        counts = {role: preview["roles"][role]["count"] for role in ROLES}
        self.assertEqual(counts, {"start": 1, "goal": 1, "bumper": 1, "marker": 4})
        self.assertIsNone(preview["roles"]["marker"]["expected"])
        self.assertIsNone(preview["roles"]["bumper"]["expected"])
        self.assertEqual(len(preview["roles"]["start"]["notes"][0]["points"]), 4)
        self.assertEqual(game.snapshot()["live_detection"], preview)
        self.assertEqual(game.snapshot()["phase"], "ready")

    def test_preview_rejects_data_that_is_not_a_jpeg(self) -> None:
        with self.assertRaises(RequestError):
            GameController(log_path=None).preview_jpeg(b"not a jpeg")

    def test_picking_colors_from_the_camera_makes_a_recolored_wall_playable(self) -> None:
        game = GameController(log_path=None)
        game.action({"action": "projection_open", "width": 1280, "height": 800})
        frame = _frame(430, RECOLORED)
        # Default colors misread this wall: the yellow bumper looks like a
        # start and the red goal like a fifth corner.
        misread = {role: data["count"] for role, data in game.preview_jpeg(frame)["roles"].items()}
        self.assertEqual(misread, {"start": 1, "goal": 0, "bumper": 0, "marker": 5})
        with self.assertRaises(RequestError) as caught:
            game.scan_jpeg(frame)
        self.assertIn("llegada", str(caught.exception))

        for role in ("marker", "start", "goal", "bumper"):
            game.action({"action": "palette_set", "role": role, "hex": sampled_hex(frame, role)})
        counts = {role: data["count"] for role, data in game.preview_jpeg(frame)["roles"].items()}
        self.assertEqual(counts, {"start": 1, "goal": 1, "bumper": 1, "marker": 4})
        self.assertEqual(game.scan_jpeg(frame)["phase"], "ready")


if __name__ == "__main__":
    unittest.main()
