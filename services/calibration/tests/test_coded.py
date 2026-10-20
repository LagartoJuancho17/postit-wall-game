"""Gate tests for the binary-coded dot alignment (contract 1.1-1.3).

Frames are 320x240 at most and the whole file runs well under 0.4 s. The
realistic cases (1280x720 camera, gamma, JPEG, exposure drift, post-its,
glare, partial views) live in services/calibration/evals/coded.py.

Steps 5-12 of the decode are also tested on hand-made inputs through the
module's step functions, because each threshold has a boundary that a
rendered scene cannot hit exactly.
"""

from functools import lru_cache
import json
import math
from pathlib import Path
import re
import unittest

import cv2
import numpy as np

from services.calibration import (
    ALIGNMENT_FRAMES,
    ALIGNMENT_REASONS,
    alignment_code,
    alignment_layout,
    decode_alignment,
    pattern_dots,
)
from services.calibration import coded
from services.calibration.tests.render import (
    capture_frames,
    homography_from_quad,
    render_pattern,
    truth_errors,
)

CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "alignment-and-color.md"

SIZE = (320, 240)  # projector and camera px in the end-to-end tests
KEYSTONE = [[24, 18], [296, 10], [304, 228], [14, 222]]  # projector corners in camera px
SQUARE = [[10, 8], [310, 8], [310, 232], [10, 232]]      # axis-aligned: x = 10 + 0.9375 px
BETWEEN_DOTS = (80, 72)  # camera px between dots 11, 12, 20 and 21 under KEYSTONE

# Contract 1.3 output keys, plus `code`: the failure code of the reason
# table, which `alignment_failed {code, ...}` logs and the panel retry rule
# (inaccurate / inconsistent) needs.
RESULT_KEYS = {
    "ok", "reason", "matrix", "decoded", "total", "fit", "inliers", "holdout",
    "holdout_p95_px", "holdout_max_px", "outliers", "coverage", "threshold",
    "gains", "rejected", "code",
}
REJECTED_KEYS = {"ambiguous", "parity", "unknown_id", "duplicate", "area"}
NO_REJECTIONS = dict.fromkeys(REJECTED_KEYS, 0)


@lru_cache(maxsize=None)
def _capture(quad: tuple, drift: float = 0.0, reflections: tuple = ()) -> tuple:
    """Rendered frames (read-only) for a 320x240 projector and camera."""

    layout = alignment_layout(*SIZE)
    G = homography_from_quad(SIZE, [list(corner) for corner in quad])
    frames = capture_frames(layout, G, SIZE, seed=7, drift=drift, reflections=reflections)
    for image in frames.values():
        image.setflags(write=False)
    return layout, G, frames


def capture(quad=KEYSTONE, **options):
    layout, G, frames = _capture(tuple(map(tuple, quad)), **options)
    return layout, G, dict(frames)  # tests replace entries, never pixels in place


def found_dots(layout, ids, camera_of=None, moves=None):
    """Identified dots as step 8 hands them to steps 9-12, sorted by id.

    Camera px = projector px + (5, 3) unless `camera_of` says otherwise;
    `moves` shifts single dots (by id) in camera px.
    """

    camera_of = camera_of or (lambda x, y: (x + 5.0, y + 3.0))
    moves = moves or {}
    targets = {dot["id"]: (dot["x"], dot["y"]) for dot in layout["dots"]}
    dots = []
    for dot_id in sorted(ids):
        x, y = camera_of(*targets[dot_id])
        dx, dy = moves.get(dot_id, (0.0, 0.0))
        dots.append({"id": dot_id, "x": x + dx, "y": y + dy})
    return dots


def solve(layout, found):
    return coded._solve(found, layout, coded._empty_result(len(layout["dots"])))


