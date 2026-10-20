"""Gate tests for the camera/projector/wall simulator: tiny frames, no I/O.

Run: python -m unittest services.simulation.tests.test_model -v
"""

from __future__ import annotations

import math
import unittest

import cv2
import numpy as np

from services.calibration.coded import alignment_layout, decode_alignment
from services.simulation.loop import SimClock, capture_alignment, frame_record, run_closed_loop
from services.simulation.model import (Camera, Paper, Projector, Wall, apply_homography,
                                       homography_from_corners, normalized_inverse, standard_rig)
from services.simulation.projector import (NEUTRAL_COLOR, RIM_COLOR, alignment_code,
                                           circle_coverage, contain_transform, convex_coverage,
                                           fill_polygon, offset_polygon, render_pattern,
                                           render_scene, rim_polygon)
from services.web.backend.server import GameController

NEUTRAL_CHROMA = 16  # max - min of 8-bit RGB; every neutral color here is <= 10
ROLE_HEX = {"start": "#f7d84b", "goal": "#ff9b49", "bumper": "#6cc678", "marker": "#d75ab9"}


def rgb(value: str) -> np.ndarray:
    return np.array([int(value[i:i + 2], 16) for i in (1, 3, 5)], np.uint8)


def chroma(image_rgb: np.ndarray) -> np.ndarray:
    return image_rgb.max(axis=2).astype(np.int16) - image_rgb.min(axis=2).astype(np.int16)


def tiny_wall(width: int = 48, height: int = 36, seed: int = 3) -> Wall:
    paper = Paper("start", ((14.0, 10.0), (30.0, 11.0), (29.0, 26.0), (13.0, 25.0)), (0.78, 0.62, 0.06))
    return Wall(width, height, (paper,), seed=seed)


def preview(width: int, height: int, notes: dict[str, list[list[tuple[float, float]]]]) -> dict:
    """A ``live_detection`` shaped like the controller's, from camera-px quads."""
    roles = {role: {"count": len(notes.get(role, [])), "expected": None,
                    "notes": [{"points": [{"x": x, "y": y} for x, y in quad]} for quad in notes.get(role, [])]}
             for role in ROLE_HEX}
    return {"width": width, "height": height, "roles": roles, "quality": {"ready": False}}


def snapshot(live: dict) -> dict:
    """The parts of a controller snapshot the projector reads."""
    return {"live_detection": live,
            "palette": {"roles": {role: {"hex": value} for role, value in ROLE_HEX.items()}},
            "camera": {"width": live["width"], "height": live["height"]},
            "game": {"status": "ready", "width": live["width"], "height": live["height"],
                     "position": {"x": 60.0, "y": 60.0}, "aim_degrees": -45.0}}


# Four notes, one per role, in a 160x120 camera frame.
CAMERA = (160, 120)
NOTES = {
    "start": [[(20.0, 20.0), (44.0, 18.0), (46.0, 42.0), (22.0, 44.0)]],
    "goal": [[(100.0, 30.0), (126.0, 34.0), (122.0, 58.0), (97.0, 54.0)]],
    "bumper": [[(40.0, 80.0), (80.0, 74.0), (82.0, 94.0), (42.0, 100.0)]],
    "marker": [[(120.0, 90.0), (138.0, 90.0), (138.0, 108.0), (120.0, 108.0)]],
}
PROJECTOR = (240, 135)
# Camera -> projector: a keystoned stretch, like a real alignment.
ALIGNMENT = np.linalg.inv(homography_from_corners(
    240, 135, ((8.0, 6.0), (150.0, 10.0), (146.0, 112.0), (12.0, 108.0))))
ALIGNMENT /= ALIGNMENT[2, 2]


def note_mask(polygons: list[np.ndarray], size: tuple[int, int]) -> np.ndarray:
    """True where any polygon covers any part of a pixel."""
    width, height = size
    mask = np.zeros((height, width), bool)
    for polygon in polygons:
        mask |= convex_coverage(polygon, (0, 0, width, height)) > 0
    return mask


def projected(mapper) -> list[np.ndarray]:
    return [mapper(np.asarray(quad)) for quads in NOTES.values() for quad in quads]


