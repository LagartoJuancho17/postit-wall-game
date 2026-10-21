"""Hardware-free quality scenario for the browser camera and projector flow.

Run with ``python -m services.web.backend.eval``. This is separate from the
deterministic gate tests and checks an end-to-end synthetic wall scenario.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from contracts import quadrilateral_center
from services.calibration import ALIGNMENT_FRAMES
from services.calibration.tests.render import homography_from_quad, project_to_camera, render_pattern

from .server import GameController, RequestError

# The projector (1280x720) lands keystoned inside the 960x720 synthetic camera.
ALIGN_PROJECTOR = (1280, 720)
ALIGN_QUAD = ((40, 30), (925, 18), (940, 705), (22, 690))


def _color(hue: int) -> tuple[int, int, int]:
    pixel = np.uint8([[[hue, 230, 230]]])
    return tuple(int(channel) for channel in cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0])


FRAME_HUES = {"start": 30, "goal": 12, "bumper": 65, "marker": 157}
# Note centers in _frame, for sampling a color the way the panel's eyedropper does.
FRAME_CENTERS = {"start": (180, 350), "goal": (780, 350), "bumper": (430, 235), "marker": (90, 90)}


def _image(green_x: int, hues: dict[str, int] | None = None, *,
           markers: bool = True) -> np.ndarray:
    hue = {**FRAME_HUES, **(hues or {})}
    image = np.full((720, 960, 3), 220, dtype=np.uint8)
    for x, y in (((90, 90), (870, 90), (870, 630), (90, 630)) if markers else ()):
        cv2.rectangle(image, (x - 22, y - 22), (x + 22, y + 22), _color(hue["marker"]), -1)
    cv2.rectangle(image, (150, 320), (210, 380), _color(hue["start"]), -1)
    cv2.rectangle(image, (750, 320), (810, 380), _color(hue["goal"]), -1)
    cv2.rectangle(image, (green_x - 35, 210), (green_x + 35, 260), _color(hue["bumper"]), -1)
    return image


def _frame(green_x: int, hues: dict[str, int] | None = None, *, markers: bool = True) -> bytes:
    okay, encoded = cv2.imencode(".jpg", _image(green_x, hues, markers=markers),
                                [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert okay
    return encoded.tobytes()


def sampled_hex(jpeg: bytes, role: str) -> str:
    """Mean color of a 5x5 patch at a note center, read from the decoded JPEG."""
    image = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    x, y = FRAME_CENTERS[role]
    blue, green, red = (int(round(c)) for c in image[y - 2:y + 3, x - 2:x + 3].reshape(-1, 3).mean(axis=0))
    return f"#{red:02x}{green:02x}{blue:02x}"


def _counts(preview: dict) -> dict[str, int]:
    return {role: data["count"] for role, data in preview["roles"].items()}


def recolored_wall() -> dict[str, object]:
    """A wall painted in other colors becomes playable by picking them."""
    now = [0.0]
    game = GameController(log_path=None, clock=lambda: now[0])
    game.action({"action": "projection_open", "width": 1280, "height": 800})
    frame = _frame(430, {"start": 105, "goal": 176, "bumper": 28, "marker": 140})
    before = _counts(game.preview_jpeg(frame))
    try:
        game.scan_jpeg(frame)
        default_scan = "accepted"
    except ValueError:
        default_scan = "refused"
    for role in ("marker", "start", "goal", "bumper"):
        game.action({"action": "palette_set", "role": role, "hex": sampled_hex(frame, role)})
    after = _counts(game.preview_jpeg(frame))
    assert game.scan_jpeg(frame)["phase"] == "ready"
    game.action({"action": "aim", "degrees": 0})
    game.action({"action": "launch"})
    for _ in range(25):
        now[0] += 0.1
        game.preview_jpeg(frame)
        state = game.tick()
        if state["game"]["status"] != "running":
            break
    assert default_scan == "refused", "default colors must not read a recolored wall"
    assert after == {"start": 1, "goal": 1, "bumper": 1, "marker": 4}, after
    assert state["game"]["status"] == "won", state["game"]
    return {"preview_default_colors": before, "default_scan": default_scan,
            "preview_picked_colors": after, "attempt_result": state["game"]["status"]}


def _boards_close(first: dict, second: dict, tolerance: float) -> bool:
    """Same board JSON up to `tolerance` px on every coordinate."""
    def flat(board):
        polygons = [board["playfield"], board["start"], board["goal"], *board["bumpers"], *board["markers"]]
        return [(point["x"], point["y"]) for polygon in polygons for point in polygon["points"]]
    a, b = flat(first), flat(second)
    return len(a) == len(b) and all(abs(x1 - x2) <= tolerance and abs(y1 - y2) <= tolerance
                                    for (x1, y1), (x2, y2) in zip(a, b))


def aligned_flow() -> dict[str, object]:
    """Align with coded patterns, then play in projector px (contract 1.5).

    Pass: the mapped board lands within 1.5 projector px of where the true
    homography puts the camera notes, and an attempt aimed at the goal wins.
    """
    now = [0.0]
    game = GameController(log_path=None, clock=lambda: now[0])
    game.action({"action": "projection_open", "width": ALIGN_PROJECTOR[0], "height": ALIGN_PROJECTOR[1]})
    frame = _frame(430, markers=False)
    game.preview_jpeg(frame, "Cámara sintética")
    layout = game.action({"action": "align_start"})["alignment"]["layout"]
    G = homography_from_quad(ALIGN_PROJECTOR, ALIGN_QUAD)
    rng = np.random.default_rng(5)
    for bit, inverse in ALIGNMENT_FRAMES:
        image = project_to_camera(render_pattern(layout, bit, inverse), G, (960, 720),
                                  gain=0.8 * (1 + rng.uniform(-0.2, 0.2)), blur=1.0,
                                  noise=1.5, jpeg=85, rng=rng)
        okay, encoded = cv2.imencode(".jpg", image)
        assert okay
        game.align_frame_jpeg(encoded.tobytes(), f"{bit}:{int(inverse)}", "Cámara sintética")
    solved = game.action({"action": "align_solve"})
    assert solved["alignment"]["status"] == "ready", solved["alignment"]["detail"]
    game.preview_jpeg(frame, "Cámara sintética")
    state = game.snapshot()
    assert state["game"]["space"] == "projector" and state["phase"] == "ready", state["message"]
    truth = np.linalg.inv(G)
    errors = []
    for name in ("start", "goal"):
        camera = np.float32([[p["x"], p["y"]] for p in state["camera_board"][name]["points"]])
        expected = cv2.perspectiveTransform(camera.reshape(-1, 1, 2), truth).reshape(-1, 2)
        mapped = np.float32([[p["x"], p["y"]] for p in state["board"][name]["points"]])
        errors.extend(np.linalg.norm(mapped - expected, axis=1).tolist())
    start = quadrilateral_center(game.board.start)
    goal = quadrilateral_center(game.board.goal)
    game.action({"action": "aim", "degrees": float(np.degrees(np.arctan2(goal.y - start.y, goal.x - start.x)))})
    game.action({"action": "launch"})
    for _ in range(40):
        now[0] += 0.1
        current = game.tick()
        if current["game"]["status"] != "running":
            break
    max_error = max(errors)
    assert max_error <= 1.5, f"mapped board error {max_error:.2f} px"
    assert current["game"]["status"] == "won", current["game"]
    quality = solved["alignment"]["quality"]
    return {"mapped_board_error_px": round(max_error, 3), "decoded": quality["decoded"],
            "holdout_p95_px": quality["holdout_p95_px"], "attempt_result": current["game"]["status"],
            "space": state["game"]["space"]}


def run() -> dict[str, object]:
    now = [0.0]
    with TemporaryDirectory() as directory:
        log_path = Path(directory) / "events.jsonl"
        game = GameController(log_path=log_path, clock=lambda: now[0])
        opened = game.action({"action": "projection_open", "width": 1280, "height": 800})
        assert opened["projection"]["mode"] == "live"
        assert "calibration" not in opened
        frame = _frame(430, markers=False)
        preview = game.preview_jpeg(frame)
        ready = game.snapshot()
        assert ready["phase"] == "ready"
        assert ready["live_detection"] == preview
        assert ready["board"] == ready["camera_board"]
        assert len(ready["board"]["markers"]) == 0
        assert preview["quality"]["ready"]
        camera_start = quadrilateral_center(game.camera_board.start)
        game_start = quadrilateral_center(game.board.start)
        coordinate_error = max(abs(game_start.x - camera_start.x),
                               abs(game_start.y - camera_start.y))
        assert coordinate_error == 0

        game.action({"action": "aim", "degrees": 0})
        launched = game.action({"action": "launch"})
        frozen_board, frozen_position = game.board, game.game.position
        moved_frame = _frame(540, markers=False)
        game.preview_jpeg(moved_frame)
        live = game.snapshot()
        assert game.board == frozen_board and game.game.position == frozen_position
        assert live["camera_board"]["bumpers"] != live["board"]["bumpers"]
        resized = game.action({"action": "projection_open", "width": 1920, "height": 1080})
        assert resized["board"] == launched["board"]
        assert resized["game"] == launched["game"]
        for _ in range(30):
            now[0] += 0.1
            game.preview_jpeg(moved_frame)
            current = game.tick()
            if current["game"]["status"] != "running":
                break
        assert current["game"]["status"] == "won", current["game"]
        elapsed_s = current["game"]["elapsed_s"]
        assert 1.5 < elapsed_s < 2.5
        reset = game.action({"action": "reset"})
        # The latest detection of the same wall: equal up to sub-pixel noise
        # (later frames also discount the projector's own rims, contract 2.5).
        assert _boards_close(reset["board"], live["camera_board"], 0.25), (reset["board"], live["camera_board"])
        assert reset["game"]["attempts"] == 1
        shift_px = (quadrilateral_center(game.board.bumpers[0]).x
                    - quadrilateral_center(frozen_board.bumpers[0]).x)
        assert shift_px > 100

        partial = _image(540, markers=False)
        partial[315:386, 145:216] = 220
        okay, encoded = cv2.imencode(".jpg", partial)
        assert okay
        game.preview_jpeg(encoded.tobytes())
        invalid = game.snapshot()
        assert invalid["board"] is None
        assert invalid["live_detection"]["roles"]["bumper"]["count"] == 1
        try:
            game.action({"action": "launch"})
        except RequestError:
            invalid_launch_refused = True
        else:
            raise AssertionError("partial live wall must not launch an old board")
        game.preview_jpeg(frame)
        now[0] += 1.501
        expired = game.snapshot()
        assert expired["live_detection"] is None and expired["board"] is None
        events = [json.loads(line)["event"] for line in log_path.read_text().splitlines()]
        assert "live_board_ready" in events
        assert "live_board_invalid" in events
        assert "live_detection_expired" in events
        assert "attempt_finished" in events
        return {"score": 1.0, "pass_threshold": 1.0,
                "camera_coordinate_error_px": coordinate_error,
                "optional_marker_count": 0,
                "attempt_result": current["game"]["status"],
                "elapsed_s": elapsed_s,
                "bumper_shift_px": round(shift_px, 1),
                "attempt_geometry_preserved": True,
                "projector_resize_preserved_attempt": True,
                "invalid_launch_refused": invalid_launch_refused,
                "expired_detection_hidden": expired["live_detection"] is None,
                "event_count": len(events), "recolored_wall": recolored_wall(),
                "aligned_flow": aligned_flow()}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
