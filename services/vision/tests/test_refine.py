"""Gate tests: sub-pixel outlines (contract 2.4) and the rim bridge (2.5).

Images are built from exact pixel coverage (supersampled with the output
pixel centers on integers), so a test cannot pass by a rendering bias.
"""

from __future__ import annotations

import math
import unittest

import cv2
import numpy as np

from contracts.geometry import offset_convex_polygon
from services.vision.detector import _candidates_by_role, _refine_pass, find_notes, refine_quad
from services.vision.palette import DEFAULT_PALETTE
from services.vision.tests.scene import bgr

SUPER = 8


def coverage(size, polygon) -> np.ndarray:
    """Fraction of each pixel inside `polygon` (pixel centers on integers)."""
    width, height = size
    big = np.zeros((height * SUPER, width * SUPER), np.uint8)
    # Output pixel i averages input [8i, 8i + 8): its center is input 8i + 3.5.
    points = np.array([[(x * SUPER + 3.5) * 16, (y * SUPER + 3.5) * 16] for x, y in polygon], np.int32)
    cv2.fillConvexPoly(big, points, 255, cv2.LINE_8, shift=4)
    return cv2.resize(big, (width, height), interpolation=cv2.INTER_AREA).astype(np.float64) / 255.0


def saturation_image(polygon, size=(160, 120), inside=200.0, outside=25.0, blur=1.2) -> np.ndarray:
    image = outside + (inside - outside) * coverage(size, polygon)
    if blur:
        image = cv2.GaussianBlur(image, (0, 0), blur)
    return np.clip(np.rint(image), 0, 255).astype(np.uint8)


def corner_error(quad, truth) -> float:
    return float(np.max(np.linalg.norm(np.asarray(quad) - np.asarray(truth), axis=1)))


def tilted_paper(size=(320, 240)):
    """A green bumper 52 x 38 px turned 21 degrees, edges from exact coverage, slightly blurred."""
    angle = math.radians(21.0)
    cos, sin = math.cos(angle), math.sin(angle)
    paper = [(160 + cos * x - sin * y, 120 + sin * x + cos * y)
             for x, y in ((-26, -19), (26, -19), (26, 19), (-26, 19))]
    share = coverage(size, paper)[..., None]
    frame = 205.0 * (1 - share) + np.array(bgr(65), np.float64) * share
    frame = cv2.GaussianBlur(np.clip(np.rint(frame), 0, 255).astype(np.uint8), (0, 0), 0.8)
    return frame, paper


def ring(polygon, inner, outer, size=(320, 240)) -> np.ndarray:
    """255 between ``polygon`` offset by ``inner`` and by ``outer`` px."""
    def filled(distance):
        image = np.zeros((size[1], size[0]), np.uint8)
        points = np.array([[x * 16, y * 16] for x, y in offset_convex_polygon(polygon, distance)], np.int32)
        cv2.fillConvexPoly(image, points, 255, cv2.LINE_8, shift=4)
        return image
    return filled(outer) & ~filled(inner)


def midpoint(a, b):
    return ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)


SQUARE = [(40.0, 30.0), (110.0, 30.0), (110.0, 90.0), (40.0, 90.0)]
TILTED = [(52.3, 24.6), (121.8, 38.1), (108.7, 99.4), (37.2, 84.9)]


