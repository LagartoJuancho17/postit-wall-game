"""Eyedropper eval: does a click on a note give a color the detector can use?

Run with ``python -m services.web.evals.eyedropper`` (needs node). Paid lane in
spirit: slower than a gate test, measures quality with a pass threshold.

The panel's real JavaScript (``ui-core.mjs``: patchOrigin, noteColorHex,
weakColor, pickVerdict) reads and judges the color, the real backend
(GameController.palette_set and preview_jpeg) detects with it. Walls are the
recolored wall from the backend eval under seven conditions: clean, dim, warm
light, sensor noise with stronger JPEG, soft focus, and a beige wall sharp
and soft. Clicks land at the note center and at 50 %, 80 %, 100 %, 110 % and
120 % of the way to each edge: "inner" rings stay on the note, "edge" puts
half the sampled square on the wall, "outside" misses the note.

A click succeeds when, with the other roles picked at their centers, the
preview finds every note of every role (1 start, 1 goal, 1 bumper, 4 corners).
A failed click must never be silent: the server refuses the color with a
reason, the panel flags it as pale, or pickVerdict explains what the next
preview shows. The plain average (averageHex, the previous eyedropper) is
scored on the same clicks as the baseline.

Measured on 2026-10-08: inner 100 %, edge 100 %, outside 51 % (plain average
35 %, 12.5 % on the beige wall), 0 silent failures out of 588 clicks.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import cv2
import numpy as np

from services.web.backend.eval import _image
from services.web.backend.server import GameController, RequestError

UI_CORE = (Path(__file__).resolve().parent.parent / "ui" / "ui-core.mjs").as_uri()
RECOLORED = {"start": 105, "goal": 176, "bumper": 28, "marker": 140}
EXPECTED = {"start": 1, "goal": 1, "bumper": 1, "marker": 4}
# A painted wall with a warm tint: mixing it into a note shifts the note's hue.
BEIGE_BGR = (150, 185, 210)
# Center and half size (x, y) of one note per role, as drawn by _image(430).
NOTES = {"start": ((180, 350), (30, 30)), "goal": ((780, 350), (30, 30)),
         "bumper": ((430, 235), (35, 25)), "marker": ((90, 90), (22, 22))}
REACH = (0.5, 0.8, 1.0, 1.1, 1.2)
DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1))
# Pass thresholds: inside the note every click works; with half the square on
# the wall, at least 90 % do; no failure is silent; and no ring is worse than
# with the plain average.
INNER_MIN = 1.0
EDGE_MIN = 0.9


def _variants() -> dict[str, bytes]:
    gray = _image(430, RECOLORED)
    beige = gray.copy()
    beige[(gray == 220).all(axis=2)] = BEIGE_BGR
    base, tinted = gray.astype(np.float32), beige.astype(np.float32)
    noise = np.random.default_rng(7).normal(0, 6, base.shape).astype(np.float32)
    images = {
        "clean": (base, 96),
        "dim": (base * 0.6, 96),
        "warm": (base * np.float32([0.8, 0.95, 1.08]), 96),  # BGR: less blue, more red
        "noisy": (base + noise, 80),
        "soft_focus": (cv2.GaussianBlur(base, (9, 9), 0), 96),
        "beige_wall": (tinted, 96),
        "beige_soft_focus": (cv2.GaussianBlur(tinted, (9, 9), 0), 96),
    }
    frames = {}
    for name, (image, quality) in images.items():
        okay, encoded = cv2.imencode(".jpg", np.clip(image, 0, 255).astype(np.uint8),
                                     [cv2.IMWRITE_JPEG_QUALITY, quality])
        assert okay
        frames[name] = encoded.tobytes()
    return frames


def _clicks(role: str) -> list[tuple[float, tuple[float, float]]]:
    (cx, cy), (hx, hy) = NOTES[role]
    clicks = [(0.0, (cx, cy))]
    for reach in REACH:
        clicks += [(reach, (cx + dx * hx * reach, cy + dy * hy * reach)) for dx, dy in DIRECTIONS]
    return clicks


def _node(script: str, payload: object) -> object:
    result = subprocess.run(["node", "--input-type=module", "-e", script], input=json.dumps(payload),
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _reader() -> str:
    return f"""
      import {{ averageHex, noteColorHex, patchOrigin, pickVerdict, SAMPLE_SIZE, weakColor }} from {json.dumps(UI_CORE)};
      let input = ""; for await (const chunk of process.stdin) input += chunk;
      const request = JSON.parse(input);
      const out = request.verdicts
        ? {{ verdicts: request.verdicts.map((v) => pickVerdict(v.role, v.hex, v.before, v.after)) }}
        : request.patches
        ? {{ note: request.patches.map(noteColorHex), average: request.patches.map(averageHex),
             weak: request.patches.map((patch) => weakColor(noteColorHex(patch))) }}
        : {{ size: SAMPLE_SIZE, origins: request.points.map((p) => patchOrigin(p, p.w, p.h, SAMPLE_SIZE)) }};
      process.stdout.write(JSON.stringify(out));
    """


def _read_patches(cases: list[dict]) -> dict[str, list]:
    """The panel's own code: where the square lands, its color both ways, and
    whether the panel would warn that the pick is pale."""
    reader = _reader()
    placed = _node(reader, {"points": [{"x": c["x"], "y": c["y"], "w": c["w"], "h": c["h"]} for c in cases]})
    size, patches = placed["size"], []
    for case, origin in zip(cases, placed["origins"]):
        square = case["rgb"][origin["y"]:origin["y"] + size, origin["x"]:origin["x"] + size]
        rgba = np.concatenate([square, np.full((size, size, 1), 255, np.uint8)], axis=2)
        patches.append(rgba.reshape(-1).tolist())
    return _node(reader, {"patches": patches})


def _counts(game: GameController, frame: bytes) -> dict[str, int]:
    return {role: data["count"] for role, data in game.preview_jpeg(frame)["roles"].items()}


def _outcome(game: GameController, frame: bytes, role: str, hex_color: str,
             fallback: str) -> tuple[str, dict | None]:
    """found: every note detected; refused: the server said why; wrong: it
    took the color and detection broke (with the preview the panel gets)."""
    try:
        game.action({"action": "palette_set", "role": role, "hex": hex_color})
        preview = game.preview_jpeg(frame)
        counts = {name: data["count"] for name, data in preview["roles"].items()}
        outcome = ("found", None) if counts == EXPECTED else ("wrong", preview)
    except RequestError:
        outcome = ("refused", None)
    game.action({"action": "palette_set", "role": role, "hex": fallback})
    return outcome


def run() -> dict[str, object]:
    frames = _variants()
    cases = []
    for variant, jpeg in frames.items():
        bgr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        for role in NOTES:
            for reach, (x, y) in _clicks(role):
                cases.append({"variant": variant, "role": role, "reach": reach, "x": x, "y": y,
                              "w": rgb.shape[1], "h": rgb.shape[0], "rgb": rgb})
    read = _read_patches(cases)
    for case, note_hex, average_hex, weak in zip(cases, read["note"], read["average"], read["weak"]):
        case.update(note_hex=note_hex, average_hex=average_hex, weak=weak)

    tally: dict[str, dict[str, list[int]]] = {"note": {}, "average": {}}
    failures, wrong = [], []
    before = {"roles": {role: {"count": count} for role, count in EXPECTED.items()}}
    for variant, frame in frames.items():
        centers = {c["role"]: c["note_hex"] for c in cases if c["variant"] == variant and c["reach"] == 0}
        game = GameController(log_path=None)
        for role, hex_color in centers.items():
            game.action({"action": "palette_set", "role": role, "hex": hex_color})
        assert _counts(game, frame) == EXPECTED, f"{variant}: center picks miss notes"
        for case in (c for c in cases if c["variant"] == variant):
            ring = "inner" if case["reach"] < 1 else "edge" if case["reach"] == 1 else "outside"
            for method in ("note", "average"):
                outcome, preview = _outcome(game, frame, case["role"], case[f"{method}_hex"],
                                            centers[case["role"]])
                for key in (ring, f"{variant}/{ring}"):
                    tally[method].setdefault(key, [0, 0])
                    tally[method][key][0] += outcome == "found"
                    tally[method][key][1] += 1
                if method == "note" and outcome != "found":
                    summary = {k: case[k] for k in ("variant", "role", "reach", "note_hex", "weak")}
                    failures.append({**summary, "outcome": outcome})
                    if outcome == "wrong":
                        after = {"roles": {name: {"count": data["count"]}
                                           for name, data in preview["roles"].items()}}
                        wrong.append({**summary, "hex": case["note_hex"], "before": before, "after": after})

    # The panel's own judgment of each wrong color: pale flag or verdict message.
    verdicts = _node(_reader(), {"verdicts": wrong})["verdicts"] if wrong else []
    silent = [{k: w[k] for k in ("variant", "role", "reach", "note_hex")} | {"seen": w["after"]["roles"]}
              for w, verdict in zip(wrong, verdicts) if not w["weak"] and not verdict]

    rate = {method: {ring: round(hit / total, 3) for ring, (hit, total) in rings.items()}
            for method, rings in tally.items()}
    report = {"clicks": len(cases), "eyedropper": rate["note"], "plain_average": rate["average"],
              "failures": len(failures), "silent_failures": silent,
              "failure_outcomes": {kind: sum(f["outcome"] == kind for f in failures) for kind in ("refused", "wrong")},
              "flagged_pale": sum(f["weak"] for f in failures)}
    assert rate["note"]["inner"] >= INNER_MIN, report
    assert rate["note"]["edge"] >= EDGE_MIN, report
    assert not silent, report
    for ring in rate["note"]:
        assert rate["note"][ring] >= rate["average"][ring], report
    return report


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False, sort_keys=True))