class LayoutTests(unittest.TestCase):
    def test_contract_sizes(self):
        for size, cols, rows in (((1920, 1080), 10, 6), ((1080, 767), 9, 7), ((787, 752), 8, 7)):
            with self.subTest(size=size):
                layout = alignment_layout(*size)
                self.assertEqual((layout["cols"], layout["rows"]), (cols, rows))
                self.assertEqual(len(layout["dots"]), cols * rows)
        self.assertEqual(alignment_layout(1920, 1080)["radius"], 0.012 * 1080)
        self.assertEqual(alignment_layout(320, 240)["radius"], 4.0)  # floor of 4 px

    def test_rounding_is_python_half_to_even(self):
        # sqrt(60 * aspect) lands exactly on .5 for these sizes; JavaScript's
        # Math.round would go up, Python's round (and pyRound) to the even side.
        for size, cols, rows in (((169, 240), 6, 9), ((294, 160), 10, 6), ((108, 320), 4, 9)):
            with self.subTest(size=size):
                root = math.sqrt(60 * (size[0] / size[1]))
                self.assertEqual(root % 1, 0.5)
                layout = alignment_layout(*size)
                self.assertEqual((layout["cols"], layout["rows"]), (cols, rows))
        # 787 x 752: rows = round(7.5) = 8, then trimmed to 7 by the 63-dot cap.
        self.assertEqual(round(60 / 8), 8)
        self.assertEqual(alignment_layout(787, 752)["rows"], 7)

    def test_clamps_at_extreme_aspects(self):
        self.assertEqual(alignment_layout(10000, 100)["cols"], 12)
        self.assertEqual(alignment_layout(10000, 100)["rows"], 5)
        self.assertEqual(alignment_layout(100, 10000)["cols"], 4)
        self.assertEqual(alignment_layout(100, 10000)["rows"], 9)
        self.assertEqual(alignment_layout(100000, 1)["cols"], 12)
        self.assertEqual(alignment_layout(1, 100000)["rows"], 9)

    def test_dot_count_never_exceeds_63(self):
        # The grid depends only on the aspect: sweep 0.01 .. 100 geometrically.
        grids = set()
        for step in range(-400, 401):
            width = 1000 * 10 ** (step / 200)
            layout = alignment_layout(width, 1000)
            cols, rows = layout["cols"], layout["rows"]
            grids.add((cols, rows))
            self.assertTrue(4 <= cols <= 12 and 3 <= rows <= 9, width)
            self.assertLessEqual(cols * rows, 63, width)
            self.assertEqual([dot["id"] for dot in layout["dots"]], list(range(1, cols * rows + 1)))
        self.assertIn((4, 9), grids)
        self.assertIn((12, 5), grids)

    def test_dots_and_frames_follow_the_formula(self):
        layout = alignment_layout(1920, 1080)
        self.assertEqual(set(layout), {"version", "width", "height", "cols", "rows",
                                       "radius", "bits", "dots", "frames"})
        self.assertEqual((layout["version"], layout["width"], layout["height"], layout["bits"]),
                         (1, 1920, 1080, 7))
        margin = 0.06
        for j in range(6):
            for i in range(10):
                dot = layout["dots"][j * 10 + i]
                self.assertEqual(dot["id"], j * 10 + i + 1)
                self.assertEqual(dot["x"], 1920 * (margin + (1 - 2 * margin) * i / (10 - 1)))
                self.assertEqual(dot["y"], 1080 * (margin + (1 - 2 * margin) * j / (6 - 1)))
        # Full floats, not rounded (contract example 115.2 / 64.8).
        self.assertAlmostEqual(layout["dots"][0]["x"], 115.2, places=9)
        self.assertAlmostEqual(layout["dots"][0]["y"], 64.8, places=9)
        expected = [{"bit": b, "inverse": inv} for b in range(7) for inv in (False, True)]
        self.assertEqual(layout["frames"], expected)
        self.assertEqual(list(ALIGNMENT_FRAMES), [(f["bit"], f["inverse"]) for f in expected])
        self.assertEqual(json.loads(json.dumps(layout)), layout)

    def test_rejects_bad_sizes(self):
        for size in ((0, 100), (100, -1), (math.nan, 100), (math.inf, 100), (True, 100), ("1", 2)):
            with self.subTest(size=size), self.assertRaises(ValueError):
                alignment_layout(*size)


