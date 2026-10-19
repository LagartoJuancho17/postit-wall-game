"""Every discovered regression belongs to exactly one deterministic gate lane."""
import unittest
from scripts.gate_tests import lane_for, test_cases


class GateLaneTests(unittest.TestCase):
    def test_lane_partition_runs_every_test_once(self):
        ids = [case.id() for case in test_cases(unittest.defaultTestLoader.discover('.'))]
        lanes = {name: {case for case in ids if lane_for(case) == name} for name in ('web', 'core')}
        self.assertTrue(lanes['web'])
        self.assertTrue(lanes['core'])
        self.assertFalse(lanes['web'] & lanes['core'])
        self.assertEqual(lanes['web'] | lanes['core'], set(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_web_boundary_is_exact_and_import_failures_are_not_skipped(self):
        self.assertEqual(lane_for('services.web.tests.test_live_detection.LiveTests.test_launch'), 'web')
        self.assertEqual(lane_for('services.vision.tests.test_detector.DetectionTests.test_frame'), 'core')
        self.assertEqual(lane_for('unittest.loader._FailedTest.broken_module'), 'core')
        self.assertEqual(lane_for('tests.test_web_http_integration.HttpTests.test_projector'), 'core')