class RefineTests(unittest.TestCase):
    def test_a_blurred_quad_is_recovered_to_a_fraction_of_a_pixel(self):
        for truth in (SQUARE, TILTED):
            feature = saturation_image(truth)
            coarse = [(x + dx, y + dy) for (x, y), (dx, dy) in zip(truth, ((1.5, 1.0), (-1.2, 1.4), (-1.0, -1.5), (1.3, -0.9)))]
            quad, outcome = refine_quad(feature, coarse)
            self.assertEqual(outcome, "refined")
            self.assertLess(corner_error(quad, truth), 0.5, truth)

    def test_either_winding_and_vertex_order_is_kept(self):
        coarse = [(x + 1, y + 1) for x, y in reversed(SQUARE)]
        quad, outcome = refine_quad(saturation_image(SQUARE), coarse)
        self.assertEqual(outcome, "refined")
        self.assertLess(corner_error(quad, list(reversed(SQUARE))), 0.5)

    def test_a_sharp_quad_stays_within_half_a_pixel(self):
        quad, outcome = refine_quad(saturation_image(TILTED, blur=0), TILTED)
        self.assertEqual(outcome, "refined")
        self.assertLess(corner_error(quad, TILTED), 0.5)

    def test_low_contrast_keeps_the_coarse_quad(self):
        feature = saturation_image(SQUARE, inside=40.0, outside=25.0)
        quad, outcome = refine_quad(feature, SQUARE)
        self.assertEqual(outcome, "few_points")
        np.testing.assert_array_equal(quad, np.array(SQUARE))

    def test_a_short_side_keeps_the_coarse_quad(self):
        tiny = [(50.0, 50.0), (57.0, 50.0), (57.0, 70.0), (50.0, 70.0)]
        quad, outcome = refine_quad(saturation_image(tiny), tiny)
        self.assertEqual(outcome, "short_side")
        np.testing.assert_array_equal(quad, np.array(tiny))

    def test_the_move_guard_scales_with_the_note(self):
        """Both sides at a corner ~2.9 px from the edge move that corner ~4.1 px
        (the blurred edge sits 0.25 px outside the analytic corner; the scan
        reaches 3 px): too far for a 20 px note (reach 4 px), fine for a 60 px
        one (reach 9 px)."""
        def shifted(truth):
            (x0, y0), _, (x1, y1), _ = truth
            return [(x0 - 3.15, y0 - 3.15), (x1, y0 - 3.15), (x1, y1), (x0 - 3.15, y1)]
        small = [(60.0, 50.0), (80.0, 50.0), (80.0, 70.0), (60.0, 70.0)]
        _, outcome = refine_quad(saturation_image(small), shifted(small))
        self.assertEqual(outcome, "moved")
        large = [(40.0, 30.0), (100.0, 30.0), (100.0, 90.0), (40.0, 90.0)]
        quad, outcome = refine_quad(saturation_image(large), shifted(large))
        self.assertEqual(outcome, "refined")
        self.assertLess(corner_error(quad, large), 0.5)
        self.assertGreater(corner_error(quad, shifted(large)), 4.0, "accepted beyond the old fixed 4 px")

    def test_a_second_pass_sees_the_side_the_first_saw_half_of(self):
        """Step 9, on a paper as a dark room shows it: a bright center under
        the projector's fill and a darker, more saturated border ring. With
        one coarse corner 5 px off, that end's profiles miss the edge."""
        angle = math.radians(21.0)
        cos, sin = math.cos(angle), math.sin(angle)
        paper = [(80 + cos * x - sin * y, 60 + sin * x + cos * y)
                 for x, y in ((-38, -26), (38, -26), (38, 26), (-38, 26))]
        center = offset_convex_polygon(paper, -3.3)
        image = 15.0 + 185.0 * coverage((160, 120), paper) - 90.0 * coverage((160, 120), center)
        feature = np.clip(np.rint(cv2.GaussianBlur(image, (0, 0), 1.2)), 0, 255).astype(np.uint8)
        coarse = [paper[0], paper[1], (paper[2][0] - 4.0, paper[2][1] - 3.0), paper[3]]
        first, outcome = _refine_pass(feature, coarse, coarse)
        self.assertEqual(outcome, "refined")
        self.assertGreater(corner_error(first, paper), 1.2)
        quad, outcome = refine_quad(feature, coarse)
        self.assertEqual(outcome, "refined")
        self.assertLess(corner_error(quad, paper), 0.9)

    def test_bad_input_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            refine_quad(np.zeros((10, 10), np.float32), SQUARE)
        with self.assertRaises(ValueError):
            refine_quad(np.zeros((10, 10), np.uint8), [(0, 0), (1, 0), (1, float("nan")), (0, 1)])


