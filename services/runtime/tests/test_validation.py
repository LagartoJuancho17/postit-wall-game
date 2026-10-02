"""Regression tests for invalid physical layouts caught before play."""

import unittest
from dataclasses import replace

from contracts import Board, Point, Polygon
from services.runtime.session import prepare_board


IDENTITY = ((1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0))


def rectangle(left: float, top: float, right: float, bottom: float) -> Polygon:
    return Polygon((Point(left, top), Point(right, top),
                    Point(right, bottom), Point(left, bottom)))


def board() -> Board:
    playfield = rectangle(100, 100, 540, 440)
    return Board(
        playfield=playfield,
        start=rectangle(150, 230, 190, 270),
        goal=rectangle(430, 230, 470, 270),
        bumpers=(rectangle(300, 180, 330, 220),),
        markers=(rectangle(80, 80, 120, 120),
                 rectangle(520, 80, 560, 120),
                 rectangle(520, 420, 560, 460),
                 rectangle(80, 420, 120, 460)),
    )


class PrepareBoardTests(unittest.TestCase):
    def test_valid_scan_is_playable(self):
        self.assertEqual(prepare_board(board(), IDENTITY, 640, 480), board())

    def test_start_too_close_to_edge_is_rejected_before_game_loop(self):
        invalid = replace(board(), start=rectangle(95, 230, 115, 250))
        with self.assertRaisesRegex(ValueError, "start centroid must fit"):
            prepare_board(invalid, IDENTITY, 640, 480)

    def test_start_overlapping_bumper_is_rejected_before_game_loop(self):
        invalid = replace(board(), start=rectangle(300, 180, 330, 220))
        with self.assertRaisesRegex(ValueError, "start overlaps a bumper"):
            prepare_board(invalid, IDENTITY, 640, 480)

    def test_note_outside_projection_is_rejected_before_game_loop(self):
        invalid = replace(board(), goal=rectangle(625, 230, 655, 270))
        with self.assertRaisesRegex(RuntimeError, "fuera del área proyectada"):
            prepare_board(invalid, IDENTITY, 640, 480)


if __name__ == "__main__":
    unittest.main()
