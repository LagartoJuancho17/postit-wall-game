"""Gate tests: hands-free play (contract 3.1): play, the countdown, the cover gesture."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cv2

from contracts import Point
from services.web.backend.eval import DROP_GOAL, _image
from services.web.backend.handsfree import (COUNTDOWN_S, COVER_MAX_S, COVER_MIN_S, COVER_SHRINK, cover_begin,
                                            cover_update, start_note)
from services.web.backend.server import GameController, RequestError

CAMERA = (960, 720)
HERE = Point(180.0, 350.0)
AREA = 3600.0


def _jpeg(image) -> bytes:
    okay, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert okay
    return encoded.tobytes()


WALL = _jpeg(_image(430, goal=DROP_GOAL))
_hand = _image(430, goal=DROP_GOAL)
_hand[310:390, 140:220] = (150, 170, 200)   # a hand over the start note
HAND = _jpeg(_hand)


def feed(cover, frames, start=0.0, every=0.25):
    """frames: a string of "s" (start seen), "h" (hidden, goal seen), "w" (both hidden),
    "g" (a ghost: the start color on the hand, 70% of the paper's area)."""
    events = []
    for k, kind in enumerate(frames):
        seen = (HERE, AREA) if kind == "s" else (HERE, 0.7 * AREA) if kind == "g" else None
        cover, event = cover_update(cover, seen, kind != "w", start + k * every, CAMERA)
        events.append(event)
    return cover, events


class CoverTests(unittest.TestCase):
    def test_a_hand_held_then_lifted_fires_once(self):
        # Hidden from 0.5 s; armed once hidden COVER_MIN_S (at 1.5 s); back at 1.75 s.
        cover, events = feed(cover_begin(), "sshhhhhs")
        self.assertEqual(events, [None, None, None, None, None, None, "armed", "fire"])
        self.assertEqual(cover, {"seen": HERE, "area": AREA, "hidden_since": None, "armed": False, "dark": 0,
                                 "spoiled": False})

    def test_the_projector_ghost_on_the_hand_counts_as_covered(self):
        # Closed loop, 2026-10-27: the start color lit on the hand read as the
        # start for 1.9 s. A ghost shrinks; under COVER_SHRINK of the usual area it is the hand.
        self.assertLess(0.7, 1.0)
        self.assertLess(0.7 * AREA, COVER_SHRINK * AREA)
        _, events = feed(cover_begin(), "ssggggggs")
        self.assertEqual(events, [None, None, None, None, None, None, "armed", None, "fire"])
        _, events = feed(cover_begin(), "ss" + "s" * 5)
        self.assertNotIn("armed", events)

    def test_a_flicker_a_long_absence_and_a_walker_never_fire(self):
        self.assertNotIn("fire", feed(cover_begin(), "shs")[1], "one frame is a flicker")
        self.assertNotIn("fire", feed(cover_begin(), "shhhs")[1], "three frames still are (0.4 s made 30 an hour)")
        long = "s" + "h" * (int(COVER_MAX_S / 0.25) + 2) + "s"
        self.assertNotIn("fire", feed(cover_begin(), long)[1], "a note taken away and stuck back later")
        self.assertNotIn("fire", feed(cover_begin(), "swwwwws")[1], "the goal went dark too")
        self.assertIn("fire", feed(cover_begin(), "shhwhhhs")[1], "one missed goal frame is flicker")
        self.assertNotIn("fire", feed(cover_begin(), "shhwwhhhs")[1], "two in a row: somebody in front")
        self.assertNotIn("fire", feed(cover_begin(), "hhhhs")[1], "never seen before the cover")

    def test_back_somewhere_else_is_a_moved_note(self):
        cover, _ = feed(cover_begin(), "shhhhh")
        _, event = cover_update(cover, (Point(HERE.x + 80, HERE.y), AREA), True, 1.5, CAMERA)
        self.assertIsNone(event)
        _, event = cover_update(cover, (Point(HERE.x + 20, HERE.y), AREA), True, 1.5, CAMERA)
        self.assertEqual(event, "fire", "20 px is within 5% of 720: the same spot, a shaky camera")

    def test_the_bounds_are_inclusive(self):
        cover, _ = feed(cover_begin(), "sh")
        self.assertEqual(cover_update(cover, (HERE, AREA), True, 0.25 + COVER_MIN_S, CAMERA)[1], "fire")
        self.assertIsNone(cover_update(cover, (HERE, AREA), True, 0.25 + COVER_MIN_S - 0.01, CAMERA)[1])

    def test_start_note_needs_exactly_one_start(self):
        from contracts import Polygon
        square = Polygon((Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)))
        self.assertEqual(start_note({"start": (square,)}), (Point(5, 5), 100.0))
        self.assertIsNone(start_note({"start": (square, square)}))
        self.assertIsNone(start_note({}))


class Rig:
    def __init__(self, case: unittest.TestCase) -> None:
        self.now = 0.0
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "events.jsonl"
        self.game = GameController(log_path=self.log, clock=lambda: self.now)
        self.frame(WALL)
        self.game.action({"action": "projection_open", "width": 1280, "height": 800})

    def act(self, name: str, **data):
        return self.game.action({"action": name, **data})

    def frame(self, jpeg: bytes, n: int = 1) -> dict:
        for _ in range(n):
            self.now += 0.25
            self.game.preview_jpeg(jpeg, "Cámara de prueba")
        return self.game.snapshot()

    def tick(self, seconds: float) -> dict:
        state = None
        for _ in range(round(seconds / 0.05)):
            self.now += 0.05
            state = self.game.tick()
        return state

    def events(self, name: str) -> list[dict]:
        return [e for e in map(json.loads, self.log.read_text().splitlines()) if e["event"] == name]


