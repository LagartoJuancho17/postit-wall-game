"""Gate tests for the levels (contract 3.2): the file, the geometry, the checks."""

import json
import tempfile
import unittest
from pathlib import Path

from contracts.model import Point, Polygon
from services.levels import (BALL_RADIUS_PX, FREE_LEVEL, LEVELS, inside, level_by_id, level_check, level_geometry,
                             level_problem, load_levels, next_level, overlaps, with_obstacles)
from services.levels.design import notes_for, play, square
from services.physics import BALL_RADIUS_PX as PHYSICS_RADIUS


def rect(x0, y0, x1, y1):
    return Polygon((Point(x0, y0), Point(x1, y0), Point(x1, y1), Point(x0, y1)))


class LevelFileTests(unittest.TestCase):
    def test_the_file_loads_with_the_free_level_first_and_unique_ids(self):
        self.assertEqual(LEVELS[0].id, FREE_LEVEL)
        self.assertEqual(len({level.id for level in LEVELS}), len(LEVELS))
        self.assertGreaterEqual(len(LEVELS), 6)
        free = level_by_id(FREE_LEVEL)
        self.assertIsNone(free.start_zone)
        self.assertIsNone(free.bumpers_max)

    def test_levels_follow_in_file_order_and_the_last_and_free_have_none_after(self):
        playable = [level for level in LEVELS if level.id != FREE_LEVEL]
        for current, upcoming in zip(playable, playable[1:]):
            self.assertIs(next_level(current), upcoming)
        self.assertIsNone(next_level(playable[-1]))
        self.assertIsNone(next_level(level_by_id(FREE_LEVEL)))
        self.assertIsNone(level_by_id("nope"))

    def test_a_broken_file_is_refused_with_the_reason(self):
        cases = [
            ({"version": 2, "levels": []}, "versión"),
            ({"version": 1, "levels": [{"id": "1"}]}, "libre"),
            ({"version": 1, "levels": [{"id": "libre"}, {"id": "libre"}]}, "repetidos"),
            ({"version": 1, "levels": [{"id": "libre", "start_zone": [0, 0, 0.2, 0.2]}]}, "dos zonas"),
            ({"version": 1, "levels": [{"id": "libre", "start_zone": [0.3, 0, 0.2, 0.2],
                                        "goal_zone": [0, 0, 1, 1]}]}, "fuera de 0..1"),
            ({"version": 1, "levels": [{"id": "libre", "bumpers_max": -1}]}, "bumpers_max"),
            ({"version": 1, "levels": [{"id": "libre", "spikes": [{"rect": [0, 0, 1, 1], "facing": "up-ish"}]}]},
             "facing"),
            ({"version": 1, "levels": [{"id": "libre", "portals": [{"entry": {"rect": [0, 0, 1, 1]},
                                                                    "exit": [1.5, 0.5]}]}]}, "portal"),
            ({"version": 1, "levels": [{"id": "libre", "walls": [{"points": [[0, 0], [1, 0]]}]}]}, "points"),
        ]
        for data, reason in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "levels.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, reason):
                    load_levels(path)

    def test_the_portal_clearance_uses_the_physics_ball(self):
        self.assertEqual(BALL_RADIUS_PX, PHYSICS_RADIUS)


class GeometryTests(unittest.TestCase):
    def test_shares_become_pixels_of_the_space(self):
        level = level_by_id("5")
        geometry = level_geometry(level, 1000, 500)
        x0, y0, x1, y1 = level.start_zone
        self.assertEqual(geometry.start_zone.points[0], Point(x0 * 1000, y0 * 500))
        self.assertEqual(geometry.start_zone.points[2], Point(x1 * 1000, y1 * 500))
        exit_x, exit_y = level.portals[0]["exit"]
        self.assertEqual(geometry.portals[0].exit, Point(exit_x * 1000, exit_y * 500))
        self.assertEqual(geometry.facings, ("up",))

    def test_the_free_level_adds_nothing_to_a_board(self):
        board = with_obstacles(_board(), level_geometry(level_by_id(FREE_LEVEL), 960, 720))
        self.assertEqual((board.walls, board.spikes, board.portals), ((), (), ()))


