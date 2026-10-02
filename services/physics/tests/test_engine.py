"""Fast, deterministic gate tests for continuous projector-space physics."""

import math
import unittest
from dataclasses import FrozenInstanceError

from contracts.model import Board, Point, Polygon
from services.physics import BALL_RADIUS_PX, GameStatus, launch, ready_state, step


def rectangle(left, top, right, bottom):
    return Polygon((Point(left, top), Point(right, top), Point(right, bottom), Point(left, bottom)))


def board(start=rectangle(40, 140, 60, 160), goal=rectangle(460, 140, 480, 160), bumpers=()):
    return Board(
        playfield=rectangle(0, 0, 500, 300),
        start=start,
        goal=goal,
        bumpers=bumpers,
        markers=(),
    )


class PhysicsTests(unittest.TestCase):
    def test_ready_centroid_is_motionless_and_immutable(self):
        initial = ready_state(board())
        self.assertEqual(initial.position, Point(50, 150))
        self.assertEqual(initial.velocity, Point(0, 0))
        self.assertEqual(initial.status, GameStatus.READY)
        self.assertEqual(BALL_RADIUS_PX, 10)
        with self.assertRaises(FrozenInstanceError):
            initial.bounce_count = 1

    def test_green_rectangle_reflects_at_circle_contact(self):
        field = board(bumpers=(rectangle(200, 100, 220, 200),))
        result = step(field, launch(field, 0, 100), 2.0)
        self.assertEqual(result.status, GameStatus.RUNNING)
        self.assertEqual(result.bounce_count, 1)
        self.assertAlmostEqual(result.position.x, 130, places=5)
        self.assertAlmostEqual(result.position.y, 150)
        self.assertAlmostEqual(result.velocity.x, -100)
        self.assertAlmostEqual(result.elapsed_s, 2)

    def test_orange_contact_wins_at_actual_impact_time(self):
        field = board(goal=rectangle(300, 140, 320, 160))
        result = step(field, launch(field, 0, 100), 3)
        self.assertEqual(result.status, GameStatus.WON)
        self.assertAlmostEqual(result.position.x, 290)
        self.assertAlmostEqual(result.elapsed_s, 2.4)
        self.assertEqual(step(field, result, 100), result)

    def test_crossing_playfield_inset_loses(self):
        field = board()
        result = step(field, launch(field, 180, 100), 1)
        self.assertEqual(result.status, GameStatus.LOST)
        self.assertAlmostEqual(result.position.x, 10)
        self.assertAlmostEqual(result.elapsed_s, 0.4)

    def test_corner_contact_reflects_both_components(self):
        field = board(
            start=rectangle(140, 40, 160, 60),
            bumpers=(rectangle(200, 100, 220, 120),),
        )
        result = step(field, launch(field, 45, math.sqrt(2) * 100), 0.5)
        self.assertEqual(result.status, GameStatus.RUNNING)
        self.assertEqual(result.bounce_count, 1)
        self.assertLess(result.velocity.x, 0)
        self.assertLess(result.velocity.y, 0)
        self.assertAlmostEqual(math.hypot(result.velocity.x, result.velocity.y), math.sqrt(2) * 100)

    def test_fast_ball_does_not_tunnel_through_green_note(self):
        field = board(bumpers=(rectangle(200, 100, 220, 200),))
        result = step(field, launch(field, 0, 50_000), 0.005)
        self.assertEqual(result.status, GameStatus.RUNNING)
        self.assertEqual(result.bounce_count, 1)
        self.assertLess(result.position.x, 190)
        self.assertLess(result.velocity.x, 0)

    def test_fast_ball_does_not_tunnel_through_goal(self):
        field = board(goal=rectangle(300, 140, 320, 160))
        result = step(field, launch(field, 0, 50_000), 0.01)
        self.assertEqual(result.status, GameStatus.WON)
        self.assertAlmostEqual(result.position.x, 290)

    def test_two_rebounds_are_resolved_within_one_frame(self):
        field = board(
            start=rectangle(240, 140, 260, 160),
            bumpers=(rectangle(200, 100, 220, 200), rectangle(300, 100, 320, 200)),
        )
        result = step(field, launch(field, 0, 100), 1.5)
        self.assertEqual(result.status, GameStatus.RUNNING)
        self.assertEqual(result.bounce_count, 2)
        self.assertAlmostEqual(result.position.x, 280, places=5)
        self.assertAlmostEqual(result.velocity.x, 100)
        self.assertAlmostEqual(result.elapsed_s, 1.5)

    def test_invalid_geometry_and_parameters_fail_early(self):
        with self.assertRaisesRegex(ValueError, "start overlaps a bumper"):
            ready_state(board(bumpers=(rectangle(45, 145, 55, 155),)))
        with self.assertRaisesRegex(ValueError, "speed_px_s"):
            launch(board(), 0, 0)
        with self.assertRaisesRegex(ValueError, "dt_s"):
            step(board(), launch(board(), 0, 100), float("nan"))


if __name__ == "__main__":
    unittest.main()
