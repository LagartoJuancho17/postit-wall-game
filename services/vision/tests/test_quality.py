"""Gate tests: the frame measures that rank fixed camera settings."""

import unittest

import cv2
import numpy as np

from services.vision import build_palette, find_notes, frame_quality
from services.vision.tests.scene import scene


class FrameQualityTests(unittest.TestCase):
    def test_a_good_frame_is_ready_and_unclipped(self):
        image, _ = scene()
        quality = frame_quality(image)
        self.assertTrue(quality["ready"])
        self.assertEqual(quality["clipped"], 0.0)
        self.assertEqual(quality["dark"], 0.0)
        self.assertGreater(quality["note_saturation"], 200)

    def test_overexposure_clips_and_washes_out_the_notes(self):
        image, _ = scene()
        bright = np.clip(image.astype(np.float32) * 2.2, 0, 255).astype(np.uint8)
        normal, washed = frame_quality(image), frame_quality(bright)
        self.assertGreater(washed["clipped"], 0.5)
        self.assertLess(washed["note_saturation"] or 0, normal["note_saturation"])

    def test_underexposure_is_dark_and_not_ready(self):
        image, _ = scene()
        dark = (image.astype(np.float32) * 0.08).astype(np.uint8)
        quality = frame_quality(dark)
        self.assertGreater(quality["dark"], 0.9)
        self.assertFalse(quality["ready"])

    def test_brightness_reads_the_wall_exposure(self):
        image, _ = scene()  # wall gray 225
        self.assertAlmostEqual(frame_quality(image)["brightness"], 225 / 255, places=2)
        half = (image.astype(np.float32) * 0.5).astype(np.uint8)
        self.assertAlmostEqual(frame_quality(half)["brightness"], 112 / 255, places=2)

    def test_blur_lowers_sharpness(self):
        image, _ = scene()
        blurred = cv2.GaussianBlur(image, (21, 21), 0)
        self.assertLess(frame_quality(blurred)["sharpness"], frame_quality(image)["sharpness"] / 3)

    def test_nothing_detected_gives_no_note_saturation(self):
        wall = np.full((480, 640, 3), 200, np.uint8)
        quality = frame_quality(wall)
        self.assertIsNone(quality["note_saturation"])
        self.assertFalse(quality["ready"])

    def test_ready_follows_the_palette_and_reuses_given_notes(self):
        image, _ = scene(hues={"start": 105, "goal": 176, "bumper": 28, "marker": 140})
        self.assertFalse(frame_quality(image)["ready"])
        palette = build_palette({"start": "#2050f0", "goal": "#f01030",
                                 "bumper": "#f0d020", "marker": "#9030f0"})
        notes = find_notes(image, palette)
        self.assertEqual(frame_quality(image, palette, notes), frame_quality(image, palette))


if __name__ == "__main__":
    unittest.main()
