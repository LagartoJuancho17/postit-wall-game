"""Fast, deterministic gate tests for continuous projector-space physics."""

import math
import unittest
from dataclasses import FrozenInstanceError, replace

from contracts.model import Board, Point, Polygon, Portal
from services.physics import BALL_RADIUS_PX, DROP_GAP_PX, GameStatus, drop, drop_point, drop_ready_state, launch, ready_state, step
from services.physics.engine import STALL_S, STILL_SPEED_PX_S


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


class GravityTests(unittest.TestCase):
    """The web game's ball falls down the wall (+y)."""

    G = 400.0

    def _fly(self, game, state, dt, seconds):
        for _ in range(round(seconds / dt)):
            state = step(game, state, dt)
        return state

    def test_free_flight_lands_exactly_on_the_parabola_whatever_the_step(self):
        game = board()
        start = launch(game, -30, 200.0, self.G)
        vx, vy = 200 * math.cos(math.radians(-30)), 200 * math.sin(math.radians(-30))
        for dt in (1 / 120, 1 / 30):
            state = self._fly(game, start, dt, 0.5)
            self.assertEqual(state.status, GameStatus.RUNNING)
            self.assertAlmostEqual(state.position.x, 50 + vx * 0.5, places=6)
            self.assertAlmostEqual(state.position.y, 150 + vy * 0.5 + 0.5 * self.G * 0.25, places=6)
            self.assertAlmostEqual(state.velocity.y, vy + self.G * 0.5, places=6)

    def test_a_ball_falls_off_the_bottom_and_is_lost(self):
        game = board()
        state = self._fly(game, launch(game, 0, 20.0, self.G), 1 / 120, 3.0)
        self.assertEqual(state.status, GameStatus.LOST)
        self.assertGreater(state.position.y, 280)   # the bottom edge, not a side

    def test_a_bounce_keeps_eighty_percent_of_the_normal_speed(self):
        floor = rectangle(20, 220, 480, 260)
        game = board(bumpers=(floor,))
        state = launch(game, 90, 50.0, self.G)   # straight down onto the floor
        before = None
        for _ in range(240):
            previous, state = state, step(game, state, 1 / 120)
            if state.bounce_count == 1:
                before = previous
                break
        self.assertIsNotNone(before, "the ball reached the floor")
        # Hit speed from energy: v^2 = v0^2 + 2 g h, h = floor top - radius - start.
        h = 220 - BALL_RADIUS_PX - 150
        speed = math.sqrt(50 ** 2 + 2 * self.G * h)
        apex = state
        while apex.velocity.y < 0:
            apex = step(game, apex, 1 / 240)
        rebound = math.sqrt(2 * self.G * (220 - BALL_RADIUS_PX - apex.position.y))
        self.assertAlmostEqual(rebound / speed, 0.8, delta=0.02)

    def test_a_ball_settles_on_a_floor_without_endless_bounces_then_ends_once_still(self):
        floor = rectangle(20, 220, 480, 260)
        game = board(bumpers=(floor,))
        state = launch(game, 90, 50.0, self.G)
        settled = None   # when the ball last went still (a bounce's apex is still for a moment too)
        for _ in range(round(25 * 120)):
            previous, state = state, step(game, state, 1 / 120)
            if previous.still_s == 0 and state.still_s > 0:
                settled = previous.elapsed_s
            if state.status != GameStatus.RUNNING:
                break
        self.assertEqual(state.status, GameStatus.LOST, "a ball resting on a note has stopped: the attempt ends")
        self.assertIsNotNone(settled)
        # Lost STALL_S after it went still, long before MAX_ATTEMPT_S.
        self.assertAlmostEqual(state.elapsed_s - settled, STALL_S, delta=2 / 120)
        self.assertLess(state.elapsed_s, 10.0)
        self.assertAlmostEqual(state.position.y, 220 - BALL_RADIUS_PX, delta=0.01, msg="it rests on the floor")
        self.assertLess(state.bounce_count, 30, "resting contact does not count as bounces")

    def test_without_gravity_motion_is_straight_and_elastic_as_before(self):
        game = board()
        state = step(game, launch(game, 0, 100.0), 1.0)
        self.assertEqual(state.position, Point(150, 150))
        self.assertEqual(state.gravity_px_s2, 0.0)

    def test_gravity_must_be_finite_and_not_negative(self):
        for bad in (-1.0, math.inf, math.nan):
            with self.assertRaises(ValueError):
                launch(board(), 0, 100.0, bad)


