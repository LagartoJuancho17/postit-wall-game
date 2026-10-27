"""Gate tests: services/calibration/touch.py, the touch-grid alignment (contract 1.6)."""

from __future__ import annotations

import math
import re
import unittest
from pathlib import Path

import numpy as np

from contracts import Point, Polygon
from services.calibration.touch import (TOUCH_GRID, TOUCH_HOLD_S, TOUCH_MARGIN, TOUCH_MAX_MODEL_PX, TOUCH_MAX_ROUNDS,
                                        TOUCH_REASONS,
                                        fit_homography, note_centers, points_text, solve_touch, touch_back, touch_begin,
                                        touch_give_up, touch_progress, touch_retake, touch_step, touch_targets,
                                        touch_update, valid_grid)

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "alignment-and-color.md"
CAMERA = (640, 480)
# Projector px -> camera px, a camera below the projector (the top looks narrower).
G = np.array([[0.30, -0.06, 150.0], [0.004, 0.42, 40.0], [0.0, 0.00028, 1.0]])
# The first run on Tobias's wall (runs/web-events.jsonl, 2026-10-27 13:49-13:53,
# FaceTime camera 1280 x 720, projector 1920 x 1080, 4 x 4): the first solver
# refused it whole (p95 30 px) and the outline stayed on an old alignment.
REAL_2026_10_27 = [Point(x, y) for x, y in (
    (154.794, 108.675), (480.654, 104.876), (800.907, 108.103), (1114.364, 114.078),
    (156.156, 281.952), (483.065, 277.573), (808.329, 276.465), (1118.763, 284.902),
    (161.512, 454.973), (487.44, 452.208), (811.273, 442.956), (1123.876, 455.912),
    (165.536, 628.091), (492.83, 624.435), (825.293, 636.653), (1118.688, 620.461))]


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

    def test_retakes_replace_the_named_touches_in_order(self):
        run = touch_begin(1280, 800, 3, 3, [])
        for index in range(9):
            for k in range(11):
                run, event = touch_update(run, [Point(100 + 40 * index, 200)], CAMERA, index * 10 + k * 0.1)
            if index < 8:
                run, _ = touch_update(run, [], CAMERA, index * 10 + 5)
        self.assertEqual((event, run["phase"]), ("done", "lift"))
        run = touch_retake(run, [2, 7])
        self.assertEqual((touch_step(run), run["rounds"], run["phase"]), (2, 1, "lift"))
        run, _ = touch_update(run, [Point(420, 200)], CAMERA, 100.0)
        self.assertEqual(run["phase"], "lift", "the last post-it leaves before the first retake arms")
        run, _ = touch_update(run, [], CAMERA, 100.1)
        self.assertEqual(touch_back(run)["captured"], run["captured"], "R does not undo during retakes")
        for k in range(11):
            run, event = touch_update(run, [Point(500, 120)], CAMERA, 101 + k * 0.1)
        self.assertEqual((event, run["captured"][2], touch_step(run)), ("captured", Point(500, 120), 7))
        run, _ = touch_update(run, [], CAMERA, 103.0)
        for k in range(11):
            run, event = touch_update(run, [Point(520, 140)], CAMERA, 104 + k * 0.1)
        self.assertEqual((event, len(run["captured"]), run["retake"]), ("done", 9, []))
        self.assertEqual(run["captured"][7], Point(520, 140))
        self.assertEqual(run["captured"][3], Point(220, 200), "the others stay as taken")

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
        self.assertEqual((result["fit"], result["holdout"], result["outliers"], result["retake"]), (16, 16, 0, None))
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
            self.assertEqual((result["fit"], result["outliers"], result["retake"]), (16, 0, None))

    def test_a_slip_is_asked_again_not_dropped(self):
        # Dropping a touch costs more than it saves (evals/touch.py): the
        # robust fit only names it, the run asks for that cross again.
        result = solve_touch(self.touches(noise=1.0, slip=6, slip_px=40), self.targets, 1280, 800)
        self.assertFalse(result["ok"])
        self.assertEqual((result["code"], result["retake"], result["matrix"]), ("touch_retake", [6], None))
        self.assertEqual(result["reason"], TOUCH_REASONS["touch_retake"].format(points="el punto 7"))

    def test_slips_next_to_each_other_are_found_where_least_squares_blames_neighbors(self):
        camera = self.touches(noise=1.0)
        for index, (dx, dy) in {2: (45, 0), 3: (0, 40), 9: (0, 60)}.items():
            camera[index] = seen(self.targets[index], dx, dy)
        result = solve_touch(camera, self.targets, 1280, 800)
        self.assertEqual((result["code"], result["retake"]), ("touch_retake", [2, 3, 9]))
        self.assertEqual(points_text([3, 4, 10]), "los puntos 3, 4 y 10")

    def test_tobias_first_run_is_fixed_by_retaking_two_crosses(self):
        targets = touch_targets(1920, 1080, 4, 4)
        touches = list(REAL_2026_10_27)
        result = solve_touch(touches, targets, 1920, 1080)
        self.assertEqual((result["code"], result["retake"]), ("touch_retake", [14]))
        self.assertGreater(result["outlier_bar_px"], 15, "his hand scatters ~4 px: the bar follows it")
        asked = []
        # Retakes land where the agreeing touches put the cross, give or take
        # the hand's own scatter (~3 px here, as in his run).
        hand = iter([(2.5, -1.8), (-3.1, 2.2), (1.4, 3.0), (-2.2, -2.6), (3.3, 0.9), (-0.8, -3.2)])
        for _ in range(TOUCH_MAX_ROUNDS):
            if result["ok"]:
                break
            asked.append([i + 1 for i in result["retake"]])
            keep = [i for i in range(16) if i not in result["retake"]]
            h = np.linalg.inv(np.array(fit_homography([touches[i] for i in keep], [targets[i] for i in keep])))
            for i in result["retake"]:
                dx, dy = next(hand)
                x, y, w = h @ np.array([targets[i].x + dx, targets[i].y + dy, 1.0])
                touches[i] = Point(float(x / w), float(y / w))
            result = solve_touch(touches, targets, 1920, 1080)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(asked[0], [15])
        self.assertLessEqual(len(asked), 2)
        self.assertLess(result["model_rms_px"], TOUCH_MAX_MODEL_PX)

    def test_loose_runs_ask_for_their_two_worst(self):
        # Every touch within the run's bar, but the mapping is loose.
        result = solve_touch(self.touches(noise=6.0, seed=2), self.targets, 1280, 800)
        self.assertEqual((result["code"], result["outliers"], result["retake"]), ("touch_retake", 0, [1, 14]))
        self.assertGreater(result["model_rms_px"], TOUCH_MAX_MODEL_PX)
        given_up = touch_give_up(result)
        self.assertEqual((given_up["code"], given_up["retake"]), ("touch_inaccurate", None))
        self.assertTrue(given_up["reason"].startswith("Los toques están muy dispersos: el contorno quedaría a unos"))

    def test_wrong_order_and_too_few_are_invalid(self):
        camera = self.touches(noise=1.0)
        transposed = [camera[(i % 4) * 4 + i // 4] for i in range(16)]
        self.assertEqual(solve_touch(transposed, self.targets, 1280, 800)["code"], "touch_invalid")
        self.assertEqual(solve_touch(camera[:8], self.targets[:8], 1280, 800)["code"], "touch_invalid")
        same = [camera[0]] * 16
        result = solve_touch(same, self.targets, 1280, 800)
        self.assertEqual(result["code"], "touch_invalid")
        self.assertIsNone(result["matrix"])

    def test_big_grids_try_spread_subsets_and_stay_fast(self):
        targets = touch_targets(1280, 800, 8, 6)
        camera = [seen(t) for t in targets]
        camera[13] = seen(targets[13], 35, -20)
        result = solve_touch(camera, targets, 1280, 800)
        self.assertEqual((result["code"], result["retake"]), ("touch_retake", [13]))

    def test_reasons_match_the_contract_table(self):
        rows = re.findall(r"^\| (touch_\w+) \| `(.+)` \|$", CONTRACT.read_text(encoding="utf-8"), re.M)
        self.assertEqual(dict(rows), TOUCH_REASONS)


if __name__ == "__main__":
    unittest.main()