class PlayTests(unittest.TestCase):
    def test_play_counts_down_freezes_the_camera_then_drops(self):
        rig = Rig(self)
        state = rig.act("play")
        game = state["game"]
        self.assertEqual((state["phase"], game["status"], game["countdown_s"], game["countdown_total_s"]),
                         ("countdown", "ready", COUNTDOWN_S, COUNTDOWN_S))
        self.assertTrue(game["camera_paused"], "the countdown lights the wall: the camera is not read")
        self.assertEqual(state["message"], "Preparados: la pelota sale en 3, 2, 1.")
        for refused in ("launch", "play", "align_start", "projection_black", "projection_neutral"):
            with self.assertRaises(RequestError, msg=refused) as caught:
                rig.act(refused)
            self.assertEqual(caught.exception.status, 409)
        state = rig.tick(COUNTDOWN_S - 0.1)
        self.assertEqual(state["phase"], "countdown")
        state = rig.tick(0.15)
        self.assertEqual(state["game"]["status"], "running")
        started = rig.events("attempt_started")[0]
        self.assertEqual((started["attempt"], started["source"]), (1, "panel"))
        self.assertEqual(rig.events("attempt_countdown")[0]["source"], "panel")

    def test_play_after_an_attempt_reads_the_board_again(self):
        rig = Rig(self)
        rig.act("play")
        state = rig.tick(COUNTDOWN_S + 4.0)
        self.assertEqual(state["game"]["status"], "won")
        rig.frame(WALL)   # the panel keeps sending the camera's frames
        state = rig.act("play", source="button")
        self.assertEqual(state["phase"], "countdown")
        self.assertEqual(rig.events("attempt_reset")[-1]["source"], "button")
        state = rig.act("reset")
        self.assertEqual(state["phase"], "ready", "R cancels the countdown")
        self.assertEqual(rig.events("attempt_countdown_cancelled")[-1]["reason"], "reset")

    def test_closing_the_projector_cancels_and_bad_payloads_are_refused(self):
        rig = Rig(self)
        rig.act("play")
        state = rig.act("projection_close")
        self.assertEqual((state["phase"], state["game"]["countdown_s"]), ("ready", 0.0))
        for payload in ({"source": "wall"}, {"source": None}, {"source": "gesture"}):
            with self.assertRaises(RequestError):
                rig.act("play", **payload)
        with self.assertRaises(RequestError):
            rig.act("hands_free", enabled="yes")

    def test_launch_still_drops_at_once(self):
        rig = Rig(self)
        self.assertEqual(rig.act("launch")["game"]["status"], "running")


class GestureTests(unittest.TestCase):
    def test_covering_the_start_arms_and_lifting_the_hand_plays(self):
        rig = Rig(self)
        rig.frame(WALL)
        state = rig.frame(HAND, 5)
        self.assertTrue(state["game"]["hands_free"]["armed"])
        self.assertEqual(state["message"], "Sacá la mano del inicio: la pelota sale.")
        state = rig.frame(WALL)
        self.assertEqual(state["phase"], "countdown")
        self.assertEqual(state["message"], "Preparados: la pelota sale en 3, 2, 1.")
        state = rig.tick(COUNTDOWN_S + 0.1)
        self.assertEqual(state["game"]["status"], "running")
        self.assertEqual(rig.events("attempt_started")[0]["source"], "gesture")

    def test_after_the_end_effect_the_same_gesture_plays_again(self):
        rig = Rig(self)
        rig.frame(WALL)
        rig.frame(HAND, 5)
        rig.frame(WALL)
        state = rig.tick(COUNTDOWN_S + 7.0)   # drop, win, end effect over
        self.assertEqual(state["game"]["status"], "won")
        self.assertFalse(state["game"]["camera_paused"])
        rig.frame(WALL)
        rig.frame(HAND, 5)
        state = rig.frame(WALL)
        self.assertEqual(state["phase"], "countdown")

    def test_switched_off_the_hand_does_nothing(self):
        rig = Rig(self)
        state = rig.act("hands_free", enabled=False)
        self.assertEqual(state["game"]["hands_free"], {"enabled": False, "armed": False})
        rig.frame(WALL)
        rig.frame(HAND, 5)
        self.assertEqual(rig.frame(WALL)["phase"], "ready")
        self.assertEqual(rig.events("hands_free")[0]["enabled"], False)

    def test_nothing_fires_during_an_attempt_or_a_touch_run(self):
        rig = Rig(self)
        rig.act("launch")
        rig.frame(HAND, 5)
        state = rig.frame(WALL)
        self.assertNotEqual(state["phase"], "countdown")
        rig.tick(6.0)
        rig.frame(WALL)
        rig.act("reset")
        rig.frame(WALL)
        rig.act("touch_start")
        rig.frame(HAND, 5)
        self.assertEqual(rig.frame(WALL)["alignment"]["status"], "touching")
        self.assertEqual(rig.events("attempt_countdown"), [])


if __name__ == "__main__":
    unittest.main()