class CodeTests(unittest.TestCase):
    def test_code_is_id_plus_even_parity_bit(self):
        self.assertEqual([alignment_code(i) for i in (1, 2, 3, 7, 63)], [65, 66, 3, 71, 63])
        for dot_id in range(1, 64):
            code = alignment_code(dot_id)
            self.assertEqual(code & 63, dot_id)
            self.assertLess(code, 128)
            self.assertEqual(code.bit_count() % 2, 0)

    def test_single_bit_errors_fail_parity(self):
        codes = [alignment_code(dot_id) for dot_id in range(1, 64)]
        self.assertEqual(len(set(codes)), 63)
        for code in codes:
            for bit in range(7):
                flipped = code ^ (1 << bit)
                self.assertNotEqual(((flipped >> 6) & 1), (flipped & 63).bit_count() % 2)

    def test_each_dot_lit_in_exactly_one_frame_of_each_pair(self):
        for size in ((1920, 1080), (1080, 767), (787, 752), (320, 240)):
            layout = alignment_layout(*size)
            ids = {dot["id"] for dot in layout["dots"]}
            for bit in range(7):
                with self.subTest(size=size, bit=bit):
                    plain = pattern_dots(layout, bit, False)
                    inverse = pattern_dots(layout, bit, True)
                    self.assertEqual(set(plain) & set(inverse), set())
                    self.assertEqual(set(plain) | set(inverse), ids)
                    for dot_id in plain:
                        self.assertEqual((alignment_code(dot_id) >> bit) & 1, 1)

    def test_rendered_pairs_project_the_same_total_light(self):
        layout = alignment_layout(*SIZE)
        totals = [
            render_pattern(layout, bit, False).astype(np.int32)
            + render_pattern(layout, bit, True)
            for bit in range(7)
        ]
        for bit in range(1, 7):
            self.assertTrue(np.array_equal(totals[bit], totals[0]), bit)

    def test_bad_ids_and_bits_raise(self):
        for bad in (-1, 64, True, 1.0, "1"):
            with self.subTest(dot_id=bad), self.assertRaises(ValueError):
                alignment_code(bad)
        layout = alignment_layout(*SIZE)
        for bad in (-1, 7, False, 0.0):
            with self.subTest(bit=bad), self.assertRaises(ValueError):
                pattern_dots(layout, bad, False)


