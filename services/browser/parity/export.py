"""Golden answers from the Python backend, for the JS port to match.

The browser build runs a JavaScript port of the vision, calibration, physics
and web-controller services. This script asks the real Python code the same
questions the JS tests ask and writes its answers to ``golden/``. The JS gate
tests (``services/browser/tests``) replay every case and must agree.

Frames are stored as raw BGR bytes (gzip), never as JPEG, so both sides see
the same pixels: the controller cases decode the JPEG once here and the
Python controller receives that same JPEG.

Regenerate after changing any Python service the port mirrors::

    ./.venv/bin/python -m services.browser.parity.export
"""

from __future__ import annotations

import gzip
import json
import math
import random
from pathlib import Path
from typing import Any, Callable

import cv2
import numpy as np

from contracts import Board, Point, Polygon, quadrilateral_center
from contracts.geometry import offset_convex_polygon
from services.calibration import (ALIGNMENT_FRAMES, alignment_code, alignment_layout, decode_alignment,
                                  estimate_homography, pattern_dots, transform_board, transform_point)
from services.calibration.tests.render import capture_frames, homography_from_quad, project_to_camera, render_pattern
from services.physics import GameStatus, launch, ready_state, step
from services.vision import build_palette, detect_board, find_notes, frame_quality, hsv_from_hex, rule_from_hex
from services.vision.detector import refine_quad
from services.vision.palette import DEFAULT_PALETTE
from services.vision.tests.scene import bgr, hex_at, scene
from services.vision.tests.test_refine import ring, saturation_image, tilted_paper
from services.web.backend.eval import FRAME_CENTERS, FRAME_HUES, _color, _image
from services.web.backend.server import (GameController, RequestError, _board_json, _normalized_inverse,
                                         _polygon_json, place_probes)

GOLDEN = Path(__file__).with_name("golden")
RECOLORED = {"start": 105, "goal": 176, "bumper": 28, "marker": 140}
SOURCES = ("contracts", "services/vision", "services/calibration", "services/physics",
           "services/web/backend/server.py", "services/web/backend/camera.py")


# ---------------------------------------------------------------- frames

def _jpeg(image: np.ndarray) -> bytes:
    okay, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert okay
    return encoded.tobytes()


class Frames:
    """Named frames written as raw BGR; ``jpeg`` keeps the bytes Python scans."""

    def __init__(self) -> None:
        self.meta: dict[str, dict[str, int]] = {}
        self.images: dict[str, np.ndarray] = {}
        self.jpegs: dict[str, bytes] = {}

    def add_raw(self, name: str, image: np.ndarray) -> np.ndarray:
        self.images[name] = image
        self.meta[name] = {"width": int(image.shape[1]), "height": int(image.shape[0])}
        return image

    def add_gray(self, name: str, image: np.ndarray) -> np.ndarray:
        """A single-channel image (refinement features, alignment frames, bridges)."""
        assert image.ndim == 2 and image.dtype == np.uint8, name
        self.images[name] = image
        self.meta[name] = {"width": int(image.shape[1]), "height": int(image.shape[0]), "channels": 1}
        return image

    def add_jpeg(self, name: str, image: np.ndarray) -> np.ndarray:
        data = _jpeg(image)
        self.jpegs[name] = data
        return self.add_raw(name, cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR))

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.gz"):
            old.unlink()
        for name, image in self.images.items():
            suffix = "gray" if image.ndim == 2 else "bgr"
            with gzip.open(directory / f"{name}.{suffix}.gz", "wb", compresslevel=6) as stream:
                stream.write(np.ascontiguousarray(image).tobytes())


def build_frames() -> Frames:
    frames = Frames()
    frames.add_jpeg("wall_430", _image(430))
    frames.add_jpeg("wall_540", _image(540))
    frames.add_jpeg("recolored", _image(430, RECOLORED))
    no_start = _image(430)
    no_start[315:386, 145:216] = 220
    frames.add_jpeg("missing_start", no_start)
    no_markers = _image(430)
    for x, y in ((90, 90), (870, 90), (870, 630), (90, 630)):
        no_markers[y - 25:y + 26, x - 25:x + 26] = 220
    frames.add_jpeg("no_markers", no_markers)
    duplicate_start = _image(430)
    duplicate_start[430:491, 150:211] = duplicate_start[350, 180]
    frames.add_jpeg("duplicate_start", duplicate_start)
    extra = _image(430)
    cv2.rectangle(extra, (458, 578), (502, 622), (200, 60, 200), -1)
    frames.add_jpeg("extra_corner", extra)
    flat = np.full((720, 960, 3), 220, np.uint8)
    frames.add_jpeg("empty_wall", flat)
    # Corners nearly in a line: four markers that cannot frame a field.
    squeezed = np.full((720, 960, 3), 220, np.uint8)
    from services.web.backend.eval import _color, FRAME_HUES
    for x, y in ((90, 350), (330, 360), (600, 350), (870, 362)):
        cv2.rectangle(squeezed, (x - 22, y - 22), (x + 22, y + 22), _color(FRAME_HUES["marker"]), -1)
    cv2.rectangle(squeezed, (150, 100), (210, 160), _color(FRAME_HUES["start"]), -1)
    cv2.rectangle(squeezed, (750, 600), (810, 660), _color(FRAME_HUES["goal"]), -1)
    frames.add_jpeg("line_of_corners", squeezed)

    scenes = {
        "scene_normal": {},
        "scene_perspective": {"perspective": True},
        "scene_dim_speckles": {"brightness": 0.55, "speckles": True},
        "scene_skew_dim_speckles": {"brightness": 0.7, "perspective": True, "speckles": True},
        "scene_recolored": {"hues": RECOLORED, "speckles": True},
        "scene_recolored_skew": {"hues": RECOLORED, "perspective": True, "brightness": 0.7, "speckles": True},
        "scene_pastel": {"saturation": 70, "brightness": 0.8},
        "scene_no_greens": {"greens": 0},
    }
    for name, options in scenes.items():
        image, expected = scene(**options)
        frames.add_raw(name, image)
        frames.meta[name]["picked"] = {
            "start": hex_at(image, expected["start"]), "goal": hex_at(image, expected["goal"]),
            "bumper": hex_at(image, expected["greens"][0]) if len(expected["greens"]) else None,
            "marker": hex_at(image, expected["markers"][0]),
        }
    frames.add_raw("tiny", np.full((12, 40, 3), 200, np.uint8))
    return frames


