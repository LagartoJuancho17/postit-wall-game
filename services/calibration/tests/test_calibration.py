import math
import unittest

import cv2
import numpy as np

from contracts.model import Board, Point, Polygon
from services.calibration import (
    detect_projected_dot,
    estimate_homography,
    reprojection_errors,
    transform_board,
    transform_point,
)


class CalibrationTests(unittest.TestCase):
    def test_four_point_homography_maps_interior(self):
        camera = [Point(0, 0), Point(400, 0), Point(400, 300), Point(0, 300)]
        projector = [Point(20, 40), Point(720, 15), Point(680, 540), Point(80, 560)]
        matrix = estimate_homography(camera, projector)
        self.assertLess(max(reprojection_errors(camera, projector, matrix)), 1e-5)
        middle = transform_point(Point(200, 150), matrix)
        self.assertTrue(300 < middle.x < 430)
        self.assertTrue(250 < middle.y < 320)

    def test_ransac_rejects_one_mismatched_correspondence(self):
        camera = [Point(x, y) for y in (0, 100, 200) for x in (0, 100, 200)]
        projector = [Point(2 * p.x + 10, 1.5 * p.y + 20) for p in camera]
        projector[-1] = Point(20, 20)
        matrix = estimate_homography(camera, projector)
        result = transform_point(Point(150, 150), matrix)
        self.assertAlmostEqual(result.x, 310, delta=0.1)
        self.assertAlmostEqual(result.y, 245, delta=0.1)
        errors = reprojection_errors(camera, projector, matrix)
        self.assertLess(max(errors[:-1]), 0.1)
        self.assertGreater(errors[-1], 200)

    def test_transform_board_includes_every_polygon(self):
        poly = Polygon((Point(1, 2), Point(3, 2), Point(3, 4)))
        board = Board(poly, poly, poly, (poly,), (poly, poly, poly, poly))
        matrix = ((2.0, 0.0, 10.0), (0.0, 3.0, 20.0), (0.0, 0.0, 1.0))
        mapped = transform_board(board, matrix)
        for polygon in (mapped.playfield, mapped.start, mapped.goal,
                        *mapped.bumpers, *mapped.markers):
            self.assertEqual(polygon.points[0], Point(12, 26))
            self.assertEqual(polygon.points[2], Point(16, 32))
        self.assertEqual(board.playfield.points[0], Point(1, 2))

    def test_dot_detection_uses_frame_difference(self):
        dark = np.full((120, 160, 3), 35, dtype=np.uint8)
        # A static bright patch should disappear in the frame difference.
        dark[5:25, 5:25] = 220
        lit = dark.copy()
        cv2.circle(lit, (96, 73), 5, (245, 245, 245), -1)
        dot = detect_projected_dot(dark, lit)
        self.assertLess(math.hypot(dot.x - 96, dot.y - 73), 0.2)

    def test_no_dot_or_two_dots_are_rejected(self):
        dark = np.zeros((100, 100, 3), dtype=np.uint8)
        with self.assertRaisesRegex(ValueError, "no projected dot"):
            detect_projected_dot(dark, dark.copy())
        lit = dark.copy()
        cv2.circle(lit, (20, 20), 4, (255, 255, 255), -1)
        cv2.circle(lit, (80, 80), 4, (255, 255, 255), -1)
        with self.assertRaisesRegex(ValueError, "multiple bright regions"):
            detect_projected_dot(dark, lit)

    def test_exposure_jump_is_not_mistaken_for_a_dot(self):
        dark = np.full((80, 120, 3), 40, dtype=np.uint8)
        lit = np.full_like(dark, 90)
        with self.assertRaisesRegex(ValueError, "large exposure change"):
            detect_projected_dot(dark, lit)

    def test_bad_inputs_fail_clearly(self):
        with self.assertRaisesRegex(ValueError, "at least four"):
            estimate_homography([Point(0, 0)] * 3, [Point(0, 0)] * 3)
        with self.assertRaisesRegex(ValueError, "equal length"):
            reprojection_errors([Point(0, 0)], [], ((1, 0, 0), (0, 1, 0), (0, 0, 1)))
        with self.assertRaisesRegex(ValueError, "outside the finite"):
            transform_point(Point(0, 1), ((1, 0, 0), (0, 1, 0), (1, 0, 0)))
        with self.assertRaisesRegex(ValueError, "equal-size"):
            detect_projected_dot(np.zeros((10, 10, 3), dtype=np.uint8),
                                 np.zeros((9, 10, 3), dtype=np.uint8))
        with self.assertRaisesRegex(ValueError, "degenerate"):
            estimate_homography([Point(x, 2 * x) for x in range(4)],
                                [Point(x, x * x) for x in range(4)])


if __name__ == "__main__":
    unittest.main()