class NumpyDefinitionTests(unittest.TestCase):
    """The spelled-out reductions equal numpy's, bit for bit (JS parity)."""

    def test_median_and_percentile_match_numpy(self):
        rng = np.random.default_rng(3)
        for count in (1, 2, 3, 4, 5, 10, 99, 100, 1001, 4096):
            for values in (rng.uniform(0, 300, count), rng.integers(0, 6, count).astype(np.float64)):
                ordered = np.sort(values)
                self.assertEqual(coded._sorted_median(ordered), float(np.median(values)), count)
                for q in (0, 1, 50, 95, 99.9, 100):
                    self.assertEqual(coded._sorted_percentile(ordered, q),
                                     float(np.percentile(values, q)), (count, q))

    def test_percentile_halfway_interpolates_from_the_upper_value(self):
        # Fraction exactly 0.5: numpy's _lerp computes b - (b - a) * 0.5, which
        # for these two values is one ulp above a + (b - a) * 0.5.
        a, b = 8.49590134363889, 37.28498294986918
        self.assertNotEqual(a + (b - a) * 0.5, b - (b - a) * 0.5)
        ordered = np.array([a, b, b + 1.0])
        self.assertEqual(coded._sorted_percentile(ordered, 25), b - (b - a) * 0.5)
        self.assertEqual(coded._sorted_percentile(ordered, 25), float(np.percentile(ordered, 25)))

    def test_gain_uses_the_four_pixel_grid_and_skips_dark_pixels(self):
        # 24 x 32 frames: P = 100, Q = 50 (ratio 2) off the grid. On the grid
        # (y % 4 == 0 and x % 4 == 0) the ratio is 1 in columns 24 and 28,
        # while columns 0-8 have P = 7 and columns 12-20 have Q = 7. A wrong
        # grid or a missing P/Q >= 8 filter moves the median off 1.0.
        plain = np.full((24, 32), 100, dtype=np.uint8)
        inverse = np.full((24, 32), 50, dtype=np.uint8)
        inverse[::4, ::4] = 100
        plain[::4, 0:9:4] = 7
        inverse[::4, 12:21:4] = 7
        frames = {}
        for bit in range(7):
            frames[(bit, False)], frames[(bit, True)] = plain, inverse
        gains, _, _ = coded._signal(frames)
        self.assertEqual(gains, [1.0] * 7)

    def test_threshold_uses_the_two_pixel_grid(self):
        strength = np.full((24, 32), 500.0)
        strength[::2, ::2] = 0.0
        self.assertEqual(coded._threshold(strength), 12.0)  # off-grid values ignored
        rng = np.random.default_rng(5)
        grid = rng.uniform(0, 40, (12, 16))
        grid[0, :3] = 900.0
        strength[::2, ::2] = grid
        expected = max(12.0, 4.0 * np.median(grid) + 6.0, 0.4 * np.percentile(grid, 99.9))
        self.assertEqual(coded._threshold(strength), float(expected))
        self.assertGreater(expected, 4.0 * np.median(grid) + 6.0)  # percentile term wins

    def test_gains_strength_and_threshold_match_the_formulas(self):
        _, _, frames = capture(drift=0.2)
        gains, differences, strength = coded._signal(frames)
        for bit in range(7):
            P = frames[(bit, False)].astype(np.float64)
            Q = frames[(bit, True)].astype(np.float64)
            p, q = P[::4, ::4], Q[::4, ::4]
            usable = (p >= 8) & (q >= 8)
            self.assertEqual(gains[bit], float(np.median(p[usable] / q[usable])))
            self.assertTrue(np.array_equal(differences[bit], P - gains[bit] * Q))
        self.assertNotEqual(gains, [1.0] * 7)  # drift really changed the exposure
        self.assertTrue(np.array_equal(strength, sum(np.abs(d) for d in differences) / 7))
        sample = strength[::2, ::2]
        expected = max(12.0, 4.0 * np.median(sample) + 6.0, 0.4 * np.percentile(sample, 99.9))
        self.assertEqual(coded._threshold(strength), float(expected))

    def test_gain_is_one_when_no_pixel_is_bright_enough(self):
        dark = {key: np.full((24, 32), 7, dtype=np.uint8) for key in ALIGNMENT_FRAMES}
        gains, _, _ = coded._signal(dark)
        self.assertEqual(gains, [1.0] * 7)


def _signals(height=24, width=32):
    return np.zeros((height, width)), [np.zeros((height, width)) for _ in range(7)]


def _paint(strength, differences, pixels, weights, code):
    """A blob whose every bit has the sign of `code` and magnitude = weight."""

    for (y, x), weight in zip(pixels, weights):
        strength[y, x] = weight
        for bit in range(7):
            differences[bit][y, x] = weight if (code >> bit) & 1 else -weight