# ---------------------------------------------------------------- helpers

def _attempt(fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "value": fn()}
    except (ValueError, RuntimeError) as exc:
        return {"ok": False, "error": str(exc)}


def _point(point: Point) -> list[float]:
    return [point.x, point.y]


def _poly(polygon: Polygon) -> list[list[float]]:
    return [_point(p) for p in polygon.points]


def _board(board: Board) -> dict[str, Any]:
    return {"playfield": _poly(board.playfield), "start": _poly(board.start), "goal": _poly(board.goal),
            "bumpers": [_poly(p) for p in board.bumpers], "markers": [_poly(p) for p in board.markers]}


def _from_board(data: dict[str, Any]) -> Board:
    def poly(points):
        return Polygon(tuple(Point(float(x), float(y)) for x, y in points))
    return Board(poly(data["playfield"]), poly(data["start"]), poly(data["goal"]),
                 tuple(poly(p) for p in data["bumpers"]), tuple(poly(p) for p in data["markers"]))


# ---------------------------------------------------------------- palette

def palette_cases() -> dict[str, Any]:
    rng = random.Random(7)
    hexes = ["#ff0000", "#00FF00", "#0000ff", "#ffffff", "#000000", "#f7d84b", "#ff9b49",
             "#6cc678", "#d75ab9", "#3a6df0", "#808080", "#7f7f80", "#ff8000", " #ABCDEF ",
             "#fff", "red", "#gg0000", "ff0000"]
    hexes += ["#%06x" % rng.randrange(0, 1 << 24) for _ in range(300)]
    hsv = [{"hex": h, **_attempt(lambda h=h: list(hsv_from_hex(h)))} for h in hexes]
    rules = [{"hex": h, **_attempt(lambda h=h: _rule_json(rule_from_hex(h)))} for h in hexes]
    roles = ("start", "goal", "bumper", "marker")
    builds = []
    picks = ["#f7d84b", "#ff9b49", "#6cc678", "#d75ab9", "#3a6df0", "#e0402a", "#30d0c0", "#a040f0",
             "#f0f040", "#40f080", "#ff40a0", "#2050ff", "#999999"]
    for _ in range(400):
        choices = {role: (rng.choice(picks + ["#%06x" % rng.randrange(0, 1 << 24)])
                          if rng.random() < 0.45 else None) for role in roles}
        changed = rng.choice([None, *roles])
        builds.append({"choices": choices, "changed": changed,
                       **_attempt(lambda c=choices, ch=changed: build_palette(c, changed=ch).to_json())})
    builds.append({"choices": {"sky": "#123456"}, "changed": None,
                   **_attempt(lambda: build_palette({"sky": "#123456"}).to_json())})
    return {"hsv": hsv, "rules": rules, "builds": builds}


def _rule_json(rule) -> dict[str, Any]:
    return {"hex": rule.hex, "hue_low": rule.hue_low, "hue_high": rule.hue_high,
            "min_saturation": rule.min_saturation, "min_value": rule.min_value}


# ---------------------------------------------------------------- vision

def vision_cases(frames: Frames) -> list[dict[str, Any]]:
    cases = []
    for name, image in frames.images.items():
        palettes = {"default": {}}
        picked = frames.meta[name].get("picked")
        if picked:
            palettes["picked"] = {role: value for role, value in picked.items() if value}
        if name == "recolored":
            palettes["picked"] = {role: _sample(image, role) for role in FRAME_CENTERS}
        for label, choices in palettes.items():
            palette_result = _attempt(lambda c=choices: build_palette(c))
            if not palette_result["ok"]:
                cases.append({"frame": name, "palette": label, "choices": choices,
                              "palette_error": palette_result["error"]})
                continue
            palette = palette_result["value"]
            notes = _attempt(lambda p=palette: {role: [_poly(poly) for poly in polys]
                                                for role, polys in find_notes(image, p).items()})
            board = _attempt(lambda p=palette: _board(detect_board(image, p)))
            quality = _attempt(lambda p=palette: frame_quality(image, p))
            cases.append({"frame": name, "palette": label, "choices": choices,
                          "notes": notes, "board": board, "quality": quality})
    return cases


def _sample(image: np.ndarray, role: str) -> str:
    x, y = FRAME_CENTERS[role]
    blue, green, red = (int(round(c)) for c in image[y - 2:y + 3, x - 2:x + 3].reshape(-1, 3).mean(axis=0))
    return f"#{red:02x}{green:02x}{blue:02x}"


# ---------------------------------------------------------------- calibration

