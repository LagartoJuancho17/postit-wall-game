"""Gate tests: levels in the game (contract 3.2) and the mark colors (2.6)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from services.levels import LEVELS
from services.web.backend.eval import FRAME_HUES, _color
from services.web.backend.server import (LEVEL_ADVANCE_S, ZONE_DEFAULTS, GameController, RequestError,
                                         create_server, zone_conflict)

LABEL = "Cámara de iPhone de Tobias"


def wall(start, goal, bumpers=()) -> bytes:
    """A 960 x 720 wall with notes at these centers (camera px = the unaligned game's px)."""
    image = np.full((720, 960, 3), 220, dtype=np.uint8)
    for x, y in ((90, 90), (870, 90), (870, 630), (90, 630)):
        cv2.rectangle(image, (x - 22, y - 22), (x + 22, y + 22), _color(FRAME_HUES["marker"]), -1)
    for role, (x, y) in (("start", start), ("goal", goal), *(("bumper", b) for b in bumpers)):
        cv2.rectangle(image, (x - 30, y - 30), (x + 30, y + 30), _color(FRAME_HUES[role]), -1)
    okay, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert okay
    return encoded.tobytes()


LEVEL1 = wall((480, 108), (480, 576))                     # both in level 1's boxes; the drop wins
LEVEL1_BUMPER = wall((480, 108), (480, 576), ((300, 330),))
OUTSIDE = wall((180, 350), (480, 576))
SPIKES = wall((441, 108), (749, 590))                     # level 4 without its bumper


class Game:
    def __init__(self, **kwargs):
        self.now = 0.0
        self.game = GameController(log_path=None, clock=lambda: self.now, **kwargs)
        self.game.action({"action": "projection_open", "width": 1280, "height": 800})

    def see(self, frame=LEVEL1, seconds=0.25):
        self.now += seconds
        self.game.tick()
        return self.game.preview_jpeg(frame, LABEL)

    def act(self, name, **data):
        return self.game.action({"action": name, **data})

    def run(self, seconds, frame=LEVEL1):
        state = None
        for _ in range(int(seconds / 0.25)):
            self.see(frame)
            state = self.game.tick()
        return state


class LevelFlowTests(unittest.TestCase):
    def test_a_new_controller_plays_free_and_lists_every_level(self):
        state = GameController(log_path=None).snapshot()
        self.assertEqual(state["level"]["id"], "libre")
        self.assertEqual([entry["id"] for entry in state["level"]["list"]], [level.id for level in LEVELS])
        self.assertIsNone(state["level"]["geometry"], "no space yet: no camera and no alignment")

    def test_the_start_must_be_in_its_box_and_the_level_says_so(self):
        game = Game(level="1")
        game.see(OUTSIDE)
        state = game.game.snapshot()
        self.assertEqual(state["phase"], "waiting_camera")
        self.assertEqual(state["level"]["problem"], "Poné el post-it de inicio dentro de su recuadro.")
        self.assertEqual(state["level"]["check"]["start"], False)
        self.assertEqual(state["level"]["geometry"]["space"], "camera")
        self.assertEqual(state["level"]["geometry"]["start_zone"]["points"][0], {"x": 422.4, "y": 43.2})
        with self.assertRaises(RequestError):
            game.act("play")
        game.see(LEVEL1)
        state = game.game.snapshot()
        self.assertEqual((state["phase"], state["level"]["problem"]), ("ready", None))

    def test_too_many_bumpers_refuse_the_board(self):
        game = Game(level="1")
        game.see(LEVEL1_BUMPER)
        self.assertEqual(game.game.snapshot()["level"]["problem"], "Este nivel se juega sin rebotes: sacá 1.")
        self.assertEqual(game.game.snapshot()["phase"], "waiting_camera")

    def test_a_won_level_counts_and_the_next_follows_after_its_party(self):
        with tempfile.TemporaryDirectory() as folder:
            progress = Path(folder) / "progress.json"
            game = Game(level="1", progress_path=progress)
            game.see()
            game.act("play")
            state = game.run(4.0)
            self.assertEqual((state["phase"], state["game"]["ended_by"]), ("won", "goal"))
            self.assertEqual(state["level"]["done"], ["1"])
            self.assertEqual(state["message"], "¡Nivel 1 superado! Sigue el nivel 2: Un rebote.")
            self.assertGreater(state["level"]["advance_s"], 0)
            state = game.run(LEVEL_ADVANCE_S)
            self.assertEqual(state["level"]["id"], "2")
            self.assertEqual(state["level"]["problem"], "Poné el post-it de inicio dentro de su recuadro.")
            self.assertEqual(json.loads(progress.read_text()), {"version": 1, "level": "2", "done": ["1"]})
            again = GameController(log_path=None, progress_path=progress)
            self.assertEqual((again.level.id, again.levels_done), ("2", ["1"]))

    def test_playing_again_after_a_win_replays_the_level(self):
        game = Game(level="1")
        game.see()
        game.act("play")
        game.run(4.0)
        game.act("play")
        self.assertEqual(game.game.snapshot()["level"]["advance_s"], 0.0)
        self.assertEqual(game.run(LEVEL_ADVANCE_S + 0.5)["level"]["id"], "1")

    def test_spikes_end_the_attempt_with_their_own_words(self):
        game = Game(level="4")
        game.see(SPIKES)
        game.act("play")
        state = game.run(4.0, SPIKES)
        self.assertEqual((state["phase"], state["game"]["ended_by"]), ("lost", "spikes"))
        self.assertEqual(state["message"], "La pelota tocó los pinchos.")
        self.assertEqual(state["level"]["done"], [])

    def test_the_last_level_has_nothing_after(self):
        game = Game(level="1")
        game.game.level = LEVELS[-1]
        game.game._level_won_unlocked()
        state = game.game.snapshot()
        self.assertEqual(state["message"], "¡Completaste todos los niveles!")
        self.assertIsNone(state["level"]["next"])
        self.assertEqual(state["level"]["advance_s"], 0.0)

    def test_choosing_a_level_is_refused_mid_attempt_and_for_unknown_ids(self):
        game = Game(level="1")
        game.see()
        with self.assertRaisesRegex(RequestError, "Nivel desconocido"):
            game.act("level", id="99")
        game.act("play")
        with self.assertRaisesRegex(RequestError, "esperá la cuenta"):
            game.act("level", id="2")
        game.run(2.6)
        with self.assertRaisesRegex(RequestError, "Terminá o reiniciá"):
            game.act("level", id="2")

    def test_the_free_level_plays_any_board(self):
        game = Game(level="1")
        game.see(OUTSIDE)
        state = game.act("level", id="libre")
        self.assertEqual(state["message"], "Modo libre. Sin recuadros ni límites: armá tu propio tablero.")
        self.assertIsNone(state["level"]["problem"])
        self.assertEqual(state["phase"], "ready")

    def test_a_damaged_progress_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            progress = Path(folder) / "progress.json"
            for text in ("{", json.dumps({"level": "99", "done": []}), json.dumps({"level": "2"})):
                progress.write_text(text)
                self.assertEqual(GameController(log_path=None, progress_path=progress, level="1").level.id, "1")

    def test_the_server_refuses_an_unknown_starting_level(self):
        with self.assertRaisesRegex(ValueError, "no hay nivel"):
            create_server(bind=False, level="99")


class MarkColorTests(unittest.TestCase):
    def test_a_mark_color_is_kept_apart_from_the_detection_color(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "palette.json"
            game = GameController(log_path=None, palette_path=path)
            hex_before = game.snapshot()["palette"]["roles"]["goal"]["hex"]
            state = game.action({"action": "mark_set", "role": "goal", "hex": " #00FFAA "})
            goal = state["palette"]["roles"]["goal"]
            self.assertEqual((goal["mark"], goal["mark_custom"], goal["hex"]), ("#00ffaa", True, hex_before))
            self.assertTrue(state["palette"]["custom"])
            self.assertEqual(json.loads(path.read_text())["marks"]["goal"], "#00ffaa")
            again = GameController(log_path=None, palette_path=path).snapshot()["palette"]["roles"]["goal"]
            self.assertEqual(again["mark"], "#00ffaa")
            state = game.action({"action": "mark_set", "role": "goal", "hex": None})
            self.assertEqual(state["palette"]["roles"]["goal"]["mark"], hex_before)
            self.assertEqual(state["message"], "La marca de llegada vuelve a su color de detección.")

    def test_bad_marks_are_refused_and_the_reset_clears_them(self):
        game = GameController(log_path=None)
        with self.assertRaisesRegex(RequestError, "#rrggbb"):
            game.action({"action": "mark_set", "role": "goal", "hex": "verde"})
        with self.assertRaisesRegex(RequestError, "Rol"):
            game.action({"action": "mark_set", "role": "sky", "hex": "#ffffff"})
        game.action({"action": "mark_set", "role": "start", "hex": "#123456"})
        state = game.action({"action": "palette_reset"})
        self.assertFalse(state["palette"]["roles"]["start"]["mark_custom"])
        self.assertFalse(state["palette"]["custom"])

    def test_a_new_mark_asks_for_the_fill_check_again(self):
        game = GameController(log_path=None)
        game._overlay = game._overlay_state("ok", [])
        state = game.action({"action": "mark_set", "role": "bumper", "hex": "#ffffff"})
        self.assertEqual(state["alignment"]["overlay"]["status"], "unchecked")

    def test_a_palette_file_without_marks_still_loads(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "palette.json"
            path.write_text(json.dumps({"version": 1, "choices": {"start": None, "goal": None, "bumper": None,
                                                                  "marker": None}}))
            state = GameController(log_path=None, palette_path=path).snapshot()
            self.assertFalse(state["palette"]["roles"]["goal"]["mark_custom"])


class ZoneColorTests(unittest.TestCase):
    """Contract 2.7: the boxes' and portals' colors, never in a role's hue."""

    def test_defaults_sit_off_every_role_and_a_choice_is_saved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "palette.json"
            game = GameController(log_path=None, palette_path=path)
            zones = game.snapshot()["palette"]["zones"]
            self.assertEqual({part: (z["hex"], z["conflict"]) for part, z in zones.items()},
                             {part: (ZONE_DEFAULTS[part], None) for part in ZONE_DEFAULTS})
            state = game.action({"action": "zone_color_set", "part": "goal", "hex": "#A5B4FC"})
            self.assertEqual(state["palette"]["zones"]["goal"], {"hex": "#a5b4fc", "custom": True, "conflict": None})
            self.assertEqual(json.loads(path.read_text())["zones"]["goal"], "#a5b4fc")
            again = GameController(log_path=None, palette_path=path).snapshot()["palette"]["zones"]["goal"]
            self.assertEqual(again["hex"], "#a5b4fc")
            state = game.action({"action": "zone_color_set", "part": "goal", "hex": None})
            self.assertEqual(state["palette"]["zones"]["goal"]["hex"], ZONE_DEFAULTS["goal"])

    def test_a_color_the_camera_would_read_as_a_post_it_is_refused(self):
        game = GameController(log_path=None)
        for hex_value, role in (("#ffd166", "inicio"), ("#00ffff", "rebotes"), ("#ff77d3", "esquinas")):
            with self.subTest(hex=hex_value), self.assertRaisesRegex(RequestError, f"post-its de {role}"):
                game.action({"action": "zone_color_set", "part": "start", "hex": hex_value})
        self.assertIsNone(zone_conflict("#e8e2c0", game.palette), "too pale for any role: allowed")
        with self.assertRaisesRegex(RequestError, "Parte del nivel"):
            game.action({"action": "zone_color_set", "part": "door", "hex": "#38bdf8"})

    def test_a_palette_change_that_makes_a_box_unsafe_flags_it(self):
        game = GameController(log_path=None)
        state = game.action({"action": "palette_set", "role": "bumper", "hex": "#2dd4bf"})
        self.assertEqual(state["palette"]["zones"]["start"]["conflict"], "bumper")
        state = game.action({"action": "palette_reset"})
        self.assertIsNone(state["palette"]["zones"]["start"]["conflict"])


if __name__ == "__main__":
    unittest.main()