class GeometryTests(unittest.TestCase):
    def test_coverage_is_exact_area(self):
        box = cv2.boxPoints(((31.3, 22.6), (17.0, 9.5), 23.0)).astype(np.float64)
        coverage = convex_coverage(box, (0, 0, 64, 48))
        self.assertAlmostEqual(float(coverage.sum()), 17.0 * 9.5, delta=0.05)
        ys, xs = np.mgrid[0:48, 0:64]
        self.assertAlmostEqual(float((coverage * xs).sum() / coverage.sum()), 31.3, delta=0.01)
        disc = circle_coverage((20.4, 15.7), 6.0, (0, 0, 40, 30))
        self.assertAlmostEqual(float(disc.sum()), math.pi * 36, delta=0.1)
        # Pixel centers at integers: [9.5, 19.5] covers pixels 10..19 exactly.
        square = convex_coverage([(9.5, 9.5), (19.5, 9.5), (19.5, 19.5), (9.5, 19.5)], (0, 0, 30, 30))
        expected = np.zeros((30, 30), np.float32)
        expected[10:20, 10:20] = 1.0
        np.testing.assert_allclose(square, expected, atol=1e-6)

    def test_offset_polygon_follows_offsetConvexPolygon(self):
        square = np.array([(0, 0), (10, 0), (10, 10), (0, 10)], float)
        for winding in (square, square[::-1]):
            grown = offset_polygon(winding, 5)
            self.assertAlmostEqual(abs(cv2.contourArea(grown.astype(np.float32))), 400.0)
        self.assertIsNone(offset_polygon(square, -5))           # collapses to a point
        self.assertAlmostEqual(abs(cv2.contourArea(offset_polygon(square * 2, -5).astype(np.float32))), 100.0)
        dented = np.array([(0, 0), (10, 0), (5, 2), (10, 10), (0, 10)], float)
        self.assertIsNone(offset_polygon(dented, 5))            # not convex
        self.assertEqual(rim_polygon(dented).tolist(), [[-5.0, -5.0], [15.0, -5.0], [15.0, 15.0], [-5.0, 15.0]])
        sliver = np.array([(0, 0), (100, 0), (50, 20)], float)  # 43 degree corners exceed miter limit 2
        self.assertEqual(len(offset_polygon(sliver, 5, 2.0)), 5)
        self.assertEqual(len(offset_polygon(sliver, 5)), 3)
        self.assertIsNone(fill_polygon(np.array([(0, 0), (14, 0), (14, 14), (0, 14)], float)))  # 4x4 < 25 px2


class CameraModelTests(unittest.TestCase):
    def test_capture_is_deterministic_with_a_seed(self):
        wall = tiny_wall()
        projector = Projector(64, 36, homography_from_corners(64, 36, ((2, 2), (46, 3), (45, 33), (3, 32))))
        image = np.zeros((36, 64, 3), np.uint8)
        image[10:20, 20:40] = (255, 155, 73)
        shots = [Camera(48, 36, seed=seed).capture(wall, image, projector).jpeg for seed in (5, 5, 6)]
        self.assertEqual(shots[0], shots[1])
        self.assertNotEqual(shots[0], shots[2])
        camera = Camera(48, 36, seed=5)
        self.assertNotEqual(camera.capture(wall, image, projector).jpeg,
                            camera.capture(wall, image, projector).jpeg)  # noise moves on per frame

    def test_projected_white_square_lands_where_g_says(self):
        g = homography_from_corners(80, 60, ((6, 4), (58, 8), (55, 44), (9, 40)))
        projector = Projector(80, 60, g, black_level=0.0)
        image = np.zeros((60, 80, 3), np.uint8)
        image[26:35, 46:55] = 255  # square centered on projector px (50, 30)
        light = projector.light(image, (64, 48)).sum(axis=2)
        ys, xs = np.mgrid[0:48, 0:64]
        centroid = np.array(((light * xs).sum(), (light * ys).sum())) / light.sum()
        expected = apply_homography(g, [(50.0, 30.0)])[0]
        wrong_way = apply_homography(normalized_inverse(g), [(50.0, 30.0)])[0]
        self.assertLess(np.linalg.norm(centroid - expected), 0.3)
        self.assertGreater(np.linalg.norm(centroid - wrong_way), 5.0)
        # And the camera photographs it there.
        wall = Wall(64, 48, ambient=(0.02, 0.02, 0.02))
        shot = Camera(64, 48, seed=1, fixed_gain=1.0).capture(wall, image, projector)
        gray = cv2.cvtColor(shot.bgr, cv2.COLOR_BGR2GRAY)
        x, y = np.round(expected).astype(int)
        self.assertGreater(int(gray[y, x]), 200)
        self.assertLess(int(gray[3, 3]), 60)

    def test_auto_exposure_moves_half_way_to_the_target_each_frame(self):
        wall = tiny_wall()
        camera = Camera(48, 36, gain=8.0, target=0.18, read_noise=0.0, shot_noise=0.0)
        ideal = camera.metered_gain(wall, None)
        errors, lumas = [], []
        for _ in range(10):
            shot = camera.capture(wall, None)
            errors.append(shot.gain - ideal)
            lumas.append(shot.mean_luma)
        self.assertGreater(abs(errors[0]), 2.0)
        for before, after in zip(errors, errors[1:]):
            self.assertAlmostEqual(after, before / 2, places=9)
        self.assertGreater(lumas[0], 2 * 0.18)
        self.assertAlmostEqual(lumas[-1], 0.18, delta=0.18 * 0.01)
        locked = Camera(48, 36, fixed_gain=1.5)
        self.assertEqual({locked.capture(wall, None).gain for _ in range(3)}, {1.5})