def homography_cases() -> list[dict[str, Any]]:
    rng = random.Random(11)
    cases = []
    for _ in range(120):
        width, height = rng.choice([(960, 720), (1280, 720), (640, 480)])
        camera = [Point(rng.uniform(40, width * 0.3), rng.uniform(40, height * 0.3)),
                  Point(rng.uniform(width * 0.7, width - 40), rng.uniform(40, height * 0.3)),
                  Point(rng.uniform(width * 0.7, width - 40), rng.uniform(height * 0.7, height - 40)),
                  Point(rng.uniform(40, width * 0.3), rng.uniform(height * 0.7, height - 40))]
        projector = [Point(p.x * rng.uniform(1.1, 1.4) + rng.uniform(-30, 80),
                           p.y * rng.uniform(1.0, 1.2) + rng.uniform(-30, 60)) for p in camera]
        cases.append(_homography_case(camera, projector))
    line = [Point(10 * i, 10 * i) for i in range(4)]
    cases.append(_homography_case(line, [Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100)]))
    same = [Point(5, 5), Point(5, 5), Point(50, 5), Point(50, 50)]
    cases.append(_homography_case(same, [Point(0, 0), Point(100, 0), Point(100, 100), Point(0, 100)]))
    cases.append(_homography_case(line[:3], line[:3]))
    return cases


def _homography_case(camera: list[Point], projector: list[Point]) -> dict[str, Any]:
    result = _attempt(lambda: [list(row) for row in estimate_homography(camera, projector)])
    case = {"camera": [_point(p) for p in camera], "projector": [_point(p) for p in projector], **result}
    if result["ok"]:
        probes = [Point(c.x + 7.5, c.y - 3.25) for c in camera]
        case["mapped"] = [_point(transform_point(p, result["value"])) for p in probes]
    return case


# ---------------------------------------------------------------- refinement and bridge (2.4, 2.5)

SQUARE = [(40.0, 30.0), (110.0, 30.0), (110.0, 90.0), (40.0, 90.0)]
TILTED = [(52.3, 24.6), (121.8, 38.1), (108.7, 99.4), (37.2, 84.9)]


def refine_cases(frames: Frames) -> list[dict[str, Any]]:
    cases = []

    def add(name: str, feature: np.ndarray, coarse) -> None:
        if name not in frames.images:
            frames.add_gray(name, feature)
        quad, outcome = refine_quad(feature, coarse)
        cases.append({"feature": name, "coarse": [[float(x), float(y)] for x, y in coarse],
                      "quad": quad.tolist(), "outcome": outcome})

    jitter = ((1.5, 1.0), (-1.2, 1.4), (-1.0, -1.5), (1.3, -0.9))
    for label, truth in (("square", SQUARE), ("tilted", TILTED)):
        feature = saturation_image(truth)
        add(f"refine_{label}", feature, [(x + dx, y + dy) for (x, y), (dx, dy) in zip(truth, jitter)])
        add(f"refine_{label}", feature, [(x + 1, y + 1) for x, y in reversed(truth)])
        add(f"refine_{label}", feature, truth)
    add("refine_sharp", saturation_image(TILTED, blur=0), TILTED)
    add("refine_low", saturation_image(SQUARE, inside=40.0, outside=25.0), SQUARE)
    tiny = [(50.0, 50.0), (57.0, 50.0), (57.0, 70.0), (50.0, 70.0)]
    add("refine_tiny", saturation_image(tiny), tiny)

    def shifted(truth):
        (x0, y0), _, (x1, y1), _ = truth
        return [(x0 - 3.15, y0 - 3.15), (x1, y0 - 3.15), (x1, y1), (x0 - 3.15, y1)]
    small = [(60.0, 50.0), (80.0, 50.0), (80.0, 70.0), (60.0, 70.0)]
    large = [(40.0, 30.0), (100.0, 30.0), (100.0, 90.0), (40.0, 90.0)]
    add("refine_small", saturation_image(small), shifted(small))
    add("refine_large", saturation_image(large), shifted(large))
    grown = [(75 + (x - 75) * 1.12, 60 + (y - 60) * 1.12) for x, y in SQUARE]
    add("refine_square", saturation_image(SQUARE), grown)
    # A dark room's two-tone paper (step 9), as in the gate test.
    angle = math.radians(21.0)
    paper = [(80 + math.cos(angle) * x - math.sin(angle) * y, 60 + math.sin(angle) * x + math.cos(angle) * y)
             for x, y in ((-38, -26), (38, -26), (38, 26), (-38, 26))]
    from services.vision.tests.test_refine import coverage
    image = 15.0 + 185.0 * coverage((160, 120), paper) - 90.0 * coverage((160, 120), offset_convex_polygon(paper, -3.3))
    add("refine_two_tone", np.clip(np.rint(cv2.GaussianBlur(image, (0, 0), 1.2)), 0, 255).astype(np.uint8),
        [paper[0], paper[1], (paper[2][0] - 4.0, paper[2][1] - 3.0), paper[3]])
    rng = random.Random(17)
    for index in range(60):
        cx, cy = rng.uniform(55, 105), rng.uniform(45, 75)
        w, h, a = rng.uniform(14, 70), rng.uniform(12, 50), math.radians(rng.uniform(-45, 45))
        truth = [(cx + math.cos(a) * x - math.sin(a) * y, cy + math.sin(a) * x + math.cos(a) * y)
                 for x, y in ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))]
        feature = saturation_image(truth, inside=rng.uniform(30, 230), outside=rng.uniform(0, 40),
                                   blur=rng.choice([0.0, 0.8, 1.2, 2.0]))
        spread = rng.choice([0.5, 2.0, 4.0, 7.0])
        add(f"refine_random_{index}", feature,
            [(x + rng.uniform(-spread, spread), y + rng.uniform(-spread, spread)) for x, y in truth])
    return cases


