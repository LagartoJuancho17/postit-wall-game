"""Gate tests: the one-parameter lens model of the alignment (contract 1.3.1)."""

from __future__ import annotations

import math
import unittest

from contracts.model import Point
from services.calibration import alignment_layout, decode_alignment
from services.calibration.lens import (LENS_K_LIMIT, distort, fit_lens, lens_for, undistort,
                                       valid_lens)
from services.calibration.tests.render import (capture_frames, film_through_lens, homography_from_quad,
                                               truth_errors)

CAMERA = (640, 360)
PROJECTOR = (1280, 720)
FULL_VIEW = ((20, 14), (620, 20), (626, 346), (14, 340))


class LensModelTests(unittest.TestCase):
    def test_lens_for_centers_the_model_and_normalizes_by_half_the_diagonal(self):
        self.assertEqual(lens_for(640, 480, 0.05), {"k": 0.05, "cx": 320.0, "cy": 240.0, "r": 400.0})

    def test_none_is_the_identity_both_ways(self):
        point = Point(12.5, -3.0)
        self.assertIs(undistort(point, None), point)
        self.assertIs(distort(point, None), point)

    def test_center_stays_and_corners_stretch_by_one_plus_k(self):
        lens = lens_for(640, 480, 0.1)
        self.assertEqual(undistort(Point(320, 240), lens), Point(320, 240))
        corner = undistort(Point(0, 0), lens)
        self.assertAlmostEqual(corner.x, 320 - 320 * 1.1)
        self.assertAlmostEqual(corner.y, 240 - 240 * 1.1)

    def test_distort_inverts_undistort_over_the_frame_for_every_allowed_k(self):
        for k in (-LENS_K_LIMIT, -0.1, -0.01, 0.0, 0.03, 0.1, LENS_K_LIMIT):
            lens = lens_for(1280, 720, k)
            for x in range(0, 1281, 160):
                for y in range(0, 721, 120):
                    back = distort(undistort(Point(x, y), lens), lens)
                    with self.subTest(k=k, x=x, y=y):
                        self.assertLess(math.hypot(back.x - x, back.y - y), 1e-6)

    def test_valid_lens(self):
        self.assertTrue(valid_lens(lens_for(640, 480, 0.2)))
        for bad in (None, {}, {"k": 0.1, "cx": 1, "cy": 1}, {**lens_for(640, 480, 0.3)},
                    {**lens_for(640, 480, 0.1), "r": 0}, {**lens_for(640, 480, 0.1), "k": math.nan},
                    {**lens_for(640, 480, 0.1), "cx": "a"}):
            with self.subTest(lens=bad):
                self.assertFalse(valid_lens(bad))


class FitLensTests(unittest.TestCase):
    def test_finds_the_minimum_of_a_smooth_cost_inside_the_limit(self):
        for target in (-0.2, -0.0337, 0.0, 0.0123, 0.07, 0.249):
            with self.subTest(target=target):
                self.assertAlmostEqual(fit_lens(lambda k: (k - target) ** 2), target, delta=1e-9)

    def test_finds_the_best_valley_of_a_noisy_cost(self):
        # RANSAC makes the real cost step-shaped; the grid keeps the right valley.
        cost = lambda k: abs(k - 0.08) + (0.5 if -0.15 < k < -0.1 else 1.0) * 0.001 * math.sin(400 * k) ** 2
        self.assertAlmostEqual(fit_lens(cost), 0.08, delta=0.002)

    def test_an_impossible_cost_means_no_lens(self):
        self.assertEqual(fit_lens(lambda k: math.inf), 0.0)

    def test_ties_keep_the_earliest_k(self):
        self.assertEqual(fit_lens(lambda k: 1.0), -LENS_K_LIMIT)


class DecodeWithLensTests(unittest.TestCase):
    def decode(self, k: float, quad=FULL_VIEW, **options):
        layout = alignment_layout(*PROJECTOR)
        G = homography_from_quad(PROJECTOR, quad)
        frames = capture_frames(layout, G, CAMERA, seed=5, **options)
        if k:
            frames = {key: film_through_lens(image, k) for key, image in frames.items()}
        return decode_alignment(frames, layout), G

    def test_a_straight_camera_decodes_without_a_lens(self):
        result, _ = self.decode(0.0)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["lens"])

    def test_wide_and_pincushion_lenses_are_fitted_and_the_mapping_matches_the_truth(self):
        for k in (0.04, 0.09, -0.06):
            result, G = self.decode(k)
            with self.subTest(k=k):
                self.assertTrue(result["ok"], result["reason"])
                self.assertEqual(set(result["lens"]), {"k", "cx", "cy", "r"})
                self.assertAlmostEqual(result["lens"]["k"], k, delta=0.003)
                errors = truth_errors(result["matrix"], G, CAMERA, PROJECTOR, lens=result["lens"], true_k=k)
                self.assertLess(float(errors.max()), 1.0)

    def test_without_the_lens_the_same_frames_fail(self):
        from services.calibration import coded
        original = coded.fit_lens
        coded.fit_lens = lambda cost: 0.0
        self.addCleanup(setattr, coded, "fit_lens", original)
        result, _ = self.decode(0.06)
        self.assertFalse(result["ok"])
        self.assertIn(result["code"], ("inaccurate", "inconsistent"))
        self.assertIsNone(result["lens"])

    def test_a_failure_the_lens_cannot_fix_keeps_its_code_and_no_lens(self):
        # Too few dots: step 10b never runs.
        result, _ = self.decode(0.06, quad=((250, 140), (390, 140), (390, 220), (250, 220)))
        self.assertFalse(result["ok"])
        self.assertIsNone(result["lens"])


if __name__ == "__main__":
    unittest.main()
