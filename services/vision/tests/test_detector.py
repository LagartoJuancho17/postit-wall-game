"""Gate tests: segmentation, geometry, and actionable failures."""

import unittest

import cv2
import numpy as np

from services.vision import DetectionError, detect_board, find_notes
from services.vision.tests.scene import HEIGHT, WIDTH, bgr, draw_note, scene


def center(polygon):
    return np.array(
        [
            sum(point.x for point in polygon.points) / len(polygon.points),
            sum(point.y for point in polygon.points) / len(polygon.points),
        ]
    )


class DetectorTests(unittest.TestCase):
    def test_rotated_notes_perspective_and_speckles(self):
        image, expected = scene(perspective=True, speckles=True)
        board = detect_board(image)
        self.assertEqual(len(board.markers), 4)
        self.assertEqual(len(board.bumpers), 2)
        self.assertLess(np.linalg.norm(center(board.start) - expected["start"]), 5)
        self.assertLess(np.linalg.norm(center(board.goal) - expected["goal"]), 5)
        for actual, target in zip(board.playfield.points, expected["markers"]):
            self.assertLess(np.linalg.norm((actual.x, actual.y) - target), 5)
        for actual, target in zip(board.markers, expected["markers"]):
            self.assertLess(np.linalg.norm(center(actual) - target), 5)
        self.assertTrue(
            any(
                abs(polygon.points[1].x - polygon.points[0].x) > 3
                and abs(polygon.points[1].y - polygon.points[0].y) > 3
                for polygon in board.bumpers
            )
        )

    def test_dim_scene_without_green_is_valid(self):
        image, _ = scene(brightness=0.55, greens=0)
        self.assertEqual(detect_board(image).bumpers, ())

    def test_missing_start_is_reported(self):
        image, _ = scene()
        image[275:385, 120:230] = 225
        with self.assertRaises(DetectionError) as caught:
            detect_board(image)
        self.assertEqual((caught.exception.role, caught.exception.found), ("start", 0))

    def test_extra_goal_is_ambiguous(self):
        image, _ = scene()
        draw_note(image, (650, 130), 12)
        with self.assertRaises(DetectionError) as caught:
            detect_board(image)
        self.assertEqual((caught.exception.role, caught.exception.found), ("goal", 2))

    def test_extra_marker_is_ambiguous(self):
        image, _ = scene()
        draw_note(image, (500, 100), 157)
        with self.assertRaises(DetectionError) as caught:
            detect_board(image)
        self.assertEqual((caught.exception.role, caught.exception.found), ("marker", 5))

    def test_missing_marker_is_reported(self):
        image, _ = scene()
        image[50:130, 50:130] = 225
        with self.assertRaises(DetectionError) as caught:
            detect_board(image)
        self.assertEqual((caught.exception.role, caught.exception.found), ("marker", 3))

    def test_perspective_marker_center_is_diagonal_intersection(self):
        image, _ = scene()
        image[48:140, 45:140] = 225
        trapezoid = np.int32([[52, 58], [130, 58], [113, 134], [69, 134]])
        cv2.fillConvexPoly(image, trapezoid, bgr(157))
        board = detect_board(image)
        observed = board.playfield.points[0]
        # The midpoint of the four visible corners has y=96. A planar
        # rectangle's physical center projects to its diagonal crossing.
        expected_y = 58 + 76 * (78 / 122)
        self.assertAlmostEqual(observed.x, 91, delta=2)
        self.assertAlmostEqual(observed.y, expected_y, delta=2)
        self.assertGreater(abs(observed.y - 96), 8)

    def test_a_wall_tinted_like_a_note_is_not_a_giant_note(self):
        # Regression: under a strongly wrong white balance the whole wall
        # turned orange and counted as one goal note, so a broken frame
        # looked ready. A blob over 5% of the frame is never a post-it.
        image, _ = scene()
        wall = np.all(image == 225, axis=2)
        image[wall] = bgr(14, 150)
        with self.assertRaises(DetectionError) as caught:
            detect_board(image)
        self.assertEqual(caught.exception.role, "goal")
        self.assertEqual(caught.exception.found, 0)

    def test_the_note_size_cap_sits_at_five_percent_of_the_frame(self):
        image, _ = scene()
        image[275:385, 120:230] = 225  # remove the regular start note
        side = int((WIDTH * HEIGHT * 0.045) ** 0.5)  # 4.5% of the frame
        draw_note(image, (480, 380), 30, (side, side))
        self.assertEqual(len(find_notes(image)["start"]), 1)
        image[380 - side:380 + side, 480 - side:480 + side] = 225
        side = int((WIDTH * HEIGHT * 0.055) ** 0.5)  # 5.5% of the frame
        draw_note(image, (480, 380), 30, (side, side))
        self.assertEqual(len(find_notes(image)["start"]), 0)

    def test_invalid_frame_is_rejected(self):
        for invalid in (None, np.zeros((100, 100), np.uint8), np.zeros((100, 100, 3), np.float32)):
            with self.assertRaises(ValueError):
                detect_board(invalid)


if __name__ == "__main__":
    unittest.main()