def _notes_json(notes) -> dict[str, list[list[list[float]]]]:
    return {role: [_poly(polygon) for polygon in polygons] for role, polygons in notes.items()}


def bridge_cases(frames: Frames) -> list[dict[str, Any]]:
    cases = []

    def add(name: str, frame: np.ndarray, bridge: np.ndarray) -> None:
        frames.add_raw(name, frame)
        frames.add_gray(f"{name}_bridge", bridge)
        cases.append({"frame": name, "bridge": f"{name}_bridge",
                      "with": _attempt(lambda: _notes_json(find_notes(frame, bridge=bridge))),
                      "without": _attempt(lambda: _notes_json(find_notes(frame)))})

    frame = np.full((240, 320, 3), 205, np.uint8)
    cv2.rectangle(frame, (100, 80), (150, 120), bgr(65), -1)
    cv2.line(frame, (125, 70), (125, 130), (255, 255, 255), 3)
    bridge = np.zeros((240, 320), np.uint8)
    cv2.line(bridge, (125, 68), (125, 132), 255, 7)
    add("bridge_split", frame, bridge)
    bare = np.full((240, 320, 3), 205, np.uint8)
    cv2.line(bare, (125, 70), (125, 130), (255, 255, 255), 3)
    add("bridge_bare", bare, bridge)
    tilted, paper = tilted_paper()
    a = ((paper[0][0] + paper[1][0]) / 2, (paper[0][1] + paper[1][1]) / 2)
    b = ((paper[2][0] + paper[3][0]) / 2, (paper[2][1] + paper[3][1]) / 2)
    cut = tilted.copy()
    cv2.line(cut, (round(a[0]), round(a[1])), (round(b[0]), round(b[1])), (255, 255, 255), 3)
    line = np.zeros((240, 320), np.uint8)
    cv2.line(line, (round(a[0]), round(a[1])), (round(b[0]), round(b[1])), 255, 7)
    add("bridge_tilted_cut", cut, line)
    for inner in (0.0, 1.5, 3.0):
        add(f"bridge_ring_{inner}", tilted, ring(paper, inner, inner + 6.0))
    return cases


def offset_cases() -> list[dict[str, Any]]:
    rng = random.Random(23)
    cases = []
    for _ in range(80):
        count = rng.randrange(3, 9)
        cx, cy, radius = rng.uniform(-50, 900), rng.uniform(-50, 700), rng.uniform(5, 200)
        stretch, turn = rng.uniform(0.3, 1.0), rng.uniform(0, math.tau)
        points = [(cx + radius * math.cos(turn + math.tau * k / count),
                   cy + radius * stretch * math.sin(turn + math.tau * k / count)) for k in range(count)]
        if rng.random() < 0.5:
            points.reverse()
        distance = rng.uniform(-8, 8)
        cases.append({"points": points, "distance": distance, "result": offset_convex_polygon(points, distance)})
    for points in ([(0, 0), (1, 1)], [(0, 0), (1, 1), (2, 2)], [(0, 0), (0, 0), (5, 0), (5, 5)],
                   [(0, 0), (10, 0), (10, 10), (0, 10)]):
        cases.append({"points": points, "distance": 6.0, "result": offset_convex_polygon(points, 6.0)})
    return cases


ALIGN_PROJECTOR = (1280, 800)
ALIGN_QUAD = ((30, 25), (930, 20), (935, 700), (25, 695))           # 960 x 720 camera (controller scripts)
SMALL_QUAD = ((20, 16), (620, 14), (624, 466), (16, 464))           # 640 x 480 camera (decode cases)


def _barrel(image: np.ndarray, k: float) -> np.ndarray:
    """A lens's barrel distortion, which no homography can fit."""
    height, width = image.shape
    ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
    nx, ny = (xs - width / 2) / (width / 2), (ys - height / 2) / (height / 2)
    factor = 1 + k * (nx * nx + ny * ny)
    return cv2.remap(image, (nx * factor * width / 2 + width / 2).astype(np.float32),
                     (ny * factor * height / 2 + height / 2).astype(np.float32), cv2.INTER_LINEAR)