class RendererTests(unittest.TestCase):
    def test_new_renderer_never_emits_saturated_light_outside_papers(self):
        state = snapshot(preview(*CAMERA, NOTES))
        aligned = note_mask(projected(lambda quad: apply_homography(ALIGNMENT, quad)), PROJECTOR)
        scale, ox, oy = contain_transform(*PROJECTOR, *CAMERA)
        unaligned = note_mask(projected(lambda quad: quad * scale + (ox, oy)), PROJECTOR)
        for theme in ("dark", "light"):
            with self.subTest(theme=theme):
                filled = render_scene(state, theme, PROJECTOR, "new", matrix=ALIGNMENT, fill=True)
                self.assertLessEqual(int(chroma(filled)[~aligned].max()), NEUTRAL_CHROMA)
                self.assertGreater(int(chroma(filled)[aligned].max()), 40)  # the fill is really there
                # Fill strictly inside: nothing colored within the inset band at the edge.
                inner = note_mask([fill_polygon(apply_homography(ALIGNMENT, np.asarray(quad)))
                                   for quads in NOTES.values() for quad in quads], PROJECTOR)
                self.assertLessEqual(int(chroma(filled)[~inner].max()), NEUTRAL_CHROMA)
                rims_only = render_scene(state, theme, PROJECTOR, "new", fill=True)  # unaligned: fill ignored
                self.assertLessEqual(int(chroma(rims_only).max()), NEUTRAL_CHROMA)
                self.assertTrue(unaligned.any())
                self.assertGreater(int((rims_only == rgb(RIM_COLOR[theme])).all(axis=2).sum()), 100)
        neutral = render_scene(state, "light", PROJECTOR, "neutral")
        self.assertEqual(np.unique(neutral.reshape(-1, 3), axis=0).tolist(), [[243, 242, 236]])

    def test_old_renderer_paints_role_color_outside_papers(self):
        """Documents the bug the contract fixes: a colored halo on bare wall."""
        state = snapshot(preview(*CAMERA, NOTES))
        scale, ox, oy = contain_transform(*PROJECTOR, *CAMERA)
        polygons = projected(lambda quad: quad * scale + (ox, oy))
        old = chroma(render_scene(state, "dark", PROJECTOR, "old"))
        outside = ~note_mask(polygons, PROJECTOR)
        self.assertGreater(int(old[outside].max()), 24)  # measured 37
        # The 18 px halo reaches well past the paper: >4 px out, still role colored.
        far = ~note_mask([offset_polygon(polygon, 4) for polygon in polygons], PROJECTOR)
        self.assertGreater(int((old[far] > NEUTRAL_CHROMA).sum()), 400)  # measured 893

    def test_aligned_ball_follows_h_like_placeBall(self):
        state = snapshot(preview(*CAMERA, {}))
        state["game"]["status"] = "running"  # no aim line: only the ball has the ball color
        image = render_scene(state, "dark", PROJECTOR, "new", matrix=ALIGNMENT)
        ball = np.argwhere((image == rgb(NEUTRAL_COLOR["dark"])).all(axis=2))
        center = apply_homography(ALIGNMENT, [(60.0, 60.0)])[0]
        self.assertLess(np.linalg.norm(ball.mean(axis=0)[::-1] - center), 0.5)
        # Radius 10 camera px through H's local stretch, like placeBall.
        stretch = math.sqrt(abs(np.linalg.det(cv2.getPerspectiveTransform(
            np.float32([(59, 59), (61, 59), (61, 61), (59, 61)]),
            np.float32(apply_homography(ALIGNMENT, [(59, 59), (61, 59), (61, 61), (59, 61)])))[:2, :2])))
        self.assertAlmostEqual(math.sqrt(len(ball) / math.pi), 10 * stretch, delta=1.0)

    def test_pattern_frames_light_every_dot_exactly_once_per_pair(self):
        self.assertEqual([alignment_code(i) for i in (1, 3, 7, 63)], [65, 3, 71, 63])
        dots = [{"id": i + 1, "x": 8.0 + 10 * i, "y": 12.0 + 3 * (i % 2)} for i in range(6)]
        layout = {"width": 72, "height": 30, "radius": 3.2, "dots": dots}
        for bit in range(7):
            on = render_pattern(layout, {"bit": bit, "inverse": False})[..., 0]
            off = render_pattern(layout, {"bit": bit, "inverse": True})[..., 0]
            for dot in dots:
                x, y = int(dot["x"]), int(dot["y"])
                lit = ((alignment_code(dot["id"]) >> bit) & 1) == 1
                self.assertEqual((on[y, x], off[y, x]), (255, 0) if lit else (0, 255))
            self.assertTrue(np.array_equal(on.astype(int) + off, np.maximum(on, off)))  # never both
        self.assertTrue(np.array_equal(render_pattern(layout, "warmup"),
                                       render_pattern(layout, {"bit": 0, "inverse": False})))