class ComponentTests(unittest.TestCase):
    """Steps 5-6 on hand-made S and D_b images."""

    def test_border_touching_and_tiny_blobs_are_ignored(self):
        strength, differences = _signals()
        height, width = strength.shape
        square = lambda y, x: [(y, x), (y, x + 1), (y + 1, x), (y + 1, x + 1)]  # noqa: E731
        _paint(strength, differences, square(10, 0), [50] * 4, 65)               # left edge
        _paint(strength, differences, square(3, width - 2), [50] * 4, 65)        # right edge
        _paint(strength, differences, square(0, 20), [50] * 4, 65)               # top edge
        _paint(strength, differences, square(height - 2, 5), [50] * 4, 65)       # bottom edge
        _paint(strength, differences, [(15, 20), (15, 21), (16, 20)], [50] * 3, 65)  # area 3
        _paint(strength, differences, square(9, 12), [50] * 4, 65)              # kept
        _paint(strength, differences, square(1, 1), [50] * 4, 65)               # kept: left 1, top 1
        _paint(strength, differences, square(height - 3, width - 3), [50] * 4, 65)  # kept
        components = coded._components(strength, differences, 10.0)
        boxes = sorted((c["left"], c["top"], c["width"], c["height"]) for c in components)
        self.assertEqual(boxes, [(1, 1, 2, 2), (12, 9, 2, 2), (width - 3, height - 3, 2, 2)])

    def test_eight_connectivity_and_weighted_centroid(self):
        strength, differences = _signals()
        # A diagonal staircase: one blob of area 4 with 8-connectivity, four
        # blobs of area 1 (all dropped) with 4-connectivity.
        _paint(strength, differences, [(5, 10), (6, 11), (7, 12), (8, 13)], [20, 40, 60, 80],
               alignment_code(5))
        (component,) = coded._components(strength, differences, 10.0)
        self.assertEqual(component["area"], 4)
        # Pixel centers at integer coordinates: (20*10 + 40*11 + 60*12 + 80*13) / 200.
        self.assertEqual((component["x"], component["y"]), (12.0, 7.0))
        self.assertEqual(component["code"], alignment_code(5))
        self.assertEqual(component["confidence"], 1.0)

    def test_confidence_is_weighted_mean_over_weighted_absolute_mean(self):
        strength, differences = _signals()
        pixels = [(18, 25), (18, 26), (19, 25), (19, 26)]
        _paint(strength, differences, pixels, [50, 50, 50, 50], alignment_code(3))
        differences[0][19, 26] = -50.0  # bit 0 (set in code 3) disagrees on one pixel
        differences[1][18, 25] = 0.0    # bit 1 has no signal on one pixel
        (component,) = coded._components(strength, differences, 10.0)
        # bit 0: |50*(50+50+50-50)| / (50*200) = 0.5; bit 1: 150/150 = 1.
        self.assertEqual(component["confidence"], 0.5)
        self.assertEqual(component["code"], alignment_code(3))

    def test_a_bit_without_any_signal_has_zero_confidence(self):
        strength, differences = _signals()
        _paint(strength, differences, [(9, 9), (9, 10), (10, 9), (10, 10)], [40] * 4, 65)
        differences[4][9:11, 9:11] = 0.0  # 0 / 0 in the formula: defined as 0
        (component,) = coded._components(strength, differences, 10.0)
        self.assertEqual(component["confidence"], 0.0)


def _component(code, area=30, confidence=1.0, x=0.0):
    return {"x": x, "y": 0.0, "area": area, "code": code, "confidence": confidence}


class IdentifyTests(unittest.TestCase):
    """Steps 6 (ambiguous), 7 and 8."""

    def test_rejection_reasons(self):
        components = [
            _component(alignment_code(9)),
            _component(alignment_code(2), confidence=0.349),   # ambiguous
            _component(alignment_code(4), confidence=0.35),    # kept: not below 0.35
            _component(1),                                     # id 1 without parity bit
            _component(alignment_code(1) | 2),                 # one flipped bit
            _component(0),                                     # id 0: parity ok, unknown
            _component(alignment_code(61)),                    # beyond N = 60
        ]
        kept, rejected = coded._identify(components, 60)
        self.assertEqual([c["id"] for c in kept], [4, 9])
        self.assertEqual(rejected, {"ambiguous": 1, "parity": 2, "unknown_id": 2,
                                    "duplicate": 0, "area": 0})

    def test_duplicates_are_all_dropped(self):
        components = [_component(alignment_code(7), x=1.0), _component(alignment_code(8)),
                      _component(alignment_code(7), x=2.0), _component(alignment_code(7), x=3.0)]
        kept, rejected = coded._identify(components, 60)
        self.assertEqual([c["id"] for c in kept], [8])
        self.assertEqual(rejected["duplicate"], 3)

    def test_area_sanity_uses_the_median(self):
        # Survivor areas sorted: 4 5 30 30 30 30 180 181 -> median (30 + 30) / 2.
        areas = {1: 30, 2: 30, 3: 30, 4: 30, 6: 180, 9: 181, 10: 5, 11: 4}
        components = [_component(alignment_code(i), area=a) for i, a in areas.items()]
        kept, rejected = coded._identify(components, 60)
        self.assertEqual([c["id"] for c in kept], [1, 2, 3, 4, 6, 10])
        self.assertEqual(rejected["area"], 2)