def alignment_cases(frames: Frames) -> dict[str, Any]:
    sizes = [(787, 752), (1280, 800), (1920, 1080), (640, 480), (320, 240), (8192, 240), (240, 8192),
             (1024, 768), (1366, 768), (800, 1280), (1280.5, 720.25)]
    layouts = []
    for width, height in sizes:
        layout = alignment_layout(width, height)
        layouts.append({"size": [width, height], "layout": layout,
                        "dots": {f"{bit}:{int(inverse)}": pattern_dots(layout, bit, inverse)
                                 for bit, inverse in ALIGNMENT_FRAMES}})
    layout = alignment_layout(*ALIGN_PROJECTOR)
    G = homography_from_quad(ALIGN_PROJECTOR, SMALL_QUAD)
    wide = homography_from_quad(ALIGN_PROJECTOR, ((-500, -400), (1140, -390), (1150, 880), (-510, 870)))
    narrow = homography_from_quad(ALIGN_PROJECTOR, tuple((320 + (x - 320) * 1.6, 240 + (y - 240) * 1.6)
                                                         for x, y in SMALL_QUAD))
    conditions = {
        "clean": (G, {}),
        "auto_exposure": (G, {"drift": 0.3, "noise": 2.0, "blur": 0.8, "seed": 4}),
        "reflection": (G, {"reflections": [(200, 150, 25, 2.5)], "noise": 1.0, "seed": 5}),
        "stale": (G, {"stale": (3, True), "seed": 6}),
        "dark": (G, {"gain": 0.02, "noise": 1.5, "seed": 7}),
        "zoomed": (wide, {"seed": 8}),
        "low_coverage": (narrow, {"seed": 1}),
        "lens_inaccurate": (G, {"seed": 1}),
        "lens_inconsistent": (G, {"seed": 1}),
        "missing": (G, {"seed": 9}),
    }
    lens = {"lens_inaccurate": 0.01, "lens_inconsistent": 0.02}
    decodes = []
    for name, (matrix, options) in conditions.items():
        captured = capture_frames(layout, matrix, (640, 480), **options)
        if name == "missing":
            del captured[(6, True)]
        if name in lens:
            captured = {key: _barrel(image, lens[name]) for key, image in captured.items()}
        keys = {}
        for (bit, inverse), image in captured.items():
            key = f"{bit}:{int(inverse)}"
            keys[key] = frames.add_gray(f"decode_{name}_{bit}_{int(inverse)}", image) is not None and \
                f"decode_{name}_{bit}_{int(inverse)}"
        decodes.append({"case": name, "frames": keys, "size": list(ALIGN_PROJECTOR),
                        "result": decode_alignment(captured, layout)})
    return {"codes": [alignment_code(i) for i in range(64)], "layouts": layouts, "decodes": decodes}


def probe_cases(frames: Frames) -> list[dict[str, Any]]:
    rng = random.Random(31)
    cases = []
    for name in ("wall_430", "wall_540", "scene_perspective", "empty_wall"):
        image = frames.images[name]
        notes = find_notes(image)
        detection = {"width": int(image.shape[1]), "height": int(image.shape[0]),
                     "roles": {role: {"notes": [_polygon_json(p) for p in notes[role]]} for role in notes}}
        for projector in ((1280, 800), (1920, 1080), (800, 600)):
            camera = (int(image.shape[1]), int(image.shape[0]))
            quad = [(x * camera[0] / 960 + rng.uniform(-20, 20), y * camera[1] / 720 + rng.uniform(-20, 20))
                    for x, y in ALIGN_QUAD]
            matrix = _normalized_inverse(homography_from_quad(projector, quad)).tolist()
            cases.append({"frame": name, "detection": detection, "matrix": matrix, "projector": list(projector),
                          "camera": list(camera),
                          "probes": place_probes(detection, matrix, projector, camera)})
    return cases


def _alignment_jpegs(frames: Frames) -> dict[str, str]:
    """The 14 coded frames filmed in the 960 x 720 wall camera, for the controller scripts."""
    layout = alignment_layout(*ALIGN_PROJECTOR)
    G = homography_from_quad(ALIGN_PROJECTOR, ALIGN_QUAD)
    names = {}
    for bit, inverse in ALIGNMENT_FRAMES:
        name = f"cam_align_{bit}_{int(inverse)}"
        frames.add_jpeg(name, project_to_camera(render_pattern(layout, bit, inverse), G, (960, 720)))
        names[f"{bit}:{int(inverse)}"] = name
    frames.add_jpeg("cam_dark", project_to_camera(np.zeros((ALIGN_PROJECTOR[1], ALIGN_PROJECTOR[0]), np.uint8),
                                                  G, (960, 720)))
    frames.add_jpeg("wall_small", cv2.resize(_image(430), (640, 480), interpolation=cv2.INTER_AREA))
    return names


def _probe_hit_frame(frames: Frames, align_names: dict[str, str]) -> None:
    """wall_430 with the goal probe's square painted in the goal color, where the camera sees it."""
    game = GameController(log_path=None, clock=lambda: 0.0)
    game.preview_jpeg(frames.jpegs["wall_430"], "Cámara de iPhone de Tobias")
    game.action({"action": "projection_open", "width": ALIGN_PROJECTOR[0], "height": ALIGN_PROJECTOR[1]})
    game.action({"action": "align_start"})
    for key, name in align_names.items():
        game.align_frame_jpeg(frames.jpegs[name], key, "Cámara de iPhone de Tobias")
    probes = game.action({"action": "align_solve"})["alignment"]["overlay"]["probes"]
    goal = next(probe for probe in probes if probe["role"] == "goal")
    G = homography_from_quad(ALIGN_PROJECTOR, ALIGN_QUAD)
    corners = cv2.perspectiveTransform(np.array([[[p["x"], p["y"]] for p in goal["points"]]], np.float64), G)[0]
    image = _image(430)
    cv2.fillConvexPoly(image, np.round(corners).astype(np.int32), _color(FRAME_HUES["goal"]))
    frames.add_jpeg("probe_hit", image)


# ---------------------------------------------------------------- physics

def _rect(left, top, right, bottom) -> Polygon:
    return Polygon((Point(left, top), Point(right, top), Point(right, bottom), Point(left, bottom)))


def _rotated(cx, cy, w, h, degrees) -> Polygon:
    angle = math.radians(degrees)
    cos, sin = math.cos(angle), math.sin(angle)
    corners = ((-w / 2, -h / 2), (w / 2, -h / 2), (w / 2, h / 2), (-w / 2, h / 2))
    return Polygon(tuple(Point(cx + x * cos - y * sin, cy + x * sin + y * cos) for x, y in corners))