class LoopTests(unittest.TestCase):
    def test_frame_record_sorts_detections(self):
        start = Paper("start", ((20, 20), (40, 20), (40, 40), (20, 40)), (0.8, 0.6, 0.1))
        goal = Paper("goal", ((80, 20), (100, 20), (100, 40), (80, 40)), (0.8, 0.3, 0.1))
        wall = Wall(160, 120, (start,))
        live = preview(160, 120, {"start": [[(21, 21), (39, 21), (39, 39), (21, 39)]],
                                  "goal": [[(82, 22), (98, 22), (98, 38), (82, 38)],    # where the goal was
                                           [(120, 80), (140, 80), (140, 100), (120, 100)]]})  # nothing there
        record = frame_record(0, live, wall, removed=[goal])
        self.assertEqual((record["phantoms"], record["ghosts"], record["missed"]), (1, 1, 0))
        self.assertAlmostEqual(record["area_ratios"][0], 18 * 18 / 400)

    def test_closed_loop_new_rules_hold_still_old_rules_grow(self):
        """The real controller in the loop, 320x180 camera, 4 frames per rule set.

        At this size one pixel of outline is ~10% of a note's area, so the
        bounds are loose (open loop measures up to 1.11); the 60-frame
        full-size numbers live in feedback_eval.
        """
        def run(rules: str) -> tuple[list[dict], int]:
            wall, projector, camera = standard_rig(scale=0.25, seed=3)
            camera.fixed_gain = camera.metered_gain(wall, None)
            clock = SimClock()
            matrix = normalized_inverse(projector.to_camera) if rules == "new" else None
            records = run_closed_loop(GameController(log_path=None, clock=clock), wall, projector, camera,
                                      frames=4, rules=rules, alignment_matrix=matrix, fill=True, clock=clock)
            return records, [paper.role for paper in wall.papers].index("goal")

        def largest(records: list[dict], index: int) -> float:
            return max(record["area_ratios"][index] or 0.0 for record in records)

        new, goal = run("new")
        self.assertTrue(all(record["ready"] for record in new))
        self.assertEqual(sum(record["phantoms"] + record["missed"] for record in new), 0)
        self.assertLess(max(largest(new, index) for index in range(8)), 1.2)
        old, goal = run("old")
        self.assertGreater(largest(old, goal), 1.8)  # the goal's own halo merges into it (x2.2)

    def test_pattern_frames_decode_to_the_true_inverse_g(self):
        """Simulated alignment frames through the contract decoder (1.3) land
        on inverse(G): the pattern renderer and pixel convention are unbiased."""
        wall, projector, camera = standard_rig(scale=0.25, seed=2)
        layout = alignment_layout(projector.width, projector.height)
        frames = {key: cv2.cvtColor(cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR),
                                    cv2.COLOR_BGR2GRAY)
                  for key, jpeg in capture_alignment(layout, wall, projector, camera).items()}
        self.assertEqual(len(frames), 14)
        result = decode_alignment(frames, layout)
        self.assertTrue(result["ok"], result["reason"])
        corners = np.vstack([paper.polygon() for paper in wall.papers])
        error = np.linalg.norm(apply_homography(np.asarray(result["matrix"]), corners)
                               - apply_homography(normalized_inverse(projector.to_camera), corners), axis=1)
        self.assertLess(float(error.max()), 0.5)  # measured 0.15 projector px

if __name__ == "__main__":
    unittest.main()