class BallisticAimTests(unittest.TestCase):
    def test_the_aim_passes_through_the_target_on_both_sides(self):
        from services.physics import ballistic_aim_degrees
        g, v = 400.0, 300.0
        for dx, dy in ((200, 0), (150, -60), (-180, 40), (120, 100)):
            aim = ballistic_aim_degrees(dx, dy, v, g)
            self.assertIsNotNone(aim, (dx, dy))
            vx, vy = v * math.cos(math.radians(aim)), v * math.sin(math.radians(aim))
            t = dx / vx
            self.assertGreater(t, 0)
            self.assertAlmostEqual(vy * t + 0.5 * g * t * t, dy, places=6)

    def test_out_of_reach_and_no_gravity(self):
        from services.physics import ballistic_aim_degrees
        self.assertIsNone(ballistic_aim_degrees(5000, 0, 100, 400))
        self.assertAlmostEqual(ballistic_aim_degrees(10, 10, 100, 0), 45.0)


class DropTests(unittest.TestCase):
    """The web game drops the ball from rest under the start note."""

    G = 400.0

    def test_the_drop_point_is_under_the_start_below_its_centroid(self):
        game = board()
        self.assertEqual(drop_point(game), Point(50.0, 160 + BALL_RADIUS_PX + DROP_GAP_PX))
        ready = drop_ready_state(game)
        self.assertEqual((ready.status, ready.velocity, ready.elapsed_s), (GameStatus.READY, Point(0.0, 0.0), 0.0))

    def test_a_slanted_start_still_leaves_the_ball_clear_of_it(self):
        slanted = Polygon((Point(40, 140), Point(80, 150), Point(76, 190), Point(36, 180)))
        position = drop_point(board(start=slanted))
        corners = slanted.points
        distance = min(math.dist((position.x, position.y), (p.x, p.y)) for p in corners)
        self.assertGreater(distance, BALL_RADIUS_PX)
        game = board(start=slanted)
        state = drop(game, self.G)
        self.assertEqual(state.status, GameStatus.RUNNING)

    def test_a_drop_falls_straight_on_the_parabola_from_rest(self):
        game = Board(playfield=rectangle(0, 0, 500, 3000), start=rectangle(40, 140, 60, 160),
                     goal=rectangle(460, 2800, 480, 2820), bumpers=(), markers=())
        y0 = drop_point(game).y
        for dt in (1 / 120, 1 / 30):
            state = drop(game, self.G)
            for k in range(1, round(1.0 / dt) + 1):
                state = step(game, state, dt)
            self.assertAlmostEqual(state.position.x, 50.0, places=9)
            self.assertAlmostEqual(state.position.y, y0 + 0.5 * self.G * 1.0, places=6)

    def test_a_goal_under_the_start_is_reached_by_the_bare_drop(self):
        game = board(goal=rectangle(40, 260, 60, 280))
        state = drop(game, self.G)
        while state.status == GameStatus.RUNNING:
            state = step(game, state, 1 / 120)
        self.assertEqual(state.status, GameStatus.WON)

    def test_a_ball_wedged_in_a_v_stops_and_the_attempt_ends_without_an_error(self):
        # Regression (2026-10-26): the gravity eval wedged a slow ball between
        # two tilted notes; resting on one face pushed it into the other, over
        # and over, until step() raised "more than 1024 collisions".
        left = Polygon((Point(10, 200), Point(50, 250), Point(50, 270), Point(10, 220)))
        right = Polygon((Point(50, 250), Point(90, 200), Point(90, 220), Point(50, 270)))
        game = board(bumpers=(left, right))
        state = drop(game, self.G)
        for _ in range(round(10 * 120)):
            state = step(game, state, 1 / 120)
            if state.status != GameStatus.RUNNING:
                break
        self.assertEqual(state.status, GameStatus.LOST)
        self.assertLess(state.elapsed_s, 10.0, "lost STALL_S after it stopped, not at MAX_ATTEMPT_S")
        self.assertGreaterEqual(state.still_s, STALL_S)
        self.assertLess(abs(state.position.x - 50.0), 1e-6, "it rests in the crease, between both faces")

    def test_a_ball_dropped_onto_a_symmetric_v_never_passes_through_a_face(self):
        # Regression (2026-10-26): touching both faces at the same instant, only
        # one was answered and the ball tunneled through the other.
        left = Polygon((Point(0, 150), Point(50, 250), Point(50, 275), Point(0, 175)))
        right = Polygon((Point(50, 250), Point(100, 150), Point(100, 175), Point(50, 275)))
        game = board(bumpers=(left, right))
        state = drop(game, self.G)
        while state.status == GameStatus.RUNNING:
            state = step(game, state, 1 / 120)
            self.assertLess(state.position.y, 250 - BALL_RADIUS_PX + 1e-6, "above the crease, never below an arm")

    def test_drop_needs_positive_gravity_and_room_under_the_start(self):
        for gravity in (0.0, -1.0, math.inf, math.nan):
            with self.subTest(gravity=gravity), self.assertRaises(ValueError):
                drop(board(), gravity)
        with self.assertRaisesRegex(ValueError, "under the start note"):
            drop_ready_state(board(start=rectangle(40, 270, 60, 290)))
        with self.assertRaisesRegex(ValueError, "start overlaps a bumper"):
            drop_ready_state(board(bumpers=(rectangle(30, 165, 70, 185),)))

    def test_the_still_clock_counts_only_while_slower_than_the_threshold(self):
        game = board()
        state = drop(game, self.G)
        state = step(game, state, 1 / 120)
        self.assertGreater(self.G / 120, STILL_SPEED_PX_S * 0)   # moving from the first step on
        self.assertEqual(state.still_s, 0.0 if self.G / 120 >= STILL_SPEED_PX_S else 1 / 120)