def physics_cases() -> list[dict[str, Any]]:
    rng = random.Random(3)
    cases = []
    field = Polygon((Point(30, 20), Point(1240, 40), Point(1220, 770), Point(50, 760)))
    for index in range(80):
        start = _rotated(rng.uniform(150, 350), rng.uniform(150, 650), 60, 60, rng.uniform(-30, 30))
        goal = _rotated(rng.uniform(900, 1100), rng.uniform(150, 650), 60, 60, rng.uniform(-30, 30))
        bumpers = tuple(_rotated(rng.uniform(450, 800), rng.uniform(100, 700), rng.uniform(40, 120),
                                 rng.uniform(30, 80), rng.uniform(-80, 80)) for _ in range(rng.randrange(0, 6)))
        board = Board(field, start, goal, bumpers, ())
        angle = rng.uniform(-180, 180)
        speed = rng.choice([150.0, 320.0, 900.0, 4000.0])
        dt = rng.choice([1 / 120, 1 / 60, 0.05])
        cases.append(_physics_case(board, angle, speed, dt, 900))
    bad = Board(field, _rect(100, 100, 160, 160), _rect(120, 120, 180, 180), (), ())
    cases.append(_physics_case(bad, 0, 100, 1 / 120, 3))
    outside = Board(field, _rect(0, 0, 40, 40), _rect(600, 300, 660, 360), (), ())
    cases.append(_physics_case(outside, 0, 100, 1 / 120, 3))
    concave = Board(field, Polygon((Point(100, 100), Point(200, 100), Point(150, 120), Point(150, 200))),
                    _rect(600, 300, 660, 360), (), ())
    cases.append(_physics_case(concave, 0, 100, 1 / 120, 3))
    return cases


def _physics_case(board: Board, angle: float, speed: float, dt: float, steps: int) -> dict[str, Any]:
    case = {"board": _board(board), "angle": angle, "speed": speed, "dt": dt}
    try:
        state = launch(board, angle, speed)
    except ValueError as exc:
        case.update(ok=False, error=str(exc))
        return case
    trace = []
    for index in range(steps):
        state = step(board, state, dt)
        if index % 15 == 0 or state.status != GameStatus.RUNNING:
            trace.append({"i": index, "x": state.position.x, "y": state.position.y,
                          "vx": state.velocity.x, "vy": state.velocity.y,
                          "t": state.elapsed_s, "bounces": state.bounce_count, "status": state.status.value})
        if state.status != GameStatus.RUNNING:
            break
    case.update(ok=True, ready=_point(ready_state(board).position), trace=trace)
    return case


# ---------------------------------------------------------------- controller

PRESET = {"exposureMode": "manual", "exposureTime": 156, "whiteBalanceMode": "manual",
          "colorTemperature": 4500, "focusMode": "manual", "focusDistance": 0}


