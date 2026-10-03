"""Gate tests: UVC readings and writes in the panel's camera vocabulary.

c920_raw.json is a real reading of the C920 on this Mac (all automatics on).
"""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from services.uvc import mapping

RAW = json.loads((Path(__file__).parent / "c920_raw.json").read_text())


class CapabilityTests(unittest.TestCase):
    def test_c920_offers_every_control_by_hand(self) -> None:
        caps = mapping.capabilities(RAW)
        self.assertEqual(caps["whiteBalanceMode"], ["manual", "continuous"])
        self.assertEqual(caps["exposureMode"], ["manual", "continuous"])  # AE bitmap 9 = manual + aperture
        self.assertEqual(caps["focusMode"], ["manual", "continuous"])
        self.assertEqual(caps["colorTemperature"], {"min": 2000, "max": 6500, "step": 1})
        self.assertEqual(caps["exposureTime"], {"min": 3, "max": 2047, "step": 1})
        self.assertEqual(caps["focusDistance"], {"min": 0, "max": 250, "step": 5})
        self.assertEqual(caps["gain"], {"min": 0, "max": 255, "step": 1})

    def test_missing_or_read_only_controls_are_left_out(self) -> None:
        raw = copy.deepcopy(RAW)
        del raw["controls"]["focus_auto"]
        raw["controls"]["wb_temperature"]["settable"] = False
        raw["controls"]["ae_mode"]["res"] = 8  # only aperture priority
        caps = mapping.capabilities(raw)
        self.assertNotIn("focusMode", caps)
        self.assertNotIn("colorTemperature", caps)
        self.assertEqual(caps["exposureMode"], ["continuous"])

    def test_settings_read_the_automatics_as_continuous(self) -> None:
        current = mapping.settings(RAW)
        self.assertEqual([current["whiteBalanceMode"], current["exposureMode"], current["focusMode"]],
                         ["continuous", "continuous", "continuous"])
        self.assertEqual((current["colorTemperature"], current["exposureTime"]), (5794, 166))


class PlanTests(unittest.TestCase):
    def test_automatics_go_off_before_any_value(self) -> None:
        writes, skipped = mapping.plan({"colorTemperature": 4000, "whiteBalanceMode": "manual",
                                        "exposureTime": 200, "exposureMode": "manual",
                                        "focusMode": "manual", "focusDistance": 37,
                                        "brightness": 128, "gain": 3}, RAW)
        self.assertEqual(writes[:3], [("wb_auto", 0), ("ae_mode", 1), ("focus_auto", 0)])
        self.assertEqual(dict(writes[3:]), {"wb_temperature": 4000, "exposure": 200, "focus": 35,
                                            "brightness": 128, "gain": 3})
        self.assertEqual(skipped, [])

    def test_values_are_clamped_and_snapped_to_the_camera_steps(self) -> None:
        writes, _ = mapping.plan({"focusMode": "manual", "focusDistance": 999,
                                  "exposureMode": "manual", "exposureTime": 0.4}, RAW)
        self.assertIn(("focus", 250), writes)
        self.assertIn(("exposure", 3), writes)

    def test_a_value_whose_automatic_stays_on_is_skipped(self) -> None:
        # The camera rejects a white balance temperature while auto white balance is on.
        writes, skipped = mapping.plan({"colorTemperature": 4000}, RAW)
        self.assertEqual(writes, [])
        self.assertEqual(skipped, ["colorTemperature"])
        manual = copy.deepcopy(RAW)
        manual["controls"]["wb_auto"]["cur"] = 0
        self.assertEqual(mapping.plan({"colorTemperature": 4000}, manual), ([("wb_temperature", 4000)], []))

    def test_controls_the_camera_lacks_are_skipped_not_sent(self) -> None:
        raw = copy.deepcopy(RAW)
        del raw["controls"]["focus_auto"]
        del raw["controls"]["focus"]
        writes, skipped = mapping.plan({"focusMode": "manual", "focusDistance": 30}, raw)
        self.assertEqual(writes, [])
        self.assertEqual(skipped, ["focusMode", "focusDistance"])


if __name__ == "__main__":
    unittest.main()
