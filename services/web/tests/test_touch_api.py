"""Gate tests: the touch-grid alignment in the web backend (contract 1.6).

The camera sits below the projector (a trapezoid: the top of the projection
looks narrower), the wall already has a start and a goal note, and a green
post-it is held on each projected cross, frame by frame at 10 fps.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from contracts import Point
from services.calibration.tests.render import homography_from_quad
from services.calibration.touch import TOUCH_HOLD_S, touch_targets
from services.vision.tests.scene import bgr
from services.web.backend.server import ALIGN_STALL_S, TOUCH_CANCELLED, TOUCH_STALLED, GameController, RequestError

CAMERA = (640, 480)
PROJECTOR = (1280, 800)
# Projector corners in the camera: seen from 2 m below, the top edge is shorter.
QUAD = ((150, 40), (500, 46), (610, 430), (30, 420))
LABEL = "Cámara de prueba"
HUES = {"start": 30, "goal": 12, "bumper": 65}
BACKGROUND = (("start", 40, 455), ("goal", 600, 462))
FPS = 10


def _jpeg(frame: np.ndarray) -> bytes:
    okay, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert okay
    return encoded.tobytes()


def wall_image(*held: tuple[float, float], background=BACKGROUND) -> np.ndarray:
    """Gray wall, the background notes, and green post-its held at camera px."""
    frame = np.full((CAMERA[1], CAMERA[0], 3), 205, np.uint8)
    for role, x, y in background:
        cv2.rectangle(frame, (x - 14, y - 14), (x + 14, y + 14), bgr(HUES[role]), -1)
    for x, y in held:
        cv2.rectangle(frame, (round(x) - 14, round(y) - 14), (round(x) + 14, round(y) + 14), bgr(HUES["bumper"]), -1)
    return frame


def wall(*held: tuple[float, float], background=BACKGROUND) -> bytes:
    return _jpeg(wall_image(*held, background=background))


def seen(G: np.ndarray, point: Point, dx: float = 0.0, dy: float = 0.0) -> tuple[float, float]:
    """Where the camera sees a projector px (G: projector px -> camera px)."""
    x, y, w = G @ np.array([point.x + dx, point.y + dy, 1.0])
    return x / w, y / w


class Rig:
    def __init__(self, case: unittest.TestCase) -> None:
        self.now = 0.0
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.log = Path(directory.name) / "events.jsonl"
        self.game = GameController(log_path=self.log, clock=lambda: self.now)
        self.G = homography_from_quad(PROJECTOR, QUAD)   # projector px -> camera px (the truth)
        self.game.action({"action": "projection_open", "width": PROJECTOR[0], "height": PROJECTOR[1]})
        self.frame(wall())

    def act(self, name: str, **data):
        return self.game.action({"action": name, **data})

    def frame(self, jpeg: bytes) -> dict:
        self.now += 1 / FPS
        self.game.preview_jpeg(jpeg, LABEL)
        return self.game.snapshot()

    def seen(self, point: Point, dx: float = 0.0, dy: float = 0.0) -> tuple[float, float]:
        return seen(self.G, point, dx, dy)

    def touch(self, target: Point, dx: float = 0.0, dy: float = 0.0, hold_s: float = TOUCH_HOLD_S + 0.2) -> dict:
        """Hold a post-it on the cross, then take it away."""
        at = self.seen(target, dx, dy)
        for _ in range(round(hold_s * FPS)):
            self.frame(wall(at))
        return self.frame(wall())

    def events(self, name: str) -> list[dict]:
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [event for event in map(json.loads, lines) if event["event"] == name]


class TouchRunTests(unittest.TestCase):
    def test_a_full_run_aligns_a_camera_far_below_the_projector(self):
        rig = Rig(self)
        state = rig.act("touch_start")
        alignment = state["alignment"]
        self.assertEqual(alignment["status"], "touching")
        self.assertEqual(state["projection"]["mode"], "touch")
        targets = touch_targets(*PROJECTOR, 4, 4)
        self.assertEqual([(t["x"], t["y"]) for t in alignment["touch"]["targets"]],
                         [(round(t.x, 3), round(t.y, 3)) for t in targets])
        self.assertEqual(rig.events("touch_started")[0]["background"], 2)
        for index, target in enumerate(targets):
            state = rig.touch(target)
            if index < len(targets) - 1:
                self.assertEqual(state["alignment"]["touch"]["step"], index + 1)
        alignment = state["alignment"]
        self.assertEqual(alignment["status"], "ready", alignment["detail"])
        self.assertEqual(alignment["quality"]["method"], "touch")
        self.assertIsNone(alignment["touch"])
        self.assertIsNone(alignment["lens"])
        self.assertEqual(state["projection"]["mode"], "live")
        self.assertRegex(alignment["detail"],
                         r"^Proyector alineado por toques: el contorno cae a unos \d+\.\d px de cada papel "
                         r"\(16 toques medidos\)\.$")
        self.assertTrue(alignment["overlay"]["probes"], "the overlay check comes next, as after the dots")
        # H maps camera px to projector px: the truth everywhere in the projection, not only at the touches.
        H = np.array(alignment["matrix"])
        worst = 0.0
        for x in np.linspace(0, PROJECTOR[0], 9):
            for y in np.linspace(0, PROJECTOR[1], 7):
                cx, cy = rig.seen(Point(float(x), float(y)))
                u, v, w = H @ np.array([cx, cy, 1.0])
                worst = max(worst, float(np.hypot(u / w - x, v / w - y)))
        self.assertLess(worst, 4.0)
        self.assertEqual(len(rig.events("touch_captured")), 16)
        solved = rig.events("touch_solved")[0]
        self.assertLessEqual(solved["holdout_p95_px"], 6.0)
        self.assertEqual(rig.events("alignment_ready")[0]["method"], "touch")

    def test_the_circle_fills_only_while_the_post_it_stays_still(self):
        rig = Rig(self)
        rig.act("touch_start")
        target = touch_targets(*PROJECTOR, 4, 4)[0]
        state = rig.frame(wall(rig.seen(target)))
        self.assertEqual(state["alignment"]["touch"]["phase"], "holding")
        state = rig.frame(wall(rig.seen(target)))
        self.assertAlmostEqual(state["alignment"]["touch"]["progress"], 0.1, places=3)
        # Moved by ~30 projector px (about 10 camera px): the hold starts over.
        for _ in range(8):
            rig.frame(wall(rig.seen(target)))
        state = rig.frame(wall(rig.seen(target, 30, 0)))
        self.assertEqual(state["alignment"]["touch"]["progress"], 0.0)
        self.assertEqual(state["alignment"]["touch"]["step"], 0)
        for _ in range(11):
            state = rig.frame(wall(rig.seen(target, 30, 0)))
        self.assertEqual(state["alignment"]["touch"]["step"], 1)
        self.assertEqual(state["alignment"]["touch"]["phase"], "lift")

    def test_the_next_cross_waits_until_the_post_it_is_taken_away(self):
        rig = Rig(self)
        rig.act("touch_start")
        targets = touch_targets(*PROJECTOR, 4, 4)
        at = rig.seen(targets[0])
        for _ in range(30):
            state = rig.frame(wall(at))
        self.assertEqual(state["alignment"]["touch"]["step"], 1)
        self.assertEqual(state["alignment"]["touch"]["phase"], "lift")
        state = rig.frame(wall())
        self.assertEqual(state["alignment"]["touch"]["phase"], "waiting")

    def test_two_new_post_its_capture_nothing(self):
        rig = Rig(self)
        rig.act("touch_start")
        a, b = touch_targets(*PROJECTOR, 4, 4)[:2]
        for _ in range(20):
            state = rig.frame(wall(rig.seen(a), rig.seen(b)))
        self.assertTrue(state["alignment"]["touch"]["many"])
        self.assertEqual(state["alignment"]["touch"]["step"], 0)

    def test_b_takes_the_background_again(self):
        rig = Rig(self)
        rig.act("touch_start")
        # A note stuck on the wall after the start would be taken for a touch.
        extra = (*BACKGROUND, ("goal", 320, 470))
        state = rig.frame(wall(background=extra))
        self.assertEqual(state["alignment"]["touch"]["phase"], "holding")
        rig.act("touch_background")
        state = rig.frame(wall(background=extra))
        self.assertEqual(state["alignment"]["touch"]["phase"], "waiting")
        self.assertEqual(rig.events("touch_background_taken")[-1]["notes"], 3)
        for _ in range(15):
            state = rig.frame(wall(background=extra))
        self.assertEqual(state["alignment"]["touch"]["step"], 0)

    def test_r_goes_back_one_point(self):
        rig = Rig(self)
        rig.act("touch_start")
        targets = touch_targets(*PROJECTOR, 4, 4)
        rig.touch(targets[0])
        rig.touch(targets[1])
        state = rig.act("touch_back")
        self.assertEqual(state["alignment"]["touch"]["step"], 1)
        self.assertEqual(state["message"], "Volvé al punto 2.")
        state = rig.touch(targets[1])
        self.assertEqual(state["alignment"]["touch"]["step"], 2)

    def test_a_slipped_touch_is_asked_again_and_the_retake_finishes_the_run(self):
        rig = Rig(self)
        rig.act("touch_start")
        targets = touch_targets(*PROJECTOR, 4, 4)
        for index, target in enumerate(targets):
            state = rig.touch(target, dx=40 if index == 6 else 0)
        alignment = state["alignment"]
        self.assertEqual(alignment["status"], "touching", "the run goes on: only the slip comes back")
        self.assertEqual(alignment["detail"], "Algunos toques no coinciden con los demás: repetí el punto 7. "
                                              "Pegá el post-it centrado en la cruz y sacá la mano.")
        touch = alignment["touch"]
        self.assertEqual((touch["step"], touch["captured"], touch["retake"], touch["rounds"]), (6, 16, [6], 1))
        self.assertEqual(state["projection"]["mode"], "touch")
        retake = rig.events("touch_retake")[0]
        self.assertEqual((retake["points"], retake["round"]), ([7], 1))
        state = rig.act("touch_back")
        self.assertEqual(state["message"], "Mientras se repiten toques, R no vuelve atrás: repetí el punto 7.")
        self.assertEqual(state["alignment"]["touch"]["captured"], 16)
        state = rig.touch(targets[6])
        alignment = state["alignment"]
        self.assertEqual(alignment["status"], "ready", alignment["detail"])
        self.assertTrue(alignment["detail"].endswith("(16 toques medidos, 1 vuelta de repetición)."))
        self.assertEqual((alignment["quality"]["outliers"], alignment["quality"]["fit"]), (0, 16))
        captured = rig.events("touch_captured")
        self.assertEqual((captured[-1]["step"], captured[-1]["retake"]), (7, True))
        self.assertEqual(rig.events("touch_solved")[0]["rounds"], 1)

    def test_a_hand_that_keeps_slipping_gives_up_after_three_rounds(self):
        rig = Rig(self)
        rig.act("touch_start")
        targets = touch_targets(*PROJECTOR, 4, 4)
        for index, target in enumerate(targets):
            state = rig.touch(target, dx=40 if index == 6 else 0)
        for round_ in range(1, 4):
            self.assertEqual(state["alignment"]["touch"]["rounds"], round_)
            state = rig.touch(targets[6], dx=-40 if round_ % 2 else 40)
        alignment = state["alignment"]
        self.assertEqual((alignment["status"], alignment["code"]), ("failed", "touch_inaccurate"))
        self.assertTrue(alignment["detail"].startswith("Los toques están muy dispersos: el contorno quedaría a unos "))
        self.assertEqual(state["projection"]["mode"], "live")
        self.assertEqual(len(rig.events("touch_retake")), 3)
        self.assertEqual(rig.events("touch_failed")[0]["rounds"], 3)

    def test_touches_in_another_order_are_refused(self):
        rig = Rig(self)
        rig.act("touch_start")
        targets = touch_targets(*PROJECTOR, 4, 4)
        for index in range(16):
            state = rig.touch(targets[(index % 4) * 4 + index // 4])   # column by column
        alignment = state["alignment"]
        self.assertEqual(alignment["status"], "failed")
        self.assertEqual(alignment["code"], "touch_invalid")
        self.assertIsNone(alignment["matrix"])
        self.assertEqual(state["projection"]["mode"], "live")
        self.assertEqual(rig.events("touch_failed")[0]["code"], "touch_invalid")

    def test_cancel_stall_and_camera_change_end_the_run(self):
        rig = Rig(self)
        rig.act("touch_start")
        state = rig.act("align_cancel")
        self.assertEqual((state["alignment"]["status"], state["alignment"]["detail"]), ("none", TOUCH_CANCELLED))
        self.assertEqual(state["projection"]["mode"], "live")
        rig.act("touch_start")
        rig.now += ALIGN_STALL_S + 0.1
        state = rig.game.snapshot()
        self.assertEqual((state["alignment"]["status"], state["alignment"]["detail"]), ("none", TOUCH_STALLED))
        rig.frame(wall())
        rig.act("touch_start")
        rig.now += 0.1
        rig.game.preview_jpeg(wall(), "Otra cámara")
        self.assertEqual(rig.game.snapshot()["alignment"]["status"], "stale")

    def test_refusals(self):
        game = GameController(log_path=None, clock=lambda: 0.0)
        with self.assertRaises(RequestError):
            game.action({"action": "touch_start"})   # no projector
        game.action({"action": "projection_open", "width": 1280, "height": 800})
        with self.assertRaises(RequestError):
            game.action({"action": "touch_start"})   # no camera yet
        rig = Rig(self)
        for cols, rows in ((2, 4), (4, 9), ("4", 4), (True, 4)):
            with self.assertRaises(RequestError):
                rig.act("touch_start", cols=cols, rows=rows)
        for name in ("touch_back", "touch_background"):
            with self.assertRaises(RequestError):
                rig.act(name)
        state = rig.act("touch_start", cols=5, rows=3)
        self.assertEqual(len(state["alignment"]["touch"]["targets"]), 15)
        for name in ("launch", "projection_black", "projection_neutral"):
            with self.assertRaises(RequestError) as caught:
                rig.act(name)
            self.assertIn("calibración por toques", str(caught.exception))
        state = rig.act("projection_close")
        self.assertEqual(state["alignment"]["status"], "none")


if __name__ == "__main__":
    unittest.main()