def alignment_scripts(align_names: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    def act(name, **data):
        return {"op": "action", "payload": {"action": name, **data}}
    label = "Cámara de iPhone de Tobias"
    feed = [{"op": "align_frame", "frame": name, "align": key, "camera": label} for key, name in align_names.items()]
    dark = [{"op": "align_frame", "frame": "cam_dark", "align": key, "camera": label} for key in align_names]
    start = [{"op": "preview", "frame": "wall_430", "camera": label},
             act("projection_open", width=ALIGN_PROJECTOR[0], height=ALIGN_PROJECTOR[1])]
    check = lambda overlay: [{"op": "check_frame", "frame": "wall_430", "kind": "neutral"},  # noqa: E731
                             {"op": "check_frame", "frame": overlay, "kind": "overlay"}, act("check_solve")]
    return {
        "align_ok": [*start, act("align_start"), {"op": "state"}, *feed, act("align_solve"),
                     {"op": "preview", "frame": "wall_430", "camera": label}, *check("wall_430"),
                     {"op": "preview", "frame": "wall_430", "camera": label},
                     act("aim", degrees=0), act("launch"), *[{"op": "tick", "advance": 0.1} for _ in range(12)],
                     {"op": "state"}, act("pause"), act("align_start"), act("align_cancel"), act("resume"),
                     act("projection_open", width=1920, height=1080), {"op": "state"}],
        "align_fill_off": [*start, act("align_start"), *feed, act("align_solve"), *check("probe_hit"),
                           {"op": "state"}, act("projection_open", width=1920, height=1080), {"op": "state"},
                           act("align_clear"), {"op": "state"}],
        "align_count_change": [*start, act("align_start"), *feed, act("align_solve"), *check("wall_540"),
                               act("projection_neutral"), {"op": "state"}, act("projection_restore")],
        "align_refusals": [{"op": "align_frame", "frame": "cam_dark", "align": "0:0", "camera": None},
                           act("align_solve"), act("check_solve"),
                           {"op": "check_frame", "frame": "wall_430", "kind": "neutral"},
                           {"op": "check_frame", "frame": "wall_430", "kind": "sideways"},
                           act("align_start"), act("projection_open", width=1280, height=800), act("align_start"),
                           {"op": "preview", "frame": "wall_430", "camera": label}, act("align_start"),
                           {"op": "align_frame", "frame": "cam_dark", "align": "7:0", "camera": label},
                           {"op": "align_frame", "frame": "cam_dark", "align": "0:2", "camera": label},
                           {"op": "align_frame", "frame": "cam_dark", "align": "00:1", "camera": label},
                           {"op": "align_frame", "frame": "cam_dark", "align": None, "camera": label},
                           {"op": "align_frame", "frame": "cam_dark", "align": "1:0", "camera": "x" * 201},
                           {"op": "align_frame", "frame": "wall_small", "align": "1:0", "camera": label},
                           act("align_solve"), act("align_cancel"), act("align_cancel"),
                           act("align_start"), act("projection_close"), {"op": "state"},
                           act("projection_open", width=1280, height=800), act("align_start"), *dark,
                           act("align_solve"), act("check_solve"), {"op": "state"},
                           act("launch"), act("align_start")],
        "align_invalidated": [*start, act("align_start"), *feed, act("align_solve"),
                              {"op": "preview", "frame": "wall_430", "camera": "Otra cámara"}, {"op": "state"},
                              act("align_start"), *[{**step, "camera": "Otra cámara"} for step in feed],
                              act("align_solve"), {"op": "preview", "frame": "wall_small", "camera": "Otra cámara"},
                              {"op": "state"}, {"op": "preview", "frame": "wall_430", "camera": "Otra cámara"},
                              act("align_start"), *[{**step, "camera": "Otra cámara"} for step in feed],
                              act("align_solve"), act("camera_changed"), {"op": "state"}],
    }


def controller_scripts() -> dict[str, list[dict[str, Any]]]:
    def act(name, **data):
        return {"op": "action", "payload": {"action": name, **data}}
    open_ = act("projection_open", width=1280, height=800)
    play = [{"op": "tick", "advance": 0.1} for _ in range(30)]
    ready = [{"op": "preview", "frame": "wall_430", "camera": "Cámara de iPhone de Tobias"}, open_]
    return {
        "win": [*ready, act("aim", degrees=0), act("launch"), *play, {"op": "state"}],
        "lose": [*ready, act("aim", degrees=180), act("launch"), *play],
        "aim_buttons": [*ready, act("aim_left"), act("aim_left"), act("aim_right"),
                        act("aim", degrees=725.5), act("aim", degrees=-190), act("aim", degrees="x"),
                        act("aim", degrees=True), act("aim", degrees=None)],
        "guards": [act("launch"), act("calibrate"), act("reset"), act("resume"), act("pause"),
                   act("aim", degrees=0), act("dance"), {"op": "action", "payload": {}},
                   {"op": "action", "payload": {"action": 3}},
                   act("projection_open", width=100, height=800),
                   act("projection_open", width=1280.5, height=800),
                   act("projection_open", width="1280", height=800),
                   {"op": "scan", "frame": "wall_430", "camera": None},
                   open_, {"op": "scan", "frame": "wall_430", "camera": None},
                   act("projection_restore"), act("calibrate"),
                   act("calibration_move", x=10, y=10), act("calibration_confirm")],
        "live_updates": [*ready, {"op": "preview", "frame": "wall_540"},
                         {"op": "state"}, {"op": "preview", "frame": "missing_start"},
                         act("launch"), act("reset"), {"op": "preview", "frame": "duplicate_start"},
                         {"op": "preview", "frame": "no_markers"}, act("launch"),
                         {"op": "preview", "frame": "wall_540"}, {"op": "state"}, act("pause"),
                         {"op": "preview", "frame": "missing_start"}, act("reset"),
                         {"op": "preview", "frame": "no_markers"}, act("reset"),
                         act("projection_open", width=1920, height=1080), {"op": "state"}],
        "stale_detection": [*ready, {"op": "tick", "advance": 1.5}, {"op": "state"},
                            {"op": "tick", "advance": 0.001}, act("launch"), act("reset"),
                            {"op": "preview", "frame": "no_markers"}, act("launch"), act("pause"),
                            {"op": "tick", "advance": 1.501}, {"op": "state"},
                            {"op": "preview", "frame": "wall_430", "camera": "Different Camera"},
                            {"op": "state"}],
        "pause_resume": [*ready, act("launch"), {"op": "tick", "advance": 0.1}, {"op": "tick", "advance": 0.1},
                         act("projection_black"), act("pause"), {"op": "tick", "advance": 0.1}, act("pause"),
                         act("projection_black"), {"op": "scan", "frame": "wall_540", "camera": None},
                         act("launch"), {"op": "tick", "advance": 0.05}, act("projection_close"),
                         {"op": "tick", "advance": 0.1}, act("resume"), open_, act("resume"),
                         {"op": "tick", "advance": 0.4}, {"op": "tick", "advance": 0.1},
                         {"op": "tick", "advance": -0.2}, act("calibrate"), act("reset"), {"op": "state"}],
        "rescan_and_resize": [*ready, act("projection_black"),
                              {"op": "scan", "frame": "wall_540", "camera": "Cámara de iPhone de Tobias"},
                              act("projection_black"), {"op": "scan", "frame": "missing_start", "camera": None},
                              act("projection_restore"),
                              act("projection_black"), {"op": "scan", "frame": "line_of_corners", "camera": None},
                              act("projection_black"), {"op": "scan", "frame": "extra_corner", "camera": None},
                              act("projection_black"), {"op": "scan", "frame": "tiny", "camera": None},
                              act("projection_black"), {"op": "scan", "frame": "wall_430", "camera": "x" * 201},
                              act("camera_changed"), *ready,
                              act("projection_open", width=1024, height=768), {"op": "state"}],
        "palette": [open_, {"op": "preview", "frame": "recolored"}, act("projection_black"),
                    {"op": "scan", "frame": "recolored", "camera": None},
                    {"op": "pick", "frame": "recolored", "role": "marker"},
                    {"op": "pick", "frame": "recolored", "role": "start"},
                    {"op": "pick", "frame": "recolored", "role": "goal"},
                    {"op": "pick", "frame": "recolored", "role": "bumper"},
                    {"op": "preview", "frame": "recolored"}, act("projection_black"),
                    {"op": "scan", "frame": "recolored", "camera": None},
                    act("palette_set", role="goal", hex="#zzzzzz"), act("palette_set", role="sky", hex="#ff0000"),
                    act("palette_set", role="goal", hex="#808080"), act("palette_set", role="goal", hex="#101010"),
                    act("palette_set", role="start", hex=" #F7D84B "),
                    act("palette_reset"), {"op": "preview", "frame": "recolored"},
                    {"op": "preview", "frame": "wall_430"}, {"op": "preview", "frame": "empty_wall"},
                    {"op": "preview", "frame": "extra_corner"}, {"op": "preview", "frame": "tiny"}],
        "camera_presets": [act("camera_preset_save", camera="HD Pro Webcam C920 (046d:082d)", settings=PRESET,
                               score=0.82, black=True, trials=12),
                           act("camera_preset_save", camera="  Cámara FaceTime HD  ", settings={"exposureTime": 20},
                               score="high", black=0),
                           act("camera_preset_save", camera="", settings=PRESET),
                           act("camera_preset_save", camera="c", settings={"exposureMode": "continuous"}),
                           act("camera_preset_save", camera="c", settings={"zoom": 2}),
                           act("camera_preset_save", camera="c", settings={}),
                           act("camera_preset_save", camera="c", settings={"gain": True}),
                           act("camera_preset_save", camera="c", settings=PRESET, capabilities=[1]),
                           act("camera_report", camera="c", report={"a": 1}),
                           act("camera_report", camera="c", report="nope"),
                           act("camera_report", camera="c", report={"big": "x" * 20001}),
                           act("camera_preset_clear", camera="Cámara FaceTime HD"),
                           act("camera_preset_clear", camera="nobody"), {"op": "state"}],
    }


def _snapshot(state: dict[str, Any]) -> dict[str, Any]:
    clean = json.loads(json.dumps(state))
    for entry in clean.get("camera_presets", {}).values():
        entry.pop("saved_at", None)
    return clean


def run_script(steps: list[dict[str, Any]], frames: Frames) -> list[dict[str, Any]]:
    now = [0.0]
    game = GameController(log_path=None, clock=lambda: now[0])
    results = []
    for entry in steps:
        op = entry["op"]
        try:
            if op == "action":
                value = game.action(entry["payload"])
            elif op == "scan":
                value = game.scan_jpeg(frames.jpegs[entry["frame"]] if entry["frame"] in frames.jpegs
                                       else _jpeg(frames.images[entry["frame"]]), entry["camera"])
            elif op == "preview":
                value = game.preview_jpeg(frames.jpegs[entry["frame"]] if entry["frame"] in frames.jpegs
                                          else _jpeg(frames.images[entry["frame"]]), entry.get("camera"))
            elif op == "tick":
                now[0] += entry["advance"]
                value = game.tick()
            elif op == "state":
                value = game.snapshot()
            elif op == "align_frame":
                frame = entry["frame"]
                value = game.align_frame_jpeg(frames.jpegs[frame] if frame in frames.jpegs
                                              else _jpeg(frames.images[frame]), entry["align"], entry.get("camera"))
            elif op == "check_frame":
                frame = entry["frame"]
                value = game.check_frame_jpeg(frames.jpegs[frame] if frame in frames.jpegs
                                              else _jpeg(frames.images[frame]), entry["kind"])
            elif op == "pick":
                value = game.action({"action": "palette_set", "role": entry["role"],
                                     "hex": _sample(frames.images[entry["frame"]], entry["role"])})
            else:
                raise AssertionError(op)
            results.append({"ok": True, "value": _snapshot(value) if "revision" in value else value})
        except RequestError as exc:
            results.append({"ok": False, "status": int(exc.status), "error": str(exc),
                            "state": _snapshot(game.snapshot())})
    return results


# ---------------------------------------------------------------- main

def export() -> dict[str, Any]:
    frames = build_frames()
    vision = vision_cases(frames)   # the wall frames only, before the ones below join
    refine = refine_cases(frames)
    bridges = bridge_cases(frames)
    alignment = alignment_cases(frames)
    probes = probe_cases(frames)
    align_names = _alignment_jpegs(frames)
    _probe_hit_frame(frames, align_names)
    frames.write(GOLDEN / "frames")
    scripts = {**controller_scripts(), **alignment_scripts(align_names)}
    golden = {
        "version": 2,
        "refine": refine,
        "bridge": bridges,
        "offset": offset_cases(),
        "alignment": alignment,
        "probes": probes,
        "frames": frames.meta,
        "palette": palette_cases(),
        "default_palette": DEFAULT_PALETTE.to_json(),
        "vision": vision,
        "homography": homography_cases(),
        "physics": physics_cases(),
        "controller": {name: {"steps": steps, "results": run_script(steps, frames)}
                       for name, steps in scripts.items()},
    }
    (GOLDEN / "golden.json").write_text(json.dumps(golden, ensure_ascii=False, allow_nan=False) + "\n",
                                        encoding="utf-8")
    return golden


if __name__ == "__main__":
    data = export()
    counts = {"frames": len(data["frames"]), "vision": len(data["vision"]), "refine": len(data["refine"]),
              "bridge": len(data["bridge"]), "decodes": len(data["alignment"]["decodes"]),
              "probes": len(data["probes"]),
              "homography": len(data["homography"]), "physics": len(data["physics"]),
              "palette_builds": len(data["palette"]["builds"]),
              "controller_steps": sum(len(s["steps"]) for s in data["controller"].values())}
    print(json.dumps({"written": str(GOLDEN), **counts}))
