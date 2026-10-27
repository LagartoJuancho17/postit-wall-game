"""Gate tests: services/calibration/touch.py, the touch-grid alignment (contract 1.6)."""

from __future__ import annotations

import math
import re
import unittest
from pathlib import Path

import numpy as np

from contracts import Point, Polygon
from services.calibration.touch import (TOUCH_GRID, TOUCH_HOLD_S, TOUCH_MARGIN, TOUCH_REASONS, note_centers,
                                        solve_touch, touch_back, touch_begin, touch_progress, touch_targets,
                                        touch_update, valid_grid)

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "alignment-and-color.md"
CAMERA = (640, 480)
# Projector px -> camera px, a camera below the projector (the top looks narrower).
G = np.array([[0.30, -0.06, 150.0], [0.004, 0.42, 40.0], [0.0, 0.00028, 1.0]])


def seen(target: Point, dx: float = 0.0, dy: float = 0.0) -> Point:
    x, y, w = G @ np.array([target.x + dx, target.y + dy, 1.0])
    return Point(float(x / w), float(y / w))


class GridTests(unittest.TestCase):
    def test_row_major_grid_inset_by_the_margin(self):
        targets = touch_targets(1280, 800, 4, 4)
        self.assertEqual(len(targets), 16)
        self.assertEqual(targets[0], Point(TOUCH_MARGIN * 1280, TOUCH_MARGIN * 800))
        self.assertEqual(targets[3].y, targets[0].y)
        self.assertAlmostEqual(targets[-1].x, (1 - TOUCH_MARGIN) * 1280)
        self.assertAlmostEqual(targets[-1].y, (1 - TOUCH_MARGIN) * 800)
        self.assertEqual(TOUCH_GRID, (4, 4), "CCV's default calibration.xml: 3 x 3 cells")

    def test_grid_sizes(self):
        self.assertTrue(valid_grid(3, 3))
        self.assertTrue(valid_grid(8, 6))
        for cols, rows in ((2, 3), (9, 4), (4, 7), (4.0, 4), (True, 4), ("4", 4), (None, 4)):
            self.assertFalse(valid_grid(cols, rows), (cols, rows))

    def test_note_centers_role_by_role(self):
        square = Polygon((Point(0, 0), Point(10, 0), Point(10, 10), Point(0, 10)))
        moved = Polygon(tuple(Point(p.x + 100, p.y) for p in square.points))
        centers = note_centers({"goal": (moved,), "start": (square,)}, ("start", "goal", "bumper"))
        self.assertEqual(centers, [Point(5, 5), Point(105, 5)])


class HoldTests(unittest.TestCase):
    def setUp(self):
        self.background = [Point(40, 455), Point(600, 462)]
        self.run = touch_begin(1280, 800, 4, 4, self.background)

    def feed(self, run, centers, now):
        return touch_update(run, self.background + centers, CAMERA, now)

    def test_hold_still_for_a_second_to_capture_the_mean(self):
        run, event = self.run, None
        for k in range(11):
            now = k / 10
            run, event = self.feed(run, [Point(300 + (k % 2), 200)], now)
            if k < 10:
                self.assertIsNone(event)
                self.assertAlmostEqual(touch_progress(run, now), now / TOUCH_HOLD_S)
        self.assertEqual(event, "captured")
        self.assertEqual(run["phase"], "lift")
        self.assertAlmostEqual(run["captured"][0].x, 300 + 5 / 11)

    def test_moving_restarts_and_two_notes_stop_the_hold(self):
        run, _ = self.feed(self.run, [Point(300, 200)], 0.0)
        run, _ = self.feed(run, [Point(300, 200)], 0.5)
        run, _ = self.feed(run, [Point(320, 200)], 0.6)   # 20 px > 0.012 * 480
        self.assertEqual((run["since"], run["samples"]), (0.6, 1))
        run, _ = self.feed(run, [Point(320, 200), Point(500, 300)], 0.7)
        self.assertEqual((run["phase"], run["many"], run["samples"]), ("waiting", True, 0))

    def test_lift_background_and_back(self):
        run = self.run
        for k in range(12):
            run, _ = self.feed(run, [Point(300, 200)], k * 0.1)
        self.assertEqual(len(run["captured"]), 1)
        run, _ = self.feed(run, [Point(300, 200)], 2.0)
        self.assertEqual(run["phase"], "lift", "the same post-it is not captured twice")
        run, _ = self.feed(run, [], 2.1)
        self.assertEqual(run["phase"], "waiting")
        run = {**run, "rebase": True}
        run, event = self.feed(run, [Point(700, 100)], 2.2)
        self.assertEqual(event, "background")
        self.assertEqual(len(run["baseline"]), 3)
        run, _ = self.feed(run, [Point(700, 100)], 2.3)
        self.assertEqual(run["phase"], "waiting", "a note in the background is not a touch")
        self.assertEqual(touch_back(run)["captured"], [])

    def test_the_last_capture_is_done(self):
        run = touch_begin(1280, 800, 3, 3, [])
        event = None
        for index in range(9):
            for k in range(11):
                run, event = touch_update(run, [Point(100 + 40 * index, 200)], CAMERA, index * 10 + k * 0.1)
            if index < 8:
                run, _ = touch_update(run, [], CAMERA, index * 10 + 5)
        self.assertEqual(event, "done")


