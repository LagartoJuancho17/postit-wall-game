"""Automatic calibration checks measured points near the physical borders."""

import unittest
from types import SimpleNamespace

import numpy as np

from contracts import Point
from services.runtime.session import Session
from services.runtime.tests.fixtures import (
    PROJECTOR_SIZE, camera_frame, known_mapping, mapped,
)
from services.vision import detect_board


def fake_session(*, bad_last_edge: bool = False):
    session = object.__new__(Session)
    session.projector = SimpleNamespace(width=PROJECTOR_SIZE[0],
                                        height=PROJECTOR_SIZE[1])
    session._camera_board = lambda: detect_board(camera_frame())
    inverse = np.linalg.inv(known_mapping())
    observations = []

    def projected_point(target):
        observations.append(target)
        point = mapped(target, inverse)
        if bad_last_edge and len(observations) == 17:
            return Point(point.x + 20, point.y)
        return point

    session._projected_point_camera = projected_point
    events = []
    session._log = lambda event, **fields: events.append((event, fields))
    return session, observations, events


class AutomaticCalibrationFlowTests(unittest.TestCase):
    def test_checks_four_points_near_marker_corners(self):
        session, observations, events = fake_session()
        _, detail = session._automatic_calibration()
        self.assertEqual(len(observations), 17)  # 9 fit + 4 middle + 4 edges
        self.assertIn("error máximo", detail)
        self.assertEqual(events[0][1]["check_points"], 8)
        self.assertLess(events[0][1]["max_edge_error_px"], 1)

    def test_edge_misalignment_rejects_automatic_calibration(self):
        session, observations, events = fake_session(bad_last_edge=True)
        with self.assertRaisesRegex(RuntimeError, "desvió"):
            session._automatic_calibration()
        self.assertEqual(len(observations), 17)
        self.assertGreater(events[0][1]["max_edge_error_px"], 5)


if __name__ == "__main__":
    unittest.main()