class SolveTests(unittest.TestCase):
    """Steps 9-12 on exact correspondences (camera = projector + (5, 3))."""

    layout = alignment_layout(*SIZE)  # 9 x 7 = 63 dots, holdout ids 5, 10, ..., 60
    every = range(1, 64)

    def test_exact_correspondences_succeed(self):
        result = solve(self.layout, found_dots(self.layout, self.every))
        self.assertTrue(result["ok"])
        self.assertEqual((result["decoded"], result["fit"], result["inliers"], result["holdout"],
                          result["outliers"]), (63, 51, 51, 12, 0))
        self.assertLess(result["holdout_max_px"], 1e-6)
        self.assertAlmostEqual(result["coverage"], 0.88 * 0.88, places=6)
        self.assertEqual(result["matrix"][2][2], 1.0)
        self.assertAlmostEqual(result["matrix"][0][2], -5.0, places=6)

    def test_needs_sixteen_decoded_dots(self):
        # Corners 1, 9, 55, 63; holdout 5, 30, 35, 55, 60; 11 fit points.
        sixteen = [1, 9, 55, 63, 5, 30, 35, 60, 11, 17, 23, 29, 41, 47, 49, 53]
        self.assertTrue(solve(self.layout, found_dots(self.layout, sixteen))["ok"])
        result = solve(self.layout, found_dots(self.layout, sixteen[:-1]))
        self.assertEqual(result["code"], "few_points")
        self.assertEqual(result["reason"], "La cámara vio solo 15 de 63 puntos. Revisá que la "
                         "cámara vea la proyección completa y que el proyector esté encendido.")
        self.assertEqual((result["decoded"], result["fit"], result["holdout"]), (15, 10, 5))
        self.assertIsNone(result["matrix"])
        self.assertIsNone(result["inliers"])

    def test_fit_inliers_need_eighty_percent(self):
        # 51 fit points: inliers >= max(10, ceil(0.8 * 51)) = 41, i.e. 10 bad ones pass.
        fit_ids = [i for i in self.every if i % 5][:11]
        for bad, ok in ((10, True), (11, False)):
            with self.subTest(bad=bad):
                moves = {dot_id: (20.0, 0.0) for dot_id in fit_ids[:bad]}
                result = solve(self.layout, found_dots(self.layout, self.every, moves=moves))
                self.assertEqual(result["inliers"], 51 - bad)
                self.assertEqual(result["ok"], ok)
                if not ok:
                    self.assertEqual(result["code"], "inconsistent")
                    self.assertEqual(result["reason"], ALIGNMENT_REASONS["inconsistent"])
                    self.assertIsNone(result["holdout_p95_px"])

    def test_holdout_outliers_allowed_up_to_ten_percent(self):
        # 12 holdout points: outliers <= max(1, floor(1.2)) = 1.
        for moved, ok in (((5,), True), ((5, 10), False)):
            with self.subTest(moved=moved):
                moves = {dot_id: (0.0, 10.0) for dot_id in moved}
                result = solve(self.layout, found_dots(self.layout, self.every, moves=moves))
                self.assertEqual(result["outliers"], len(moved))
                self.assertEqual(result["ok"], ok)
                if not ok:
                    self.assertEqual(result["code"], "inaccurate")
                    self.assertEqual(result["reason"], "La alineación quedó imprecisa (0.0 px). "
                                     "Fijá la cámara y el proyector y volvé a alinear.")

    def test_holdout_p95_and_max(self):
        holdout = range(5, 64, 5)
        everywhere = {dot_id: (1.2, 1.6) for dot_id in holdout}  # every error 2.0 px
        result = solve(self.layout, found_dots(self.layout, self.every, moves=everywhere))
        self.assertAlmostEqual(result["holdout_p95_px"], 2.0, places=6)
        self.assertEqual(result["reason"], ALIGNMENT_REASONS["inaccurate"].format(p95=2.0))
        # One error of 3.2 px: p95 = 3.2 * 0.45 = 1.44 passes, max 3.2 > 3 fails.
        result = solve(self.layout, found_dots(self.layout, self.every, moves={30: (3.2, 0.0)}))
        self.assertAlmostEqual(result["holdout_p95_px"], 1.44, places=6)
        self.assertAlmostEqual(result["holdout_max_px"], 3.2, places=6)
        self.assertEqual(result["outliers"], 0)
        self.assertEqual(result["code"], "inaccurate")
        self.assertIn("(1.4 px)", result["reason"])
        self.assertIsNone(result["coverage"])

    def test_needs_four_holdout_points(self):
        ids = [i for i in self.every if i % 5 or i in (5, 30, 60)]
        result = solve(self.layout, found_dots(self.layout, ids))
        self.assertEqual((result["holdout"], result["code"]), (3, "inaccurate"))

    def test_low_coverage(self):
        # Rows 0-2, columns 0-5: 18 dots spanning 176 x 70.4 of 320 x 240 px.
        ids = [j * 9 + i + 1 for j in range(3) for i in range(6)]
        result = solve(self.layout, found_dots(self.layout, ids))
        self.assertEqual(result["code"], "low_coverage")
        self.assertEqual(result["reason"], ALIGNMENT_REASONS["low_coverage"])
        self.assertAlmostEqual(result["coverage"], 176 * 70.4 / (320 * 240), places=5)
        self.assertEqual(result["holdout"], 4)
        self.assertIsNotNone(result["holdout_p95_px"])


