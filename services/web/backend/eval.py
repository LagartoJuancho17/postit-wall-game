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


def _frame(green_x: int) -> bytes:
    image = np.full((720, 960, 3), 220, dtype=np.uint8)
    for x, y in ((90, 90), (870, 90), (870, 630), (90, 630)):
        cv2.rectangle(image, (x - 22, y - 22), (x + 22, y + 22), _color(157), -1)
    cv2.rectangle(image, (150, 320), (210, 380), _color(30), -1)
    cv2.rectangle(image, (750, 320), (810, 380), _color(12), -1)
    cv2.rectangle(image, (green_x - 35, 210), (green_x + 35, 260), _color(65), -1)
    okay, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 96])
    assert okay
    return encoded.tobytes()


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
                "event_count": len(events)}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