class LevelObstacleTests(unittest.TestCase):
    """Contract 3.2: a level's walls bounce, spikes lose, portals move the ball."""

    G = 480.0

    def field(self, **obstacles):
        return replace(board(start=rectangle(40, 20, 60, 40), goal=rectangle(400, 260, 420, 280)), **obstacles)

    def run_drop(self, game, seconds=6.0):
        state = drop(game, self.G)
        for _ in range(int(seconds * 120)):
            state = step(game, state, 1 / 120)
            if state.status != GameStatus.RUNNING:
                break
        return state

    def test_a_wall_bounces_like_a_bumper_and_counts_as_a_bounce(self):
        game = self.field(walls=(rectangle(20, 150, 80, 160),))
        state = drop(game, self.G)
        for _ in range(120 * 3):
            state = step(game, state, 1 / 120)
            if state.bounce_count:
                break
        self.assertEqual(state.bounce_count, 1)
        self.assertLess(state.velocity.y, 0, "it goes back up after the wall")
        self.assertLessEqual(state.position.y, 150 - BALL_RADIUS_PX + 1e-6)

    def test_touching_spikes_loses_at_the_touch_with_its_reason(self):
        state = self.run_drop(self.field(spikes=(rectangle(20, 150, 80, 160),)))
        self.assertEqual((state.status, state.ended_by), (GameStatus.LOST, "spikes"))
        self.assertAlmostEqual(state.position.y, 150 - BALL_RADIUS_PX, places=6)

    def test_every_end_names_why(self):
        self.assertEqual(self.run_drop(board(start=rectangle(40, 20, 60, 40))).ended_by, "exit")
        won = self.run_drop(board(start=rectangle(390, 20, 410, 40), goal=rectangle(390, 200, 420, 230)))
        self.assertEqual((won.status, won.ended_by), (GameStatus.WON, "goal"))
        flat = self.run_drop(self.field(walls=(rectangle(0, 150, 500, 160),)), seconds=30)
        self.assertEqual((flat.status, flat.ended_by), (GameStatus.LOST, "stall"))
        self.assertEqual(drop(board(), self.G).ended_by, "")

    def test_a_portal_moves_the_ball_to_its_exit_with_the_same_velocity(self):
        portal = Portal(rectangle(20, 150, 80, 160), Point(410, 120))
        game = self.field(portals=(portal,))
        state = drop(game, self.G)
        before = None
        while state.status == GameStatus.RUNNING and state.position.x < 100:
            before = state
            state = step(game, state, 1 / 120)
        self.assertEqual(state.status, GameStatus.RUNNING)
        self.assertAlmostEqual(state.position.x, 410)
        self.assertGreater(state.position.y, 120)
        self.assertAlmostEqual(state.velocity.y, before.velocity.y + self.G / 120, places=6)
        final = self.run_drop(game)
        self.assertEqual((final.status, final.ended_by), (GameStatus.WON, "goal"))

    def test_the_ball_at_rest_may_not_touch_a_level_obstacle(self):
        start = rectangle(40, 20, 60, 40)
        under = rectangle(30, 45, 70, 60)
        for field, reason in ((dict(walls=(under,)), "start overlaps a wall"),
                              (dict(spikes=(under,)), "start overlaps spikes"),
                              (dict(portals=(Portal(under, Point(400, 100)),)), "start overlaps a portal")):
            with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                drop_ready_state(replace(board(start=start), **field))

    def test_obstacles_are_validated_like_notes(self):
        bent = Polygon((Point(100, 100), Point(200, 100), Point(150, 120), Point(150, 200)))
        for field, label in ((dict(walls=(bent,)), "wall 0"), (dict(spikes=(bent,)), "spikes 0"),
                             (dict(portals=(Portal(bent, Point(1, 1)),)), "portal 0"),
                             (dict(portals=(Portal(rectangle(100, 100, 120, 120), Point(math.nan, 1)),)),
                              "portal 0 must have a finite exit")):
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, label):
                drop_ready_state(replace(board(), **field))

    def test_a_board_without_obstacles_moves_bit_for_bit_as_before(self):
        plain = board(start=rectangle(40, 20, 60, 40), bumpers=(rectangle(20, 150, 80, 160),))
        empty = replace(plain, walls=(), spikes=(), portals=())
        a, b = drop(plain, self.G), drop(empty, self.G)
        for _ in range(600):
            a, b = step(plain, a, 1 / 120), step(empty, b, 1 / 120)
        self.assertEqual(a, b)