class BridgeTests(unittest.TestCase):
    """Contract 2.5: a paper crossed by our own saturated rim stays one paper."""

    # 320x240: a note may cover at most 5% of the frame (3840 px).
    def _split_paper(self):
        frame = np.full((240, 320, 3), 205, np.uint8)
        cv2.rectangle(frame, (100, 80), (150, 120), bgr(65), -1)     # one green bumper
        cv2.line(frame, (125, 70), (125, 130), (255, 255, 255), 3)   # a white rim across it
        bridge = np.zeros((240, 320), np.uint8)
        cv2.line(bridge, (125, 68), (125, 132), 255, 7)              # where the backend draws it
        return frame, bridge

    def test_without_the_bridge_the_rim_splits_the_paper(self):
        frame, _ = self._split_paper()
        self.assertEqual(len(find_notes(frame)["bumper"]), 2)

    def test_the_bridge_joins_it_back_into_the_whole_paper(self):
        frame, bridge = self._split_paper()
        notes = find_notes(frame, bridge=bridge)["bumper"]
        self.assertEqual(len(notes), 1)
        xs = [point.x for point in notes[0].points]
        ys = [point.y for point in notes[0].points]
        self.assertLess(abs(min(xs) - 100), 1.0)
        self.assertLess(abs(max(xs) - 150), 1.0)
        self.assertLess(abs(min(ys) - 80), 1.0)
        self.assertLess(abs(max(ys) - 120), 1.0)

    def test_the_bridge_adds_nothing_on_bare_wall(self):
        frame = np.full((240, 320, 3), 205, np.uint8)
        cv2.line(frame, (125, 70), (125, 130), (255, 255, 255), 3)
        bridge = np.zeros((240, 320), np.uint8)
        cv2.line(bridge, (125, 68), (125, 132), 255, 7)
        self.assertEqual({role: len(notes) for role, notes in find_notes(frame, bridge=bridge).items()},
                         {"start": 0, "goal": 0, "bumper": 0, "marker": 0})

    def test_a_rim_beside_a_paper_never_moves_its_outline(self):
        """The band may reach the paper (blur, alignment slack): it only joins
        pieces. Regression: a band that grew the mask where it touched gave a
        grown or jagged coarse quad, which the refinement could not always
        undo (feedback eval, 2026-10-20: a corner 8.7 px off). The coarse
        quads must not change, not only the refined ones."""
        frame, paper = tilted_paper()
        plain = _candidates_by_role(frame, DEFAULT_PALETTE)["bumper"]
        self.assertEqual(len(plain), 1)
        for inner in (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0):  # band from on the edge to 3 px out
            bridge = ring(paper, inner, inner + 6.0)
            bridged = _candidates_by_role(frame, DEFAULT_PALETTE, bridge)["bumper"]
            self.assertEqual([c.coarse for c in bridged], [c.coarse for c in plain], inner)
            self.assertEqual(find_notes(frame, bridge=bridge), find_notes(frame), inner)

    def test_a_tilted_paper_cut_by_a_rim_is_whole_again(self):
        frame, paper = tilted_paper()
        (x0, y0), (x1, y1) = midpoint(paper[0], paper[1]), midpoint(paper[2], paper[3])
        cut = frame.copy()
        cv2.line(cut, (round(x0), round(y0)), (round(x1), round(y1)), (255, 255, 255), 3)
        self.assertEqual(len(find_notes(cut)["bumper"]), 2)
        bridge = np.zeros(frame.shape[:2], np.uint8)
        cv2.line(bridge, (round(x0), round(y0)), (round(x1), round(y1)), 255, 7)
        notes = find_notes(cut, bridge=bridge)["bumper"]
        self.assertEqual(len(notes), 1)
        found = [(point.x, point.y) for point in notes[0].points]
        self.assertLess(max(min(math.dist(p, q) for q in paper) for p in found), 1.0)

    def test_a_bridge_of_another_size_is_refused(self):
        frame, _ = self._split_paper()
        with self.assertRaises(ValueError):
            find_notes(frame, bridge=np.zeros((10, 10), np.uint8))


if __name__ == "__main__":
    unittest.main()
