"""Runtime gate tests that do not touch a webcam or a physical projector."""

import math
import os
import unittest
from dataclasses import replace

# Must be set before pygame initializes its video/audio backends.
os.environ["SDL_VIDEODRIVER"] = "dummy"
os.environ["SDL_AUDIODRIVER"] = "dummy"

import pygame

from contracts import Board, Point, Polygon
from services.calibration import estimate_homography, reprojection_errors, transform_board
from services.physics import GameStatus, launch, ready_state, step
from services.runtime.projector import Projector, WHITE, YELLOW
from services.runtime.session import (
    board_fits_screen,
    calibration_points,
    choose_display,
    prepare_board,
)
from services.runtime.tests.fixtures import (
    GOAL_CENTER,
    MARKER_CENTERS,
    PROJECTOR_SIZE,
    START_CENTER,
    camera_frame,
    camera_observations,
    known_mapping,
    mapped,
)
from services.vision import detect_board


def square(x: float, y: float, side: float = 30) -> Polygon:
    return Polygon((Point(x, y), Point(x + side, y),
                    Point(x + side, y + side), Point(x, y + side)))


def simple_board() -> Board:
    playfield = Polygon((Point(100, 120), Point(1100, 120),
                         Point(1100, 680), Point(100, 680)))
    return Board(playfield, square(220, 330, 60), square(900, 350, 60),
                 (square(520, 220, 50),),
                 (square(85, 105), square(1085, 105),
                  square(1085, 665), square(85, 665)))


def center(polygon: Polygon) -> Point:
    return Point(sum(p.x for p in polygon.points) / len(polygon.points),
                 sum(p.y for p in polygon.points) / len(polygon.points))


class RuntimeGeometryTests(unittest.TestCase):
    def test_calibration_points_are_distinct_ordered_and_in_bounds(self):
        fit, checks = calibration_points(1000, 800)
        self.assertEqual(len(fit), 9)
        self.assertEqual(len(checks), 4)
        self.assertEqual(fit[0], Point(200, 160))
        self.assertEqual(fit[4], Point(500, 400))
        self.assertEqual(fit[-1], Point(800, 640))
        self.assertEqual(checks, (Point(350, 280), Point(650, 280),
                                  Point(350, 520), Point(650, 520)))
        self.assertEqual(len(set(fit + checks)), 13)
        self.assertTrue(all(0 < p.x < 1000 and 0 < p.y < 800 for p in fit + checks))

    def test_display_selection_and_invalid_indexes(self):
        self.assertEqual(choose_display([(1280, 800)], None), 0)
        self.assertEqual(choose_display([(1512, 982), (1280, 800)], None), 1)
        self.assertEqual(choose_display([(1512, 982), (1280, 800)], 0), 0)
        self.assertEqual(choose_display([(1512, 982), (1280, 800)], 1), 1)
        with self.assertRaisesRegex(RuntimeError, "No hay pantallas"):
            choose_display([], None)
        for bad_index in (-1, 2):
            with self.subTest(bad_index=bad_index):
                with self.assertRaisesRegex(ValueError, "no existe"):
                    choose_display([(1512, 982), (1280, 800)], bad_index)

    def test_screen_bounds_check_every_game_polygon(self):
        board = simple_board()
        self.assertTrue(board_fits_screen(board, 1280, 800))
        outside = (
            replace(board, playfield=Polygon((Point(-0.01, 120), *board.playfield.points[1:]))),
            replace(board, start=square(-31, 330)),
            replace(board, goal=square(1280, 350)),
            replace(board, bumpers=(square(520, 800),)),
        )
        for candidate in outside:
            with self.subTest(polygon=candidate):
                self.assertFalse(board_fits_screen(candidate, 1280, 800))
        self.assertFalse(board_fits_screen(board, 0, 800))

    def test_synthetic_camera_to_projector_to_ready_ball(self):
        projector_size = PROJECTOR_SIZE
        fit, checks = calibration_points(*projector_size)
        matrix = estimate_homography(camera_observations(fit), fit)
        self.assertLess(max(reprojection_errors(camera_observations(checks), checks, matrix)), 0.02)

        camera_board = detect_board(camera_frame(speckles=True))
        board = prepare_board(camera_board, matrix, *projector_size)
        self.assertTrue(board_fits_screen(board, *projector_size))
        self.assertEqual(len(board.bumpers), 1)
        for actual, source in zip(board.playfield.points, MARKER_CENTERS):
            expected = mapped(Point(*source), known_mapping())
            self.assertLess(math.hypot(actual.x - expected.x, actual.y - expected.y), 3)

        state = ready_state(board)
        self.assertEqual(state.status, GameStatus.READY)
        expected_start = mapped(Point(*START_CENTER), known_mapping())
        self.assertLess(math.hypot(state.position.x - expected_start.x,
                                   state.position.y - expected_start.y), 3)
        expected_goal = mapped(Point(*GOAL_CENTER), known_mapping())
        actual_goal = center(board.goal)
        self.assertLess(math.hypot(actual_goal.x - expected_goal.x,
                                   actual_goal.y - expected_goal.y), 3)
        running = step(board, launch(board, 0, 300), 0.05)
        self.assertEqual(running.status, GameStatus.RUNNING)
        self.assertGreater(running.position.x, state.position.x)

    def test_prepare_board_rejects_offscreen_and_unplayable_scan(self):
        camera_board = detect_board(camera_frame())
        shifted_offscreen = ((1.0, 0.0, -500.0),
                             (0.0, 1.0, 0.0),
                             (0.0, 0.0, 1.0))
        with self.assertRaisesRegex(RuntimeError, "fuera del área proyectada"):
            prepare_board(camera_board, shifted_offscreen, *PROJECTOR_SIZE)

        fit, _ = calibration_points(*PROJECTOR_SIZE)
        matrix = estimate_homography(camera_observations(fit), fit)
        impossible = replace(camera_board, bumpers=(camera_board.start,))
        with self.assertRaisesRegex(ValueError, "start overlaps a bumper"):
            prepare_board(impossible, matrix, *PROJECTOR_SIZE)

    def test_rescan_between_attempts_changes_green_geometry(self):
        fit, _ = calibration_points(*PROJECTOR_SIZE)
        matrix = estimate_homography(camera_observations(fit), fit)
        first = transform_board(detect_board(camera_frame(green_center=(405, 190))), matrix)
        second = transform_board(detect_board(camera_frame(green_center=(515, 245))), matrix)
        self.assertEqual(ready_state(first).status, GameStatus.READY)
        self.assertEqual(ready_state(second).status, GameStatus.READY)
        self.assertEqual(first.start, second.start)
        previous = center(first.bumpers[0])
        current = center(second.bumpers[0])
        self.assertGreater(math.hypot(previous.x - current.x,
                                      previous.y - current.y), 100)


class ProjectorSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.init()
        cls.screen = pygame.display.set_mode(PROJECTOR_SIZE)
        cls.projector = Projector(cls.screen)

    @classmethod
    def tearDownClass(cls):
        pygame.quit()

    def test_preview_and_game_draw_to_dummy_display(self):
        board = simple_board()
        self.projector.preview(board, "Calibración sintética: 0 px")
        self.assertEqual(self.screen.get_at((250, 330))[:3], YELLOW)
        self.projector.game(board, ready_state(board), 0)
        self.assertEqual(self.screen.get_at((250, 360))[:3], WHITE)


if __name__ == "__main__":
    unittest.main()
