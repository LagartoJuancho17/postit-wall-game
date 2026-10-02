"""Hardware-free quality scenario for the browser camera and projector flow.

Run with ``python -m services.web.backend.eval``. This is separate from the
deterministic gate tests and checks an end-to-end synthetic wall scenario.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory

import cv2
import numpy as np

from contracts import quadrilateral_center

from .server import GameController


def _color(hue: int) -> tuple[int, int, int]:
    pixel = np.uint8([[[hue, 230, 230]]])
    return tuple(int(channel) for channel in cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0])


FRAME_HUES = {"start": 30, "goal": 12, "bumper": 65, "marker": 157}
# Note centers in _frame, for sampling a color the way the panel's eyedropper does.
FRAME_CENTERS = {"start": (180, 350), "goal": (780, 350), "bumper": (430, 235), "marker": (90, 90)}


def _image(green_x: int, hues: dict[str, int] | None = None) -> np.ndarray:
    hue = {**FRAME_HUES, **(hues or {})}
    image = np.full((720, 960, 3), 220, dtype=np.uint8)
    for x, y in ((90, 90), (870, 90), (870, 630), (90, 630)):
        cv2.rectangle(image, (x - 22, y - 22), (x + 22, y + 22), _color(hue["marker"]), -1)
    cv2.rectangle(image, (150, 320), (210, 380), _color(hue["start"]), -1)
    cv2.rectangle(image, (750, 320), (810, 380), _color(hue["goal"]), -1)
    cv2.rectangle(image, (green_x - 35, 210), (green_x + 35, 260), _color(hue["bumper"]), -1)
    return image


def _frame(green_x: int, hues: dict[str, int] | None = None) -> bytes:
    okay, encoded = cv2.imencode(".jpg", _image(green_x, hues), [cv2.IMWRITE_JPEG_QUALITY, 96])
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
    assert game.scan_jpeg(frame)["phase"] == "scanned"
    game.action({"action": "calibrate"})
    for marker in game.camera_board.markers:
        p = quadrilateral_center(marker)
        game.action({"action": "calibration_confirm", "x": p.x + 80, "y": p.y + 30})
    game.action({"action": "aim", "degrees": 0})
    game.action({"action": "launch"})
    for _ in range(25):
        now[0] += 0.1
        state = game.tick()
        if state["game"]["status"] != "running":
            break
    assert default_scan == "refused", "default colors must not read a recolored wall"
    assert after == {"start": 1, "goal": 1, "bumper": 1, "marker": 4}, after
    assert state["game"]["status"] == "won", state["game"]
    return {"preview_default_colors": before, "default_scan": default_scan,
            "preview_picked_colors": after, "attempt_result": state["game"]["status"]}


def run() -> dict[str, float | int | str]:
    now = [0.0]
    with TemporaryDirectory() as directory:
        log_path = Path(directory) / "events.jsonl"
        game = GameController(log_path=log_path, clock=lambda: now[0])
        game.action({"action": "projection_open", "width": 1280, "height": 800})
        scan = game.scan_jpeg(_frame(430))
        assert scan["phase"] == "scanned"
        assert scan["camera"]["width"] == 960
        assert len(scan["camera_board"]["markers"]) == 4

        game.action({"action": "calibrate"})
        for marker in game.camera_board.markers:
            p = quadrilateral_center(marker)
            ready = game.action({"action": "calibration_confirm",
                                 "x": p.x + 80, "y": p.y + 30})
        assert ready["phase"] == "ready"
        assert ready["board"] is not None
        camera_start = quadrilateral_center(game.camera_board.start)
        mapped_start = quadrilateral_center(game.board.start)
        mapping_error = math.hypot(mapped_start.x - camera_start.x - 80,
                                   mapped_start.y - camera_start.y - 30)
        assert mapping_error < 1.0, f"manual mapping error {mapping_error:.3f} px"

        game.action({"action": "aim", "degrees": 0})
        game.action({"action": "launch"})
        for _ in range(25):
            now[0] += 0.1
            current = game.tick()
            if current["game"]["status"] != "running":
                break
        assert current["game"]["status"] == "won", current["game"]
        elapsed_s = current["game"]["elapsed_s"]
        assert 1.5 < elapsed_s < 2.5

        original_green = quadrilateral_center(game.board.bumpers[0])
        game.action({"action": "projection_black"})
        rescan = game.scan_jpeg(_frame(540))
        moved_green = quadrilateral_center(game.board.bumpers[0])
        shift_px = moved_green.x - original_green.x
        assert rescan["phase"] == "ready"
        assert shift_px > 100
        assert rescan["game"]["attempts"] == 1

        resized = game.action({"action": "projection_open", "width": 1024, "height": 768})
        assert resized["calibration"]["status"] == "none"
        assert resized["board"] is None
        assert resized["camera_board"] is None
        events = [json.loads(line)["event"] for line in log_path.read_text().splitlines()]
        assert events.count("board_scanned") == 2
        assert "calibration_completed" in events
        assert "attempt_finished" in events
        return {"mapping_error_px": round(mapping_error, 3),
                "attempt_result": current["game"]["status"],
                "elapsed_s": elapsed_s,
                "bumper_shift_px": round(shift_px, 1),
                "event_count": len(events),
                "recolored_wall": recolored_wall()}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
