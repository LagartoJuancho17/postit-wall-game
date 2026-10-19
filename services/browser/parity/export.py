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
from services.calibration import estimate_homography, transform_board, transform_point
from services.physics import GameStatus, launch, ready_state, step
from services.vision import build_palette, detect_board, find_notes, frame_quality, hsv_from_hex, rule_from_hex
from services.vision.palette import DEFAULT_PALETTE
from services.vision.tests.scene import hex_at, scene
from services.web.backend.eval import FRAME_CENTERS, _image
from services.web.backend.server import GameController, RequestError, _board_json, _polygon_json

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

    def add_jpeg(self, name: str, image: np.ndarray) -> np.ndarray:
        data = _jpeg(image)
        self.jpegs[name] = data
        return self.add_raw(name, cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR))

    def write(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        for old in directory.glob("*.bgr.gz"):
            old.unlink()
        for name, image in self.images.items():
            with gzip.open(directory / f"{name}.bgr.gz", "wb", compresslevel=6) as stream:
                stream.write(np.ascontiguousarray(image).tobytes())


def build_frames() -> Frames:
    frames = Frames()
    frames.add_jpeg("wall_430", _image(430))
    frames.add_jpeg("wall_540", _image(540))
    frames.add_jpeg("recolored", _image(430, RECOLORED))
    no_start = _image(430)
    no_start[315:386, 145:216] = 220
    frames.add_jpeg("missing_start", no_start)
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


def _confirm_corners(frame: str, dx: float = 80, dy: float = 30) -> list[dict[str, Any]]:
    return [{"op": "confirm_corner", "index": i, "dx": dx, "dy": dy} for i in range(4)]


def controller_scripts() -> dict[str, list[dict[str, Any]]]:
    def act(name, **data):
        return {"op": "action", "payload": {"action": name, **data}}
    open_ = act("projection_open", width=1280, height=800)
    play = [{"op": "tick", "advance": 0.1} for _ in range(30)]
    ready = [open_, act("projection_black"), {"op": "scan", "frame": "wall_430", "camera": "Cámara de iPhone de Tobias"},
             act("calibrate"), *_confirm_corners("wall_430")]
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
        "calibration_moves": [open_, act("projection_black"), {"op": "scan", "frame": "wall_430", "camera": None},
                              act("calibrate"), act("calibration_move", x=-1, y=5),
                              act("calibration_move", x=1280, y=5), act("calibration_move", x=400.25, y=300.5),
                              act("calibration_confirm"), act("calibration_move", x=900, y=200),
                              act("calibration_confirm", x=901, y=201),
                              act("calibration_confirm", x=900, y=650), act("projection_close"),
                              open_, act("calibration_confirm", x=350, y=640), {"op": "state"}],
        "bad_calibration": [open_, act("projection_black"), {"op": "scan", "frame": "wall_430", "camera": None},
                            act("calibrate"), act("calibration_confirm", x=100, y=100),
                            act("calibration_confirm", x=101, y=101), act("calibration_confirm", x=102, y=102),
                            act("calibration_confirm", x=103, y=103), act("calibrate"),
                            act("calibration_confirm", x=5, y=5), act("calibration_confirm", x=1275, y=5),
                            act("calibration_confirm", x=1275, y=795), act("calibration_confirm", x=5, y=795)],
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
                                          else _jpeg(frames.images[entry["frame"]]))
            elif op == "tick":
                now[0] += entry["advance"]
                value = game.tick()
            elif op == "state":
                value = game.snapshot()
            elif op == "pick":
                value = game.action({"action": "palette_set", "role": entry["role"],
                                     "hex": _sample(frames.images[entry["frame"]], entry["role"])})
            elif op == "confirm_corner":
                point = quadrilateral_center(game.camera_board.markers[entry["index"]])
                value = game.action({"action": "calibration_confirm",
                                     "x": point.x + entry["dx"], "y": point.y + entry["dy"]})
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
    frames.write(GOLDEN / "frames")
    scripts = controller_scripts()
    golden = {
        "version": 1,
        "frames": frames.meta,
        "palette": palette_cases(),
        "default_palette": DEFAULT_PALETTE.to_json(),
        "vision": vision_cases(frames),
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
    counts = {"frames": len(data["frames"]), "vision": len(data["vision"]),
              "homography": len(data["homography"]), "physics": len(data["physics"]),
              "palette_builds": len(data["palette"]["builds"]),
              "controller_steps": sum(len(s["steps"]) for s in data["controller"].values())}
    print(json.dumps({"written": str(GOLDEN), **counts}))
