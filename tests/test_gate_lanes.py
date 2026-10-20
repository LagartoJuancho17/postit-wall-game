"""Every discovered regression belongs to exactly one deterministic gate lane."""
import unittest
from scripts.gate_tests import LANES, lane_for, test_cases, test_modules


class GateLaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ids = [case.id() for case in test_cases(unittest.defaultTestLoader.discover('.'))]

    def test_lanes_import_exactly_the_modules_discover_finds(self):
        discovered = {".".join(case.split(".")[:-2]) for case in self.ids}
        self.assertEqual(set(test_modules()), discovered)

    def test_lane_partition_runs_every_test_once(self):
        ids = self.ids
        lanes = {name: {case for case in ids if lane_for(case) == name} for name in LANES}
        for name in LANES:
            self.assertTrue(lanes[name], f"lane {name} is empty")
        for index, first in enumerate(LANES):
            for second in LANES[index + 1:]:
                self.assertFalse(lanes[first] & lanes[second], (first, second))
        self.assertEqual(set().union(*lanes.values()), set(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_lane_boundaries_are_exact_and_import_failures_are_not_skipped(self):
        self.assertEqual(lane_for('services.web.tests.test_contract_matrix.StateMatrixContractTests.test_x'), 'matrix')
        self.assertEqual(lane_for('services.web.tests.test_live_detection.LiveTests.test_launch'), 'live')
        self.assertEqual(lane_for('services.web.tests.test_alignment_api.AlignmentLifecycleTests.test_x'), 'live')
        self.assertEqual(lane_for('services.web.tests.test_palette_api.PaletteTests.test_x'), 'web')
        self.assertEqual(lane_for('services.web.tests.test_live_detection_extra.Tests.test_x'), 'web')
        self.assertEqual(lane_for('services.vision.tests.test_detector.DetectionTests.test_frame'), 'vision')
        self.assertEqual(lane_for('services.simulation.tests.test_model.LoopTests.test_x'), 'sim')
        self.assertEqual(lane_for('services.calibration.tests.test_coded.DecodeTests.test_x'), 'sim')
        self.assertEqual(lane_for('tests.test_gate_lanes.GateLaneTests.test_x'), 'web')
        self.assertEqual(lane_for('services.runtime.tests.test_runtime.RuntimeTests.test_x'), 'core')
        self.assertEqual(lane_for('unittest.loader._FailedTest.broken_module'), 'core')
        self.assertEqual(lane_for('tests.test_web_http_integration.HttpTests.test_projector'), 'core')

    def test_the_hook_runs_every_lane(self):
        with open('.githooks/pre-commit', encoding='utf-8') as stream:
            hook = stream.read()
        loops = [line.split(" in ", 1)[1].rstrip("; do").split() for line in hook.splitlines()
                 if line.startswith("for lane in ")]
        self.assertEqual(len(loops), 2, "one loop starts the lanes, one prints them")
        for loop in loops:
            self.assertEqual(sorted(loop), sorted(LANES))
        self.assertIn('scripts/gate_tests.py "$lane"', hook)