class DecodeTests(unittest.TestCase):
    """decode_alignment end to end on rendered 320x240 frames."""

    def assertContractShape(self, result):
        self.assertEqual(set(result), RESULT_KEYS)
        json.dumps(result, allow_nan=False)
        if result["rejected"] is not None:
            self.assertEqual(set(result["rejected"]), REJECTED_KEYS)

    def test_perfect_decode_recovers_the_true_homography(self):
        layout, G, frames = capture()
        result = decode_alignment(frames, layout)
        self.assertContractShape(result)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["reason"])
        self.assertIsNone(result["code"])
        self.assertEqual((result["decoded"], result["total"], result["fit"], result["inliers"],
                          result["holdout"], result["outliers"]), (63, 63, 51, 51, 12, 0))
        self.assertEqual(result["rejected"], NO_REJECTIONS)
        self.assertEqual(result["gains"], [1.0] * 7)
        self.assertGreaterEqual(result["threshold"], 12.0)
        self.assertLess(result["holdout_p95_px"], 0.25)
        self.assertAlmostEqual(result["coverage"], 0.88 * 0.88, places=5)
        self.assertEqual(result["matrix"][2][2], 1.0)
        errors = truth_errors(result["matrix"], G, SIZE, SIZE, step=4)
        self.assertGreater(len(errors), 3000)
        self.assertLess(np.percentile(errors, 95), 0.1)
        self.assertLess(errors.max(), 0.2)

    def test_result_does_not_depend_on_frame_order(self):
        layout, _, frames = capture()
        first = decode_alignment(frames, layout)
        again = decode_alignment(dict(reversed(list(frames.items()))), layout)
        self.assertEqual(first, again)

    def test_stale_pair_is_rejected(self):
        # The camera hands over the previous frame again: both frames of the
        # bit-3 pair show the plain pattern, so bit 3 carries no signal.
        layout, _, frames = capture()
        frames[(3, True)] = frames[(3, False)]
        result = decode_alignment(frames, layout)
        self.assertContractShape(result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "few_points")
        self.assertEqual(result["rejected"]["ambiguous"], 63)
        self.assertEqual(result["decoded"], 0)
        self.assertEqual(result["reason"], ALIGNMENT_REASONS["few_points"].format(decoded=0, total=63))
        self.assertIsNone(result["matrix"])

    def test_few_visible_dots_fail_with_few_points(self):
        # Someone stands in front of the wall: only a window stays visible.
        layout, _, frames = capture()
        for key, image in frames.items():
            covered = np.zeros_like(image)
            covered[60:130, 60:160] = image[60:130, 60:160]
            frames[key] = covered
        result = decode_alignment(frames, layout)
        self.assertEqual(result["code"], "few_points")
        self.assertTrue(0 < result["decoded"] < 16, result["decoded"])
        self.assertEqual(result["reason"], ALIGNMENT_REASONS["few_points"].format(
            decoded=result["decoded"], total=63))

    def test_static_glare_is_not_a_dot(self):
        # A saturated glossy highlight between dots, with exposure drifting
        # 20% between frames, cancels in every pair.
        x, y = BETWEEN_DOTS
        layout, G, frames = capture(drift=0.2, reflections=((x, y, 4.0, 3.0),))
        self.assertEqual(int(frames[(0, False)][y, x]), 255)
        result = decode_alignment(frames, layout)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(result["decoded"], 63)
        self.assertEqual(result["rejected"], NO_REJECTIONS)
        _, differences, strength = coded._signal(frames)
        components = coded._components(strength, differences, coded._threshold(strength))
        self.assertEqual(len(components), 63)
        self.assertGreater(min(math.hypot(c["x"] - x, c["y"] - y) for c in components), 10)
        self.assertLess(np.percentile(truth_errors(result["matrix"], G, SIZE, SIZE), 95), 0.15)

    def test_lights_that_are_not_dots_are_rejected(self):
        x, y = BETWEEN_DOTS
        cases = (
            # On in every plain frame: decodes as 127, whose parity is wrong.
            ("parity", [(bit, False) for bit in range(7)]),
            # On in the plain frames of bits 0-3 only: bits 4-6 have no signal.
            ("ambiguous", [(bit, False) for bit in range(4)]),
        )
        for reason, lit in cases:
            with self.subTest(reason=reason):
                layout, _, frames = capture()
                for key in lit:
                    image = frames[key].copy()
                    cv2.circle(image, (x, y), 3, 255, -1)
                    frames[key] = image
                result = decode_alignment(frames, layout)
                self.assertTrue(result["ok"], result["reason"])
                self.assertEqual(result["decoded"], 63)
                self.assertEqual(result["rejected"], {**NO_REJECTIONS, reason: 1})

    def test_dots_cut_by_the_frame_border_are_ignored(self):
        # SQUARE puts column 0 at camera x = 28 and row 6 at y = 218.56.
        # Cropping there cuts those 15 dots in half; their codes are intact,
        # so only the border rule keeps their biased centroids out.
        layout, G, frames = capture(quad=SQUARE)
        cropped = {key: image[:219, 28:] for key, image in frames.items()}
        result = decode_alignment(cropped, layout)
        self.assertTrue(result["ok"], result["reason"])
        self.assertEqual(result["decoded"], 8 * 6)
        self.assertEqual(result["rejected"], NO_REJECTIONS)
        shifted = np.array([[1, 0, -28], [0, 1, 0], [0, 0, 1]], dtype=np.float64) @ G
        errors = truth_errors(result["matrix"], shifted, (292, 219), SIZE, step=4)
        self.assertLess(np.percentile(errors, 95), 0.1)

    def test_missing_or_mismatched_frames(self):
        layout, _, frames = capture()
        missing = dict(frames)
        del missing[(6, True)]
        smaller = dict(frames)
        smaller[(2, False)] = frames[(2, False)][:-1]
        for name, broken in (("missing", missing), ("size", smaller), ("empty", {})):
            with self.subTest(name):
                result = decode_alignment(broken, layout)
                self.assertContractShape(result)
                self.assertEqual(result["code"], "frames")
                self.assertEqual(result["reason"], "Faltan cuadros del patrón de alineación.")
                self.assertFalse(result["ok"])
                self.assertEqual(result["total"], 63)
                computed = {k for k, v in result.items() if v is not None}
                self.assertEqual(computed, {"ok", "reason", "code", "total"})
        wrong = dict(frames)
        wrong[(0, False)] = frames[(0, False)].astype(np.float32)
        with self.assertRaises(ValueError):
            decode_alignment(wrong, layout)

    def test_reasons_match_the_contract_table(self):
        rows = re.findall(r"^\| (\w+) \| `(.+)` \|$", CONTRACT.read_text(encoding="utf-8"), re.M)
        self.assertEqual(len(rows), 5)
        self.assertEqual(dict(rows), ALIGNMENT_REASONS)
        self.assertEqual(ALIGNMENT_REASONS["inaccurate"].format(p95=1.25),
                         "La alineación quedó imprecisa (1.2 px). Fijá la cámara y el "
                         "proyector y volvé a alinear.")


if __name__ == "__main__":
    unittest.main()
