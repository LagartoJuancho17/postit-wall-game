"""Simulated rooms for the fixed-camera search, scored by the real backend.

Each room has a light color (kelvin) and a light level, expressed as the
exposure that would show the wall at its true gray. For every exposure and
white balance in the panel's search grid, this renders what a fixed camera
would capture, sends it through ``GameController.preview_jpeg`` and keeps the
counts and quality. ``evals/flows.mjs`` then runs the panel's chooser on these
trials and checks it picks a setting the scan accepts.

    ./.venv/bin/python -m services.web.contract.camera_trials          # write
    ./.venv/bin/python -m services.web.contract.camera_trials --check  # verify
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from services.web.backend.eval import _image
from services.web.backend.server import GameController

TRIALS_PATH = Path(__file__).with_name("camera-trials.json")
# Must equal the grid ui-core.mjs builds for a C920 (a test checks it).
GRID_EXPOSURE = [50, 100, 200, 400, 800]        # 100 µs units: 5 to 80 ms
GRID_TEMPERATURE = [3000, 4000, 5000, 6000]     # kelvin
ROOMS = [
    {"name": "neutral", "light_k": 4500, "ideal_exposure": 260},
    {"name": "warm_lamps", "light_k": 3000, "ideal_exposure": 350},
    {"name": "cool_daylight", "light_k": 6000, "ideal_exposure": 130},
    {"name": "dim", "light_k": 4000, "ideal_exposure": 700},
    {"name": "bright", "light_k": 5000, "ideal_exposure": 70},
]


def kelvin_rgb(kelvin: float) -> np.ndarray:
    """Color of a black-body light, normalized to green (Tanner Helland fit)."""
    t = kelvin / 100
    red = 255 if t <= 66 else 329.698727446 * (t - 60) ** -0.1332047592
    green = (99.4708025861 * math.log(t) - 161.1195681661 if t <= 66
             else 288.1221695283 * (t - 60) ** -0.0755148492)
    blue = 255 if t >= 66 else 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    rgb = np.clip(np.array([red, green, blue], np.float64), 1, 255)
    return rgb / rgb[1]


def capture(room: dict[str, Any], exposure: float, white_balance: float, seed: int) -> bytes:
    """What a camera with fixed exposure and white balance sees in this room."""
    reflectance = _image(430).astype(np.float64) / 255
    cast = kelvin_rgb(room["light_k"]) / kelvin_rgb(white_balance)  # RGB, neutral when equal
    light = cast[::-1] * (exposure / room["ideal_exposure"])        # BGR order
    noise = np.random.default_rng(seed).normal(0, 2.0, reflectance.shape)
    frame = np.clip(reflectance * light * 255 + noise, 0, 255).astype(np.uint8)
    okay, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert okay
    return encoded.tobytes()


def build_trials() -> dict[str, Any]:
    game = GameController(log_path=None)
    rooms = []
    for room_index, room in enumerate(ROOMS):
        trials = []
        for exposure in GRID_EXPOSURE:
            for temperature in GRID_TEMPERATURE:
                seed = room_index * 1000 + exposure + temperature
                preview = game.preview_jpeg(capture(room, exposure, temperature, seed))
                trials.append({
                    "settings": {"exposureTime": exposure, "colorTemperature": temperature},
                    "preview": {"roles": {role: {"count": data["count"], "expected": data["expected"]}
                                          for role, data in preview["roles"].items()},
                                "quality": preview["quality"]},
                })
        rooms.append({**room, "trials": trials})
    return {"version": 1, "grid": {"exposureTime": GRID_EXPOSURE,
                                   "colorTemperature": GRID_TEMPERATURE},
            "rooms": rooms}


def differences(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Counts and readiness must match exactly; measures within a tolerance."""
    problems = []
    if old["grid"] != new["grid"] or [r["name"] for r in old["rooms"]] != [r["name"] for r in new["rooms"]]:
        return ["grid or rooms changed"]
    tolerance = {"clipped": 0.02, "dark": 0.02, "brightness": 0.02, "note_saturation": 6.0}
    for old_room, new_room in zip(old["rooms"], new["rooms"]):
        for old_trial, new_trial in zip(old_room["trials"], new_room["trials"]):
            where = f"{new_room['name']} {new_trial['settings']}"
            a, b = old_trial["preview"], new_trial["preview"]
            if a["roles"] != b["roles"] or a["quality"]["ready"] != b["quality"]["ready"]:
                problems.append(f"{where}: counts or ready changed")
            for key, limit in tolerance.items():
                x, y = a["quality"][key], b["quality"][key]
                if (x is None) != (y is None) or (x is not None and abs(x - y) > limit):
                    problems.append(f"{where}: {key} {x} -> {y}")
    return problems


def main(argv: list[str]) -> int:
    trials = build_trials()
    if "--check" in argv:
        problems = differences(json.loads(TRIALS_PATH.read_text(encoding="utf-8")), trials)
        print(json.dumps({"camera_trials_match": not problems, "problems": problems[:10]},
                         ensure_ascii=False))
        return 1 if problems else 0
    TRIALS_PATH.write_text(json.dumps(trials, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {TRIALS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