def _board():
    from contracts.model import Board
    return Board(rect(0, 0, 960, 720), rect(10, 10, 50, 50), rect(100, 600, 140, 640), (), ())


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.level = level_by_id("4")
        self.geometry = level_geometry(self.level, 1920, 1080)

    def notes(self, bumpers=(), **where):
        return notes_for(self.level, [{"center": b, "angle": 0} for b in bumpers], **where)

    def test_notes_in_their_zones_play(self):
        check = level_check(self.level, self.geometry, self.notes())
        self.assertEqual(check, {"start": True, "goal": True, "bumpers": 0, "bumpers_max": 1, "covered": []})
        self.assertIsNone(level_problem(check))

    def test_each_problem_in_the_order_an_operator_fixes_it(self):
        out = level_problem(level_check(self.level, self.geometry, self.notes(start=(0.1, 0.5), goal=(0.1, 0.1))))
        self.assertEqual(out, "Poné el post-it de inicio dentro de su recuadro.")
        goal = level_problem(level_check(self.level, self.geometry, self.notes(goal=(0.5, 0.9))))
        self.assertEqual(goal, "Poné el post-it de llegada dentro de su recuadro.")
        many = level_problem(level_check(self.level, self.geometry, self.notes(bumpers=((0.1, 0.4), (0.9, 0.4)))))
        self.assertEqual(many, "Este nivel deja usar 1 rebote: sacá 1.")
        none = level_by_id("1")
        zero = level_problem(level_check(none, level_geometry(none, 1920, 1080),
                                         notes_for(none, [{"center": (0.1, 0.4)}, {"center": (0.9, 0.4)}])))
        self.assertEqual(zero, "Este nivel se juega sin rebotes: sacá 2.")
        covered = level_problem(level_check(self.level, self.geometry, self.notes(bumpers=((0.45, 0.575),))))
        self.assertEqual(covered, "Un post-it tapa los pinchos: movelo.")

    def test_two_start_notes_are_never_in_the_zone(self):
        notes = self.notes()
        notes["start"] = notes["start"] * 2
        self.assertFalse(level_check(self.level, self.geometry, notes)["start"])

    def test_a_note_near_a_portal_exit_covers_it(self):
        level = level_by_id("5")
        geometry = level_geometry(level, 1920, 1080)
        exit_ = geometry.portals[0].exit
        side = 75.6
        touching = square(exit_.x, exit_.y + side / 2 + BALL_RADIUS_PX + 3, side)
        clear = square(exit_.x, exit_.y + side / 2 + BALL_RADIUS_PX + 5, side)
        notes = notes_for(level, [])
        self.assertEqual(level_check(level, geometry, {**notes, "bumper": (touching,)})["covered"], ["portal"])
        self.assertEqual(level_check(level, geometry, {**notes, "bumper": (clear,)})["covered"], [])

    def test_inside_and_overlaps_are_exact_on_edges_either_winding(self):
        box = rect(0, 0, 10, 10)
        reverse = Polygon(tuple(reversed(box.points)))
        for shape in (box, reverse):
            self.assertTrue(inside(Point(10, 5), shape))
            self.assertFalse(inside(Point(10.001, 5), shape))
        self.assertTrue(overlaps(box, rect(10, 0, 20, 10)))      # sharing an edge
        self.assertFalse(overlaps(box, rect(10.01, 0, 20, 10)))
        diamond = Polygon((Point(15, 5), Point(20, 10), Point(15, 15), Point(10.5, 10)))
        self.assertFalse(overlaps(box, diamond))


class SolutionTests(unittest.TestCase):
    def test_every_level_is_won_by_its_solution_from_the_zone_centers(self):
        for level in LEVELS:
            if level.id == FREE_LEVEL:
                continue
            with self.subTest(level=level.id):
                self.assertLessEqual(len(level.solution), level.bumpers_max)
                throw = play(level, list(level.solution))
                self.assertIsNone(throw.problem)
                self.assertEqual((throw.status, throw.ended_by), ("won", "goal"))

    def test_levels_with_bumpers_need_them(self):
        for level in LEVELS:
            if level.id == FREE_LEVEL:
                continue
            with self.subTest(level=level.id):
                self.assertEqual(play(level, []).status == "won", level.bumpers_max == 0)


if __name__ == "__main__":
    unittest.main()