class SolveTests(unittest.TestCase):
    targets = touch_targets(1280, 800, 4, 4)

    def touches(self, noise=0.0, seed=1, slip=None, slip_px=0.0):
        rng = np.random.default_rng(seed)
        out = []
        for index, target in enumerate(self.targets):
            dx, dy = rng.normal(0, noise, 2) if noise else (0.0, 0.0)
            out.append(seen(target, dx + (slip_px if index == slip else 0.0), dy))
        return out

    def test_exact_touches_recover_the_mapping(self):
        result = solve_touch(self.touches(), self.targets, 1280, 800)
        self.assertTrue(result["ok"])
        self.assertLess(result["holdout_max_px"], 1e-3)
        self.assertEqual((result["fit"], result["holdout"], result["outliers"], result["dropped"]), (16, 16, 0, None))
        H = np.array(result["matrix"])
        for x, y in ((0, 0), (1280, 800), (640, 10)):
            p = seen(Point(x, y))
            u, v, w = H @ np.array([p.x, p.y, 1.0])
            self.assertAlmostEqual(u / w, x, places=3)
            self.assertAlmostEqual(v / w, y, places=3)

    def test_careful_hands_pass(self):
        for seed in range(10):
            result = solve_touch(self.touches(noise=1.0, seed=seed), self.targets, 1280, 800)
            self.assertTrue(result["ok"], (seed, result["reason"]))

    def test_one_slip_is_dropped(self):
        result = solve_touch(self.touches(noise=1.0, slip=6, slip_px=40), self.targets, 1280, 800)
        self.assertTrue(result["ok"])
        self.assertEqual((result["dropped"], result["fit"], result["outliers"]), (7, 15, 1))

    def test_two_slips_are_refused_naming_the_worst(self):
        camera = self.touches(noise=1.0)
        camera[2] = seen(self.targets[2], 45, 0)
        camera[9] = seen(self.targets[9], 0, 60)
        result = solve_touch(camera, self.targets, 1280, 800)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "touch_inaccurate")
        self.assertIsNone(result["matrix"])
        self.assertRegex(result["reason"], r"^Los toques no coinciden entre sí: el punto (3|10) quedó corrido unos")

    def test_wrong_order_and_too_few_are_invalid(self):
        camera = self.touches(noise=1.0)
        transposed = [camera[(i % 4) * 4 + i // 4] for i in range(16)]
        self.assertEqual(solve_touch(transposed, self.targets, 1280, 800)["code"], "touch_invalid")
        self.assertEqual(solve_touch(camera[:8], self.targets[:8], 1280, 800)["code"], "touch_invalid")
        same = [camera[0]] * 16
        result = solve_touch(same, self.targets, 1280, 800)
        self.assertEqual(result["code"], "touch_invalid")
        self.assertFalse(math.isfinite(result["holdout_p95_px"] or math.inf) and result["ok"])

    def test_reasons_match_the_contract_table(self):
        rows = re.findall(r"^\| (touch_\w+) \| `(.+)` \|$", CONTRACT.read_text(encoding="utf-8"), re.M)
        self.assertEqual(dict(rows), TOUCH_REASONS)


if __name__ == "__main__":
    unittest.main()
