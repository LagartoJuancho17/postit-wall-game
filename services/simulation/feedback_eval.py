"""Closed-loop feedback eval: does what the projector draws change what the camera detects?

Run with ``python -m services.simulation.feedback_eval`` (``--jobs N`` runs
cases in parallel; results do not depend on it, ``--only NAME`` runs some
cases, ``--out PATH`` also writes the JSON).

The real setup: an iPhone camera at 1280x720 and a 1920x1080 projector whose
image lands on the wall with a keystone (``model.standard_rig``). Each case
drives the real ``GameController`` through ``preview_jpeg`` for 10 warm-up
frames plus 60 measured frames (one per 250 ms preview). The backend has no
alignment endpoints yet, so aligned cases hand the projector the camera ->
projector matrix directly: the true ``inverse(G)``, or that plus a 2 px
error.

Measured after warm-up (docs/alignment-and-color.md, "Measurable outcomes"):

- flips: ready/invalid changes of the live board not explained by the wall
  (a removed goal explains one); wrong-ready frames: frames where the board
  disagrees with the wall (a board stuck invalid never flips);
- phantoms: detections that touch no paper on the wall nor a removed one;
- max area growth: largest detected-area / true-area - 1 of any paper, and
  the same against that paper detected under the neutral projection
  (detections run ~10% smaller than the true paper, so growth vs truth
  alone would let ~15% of real growth through);
- ghost lifetime: frames from the removal until the removed paper's last
  detection disappears;
- rim corner error (aligned): the rim the projector draws for the detection
  vs the rim the true outline would get, both offset 5 px, projector px. The
  same metric under the neutral projection (``neutral_reference``) says how
  much of it detection costs with no feedback at all; ``min_rim_clearance_px``
  < 0 means the rim stroke lands on the paper.

Pass for the NEW rules: 0 flips, 0 wrong-ready frames, 0 phantoms, both
growths <= 5%, ghost lifetime <= 3 frames, rim corner error <= 3 px.
OLD-rule cases are the baseline: the projector before the contract, reported
but not scored. The JSON is ``{score, pass_threshold, limits, setup,
baseline, cases}``; ``score`` is the fraction of NEW cases that pass and the
exit status is non-zero when any NEW case fails.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

from services.web.backend.server import GameController

from .loop import SimClock, run_closed_loop
from .model import apply_homography, normalized_inverse, standard_rig
from .projector import render_scene

WARMUP_FRAMES = 10
MEASURED_FRAMES = 60
FRAMES = WARMUP_FRAMES + MEASURED_FRAMES
REMOVE_AT = 20            # absolute frame index of the first frame without the paper
REMOVED_ROLE = "goal"     # the measured failure: the goal's orange halo
REFERENCE_FRAMES = 5      # open-loop frames under the neutral projection
ALIGNMENT_ERROR = (1.6, 1.2)  # projector px: a 2.0 px error at every point
PASS_THRESHOLD = 1.0
LIMITS = {"flips": 0, "phantoms": 0, "max_area_growth": 0.05,
          "ghost_lifetime_frames": 3, "max_rim_corner_error_px": 3.0, "wrong_ready_frames": 0}
DARK_ROOM = 0.01    # ambient irradiance; the projector's full white is 1.0
NORMAL_ROOM = 0.1
BRIGHT_ROOM = 1.0


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    rules: str                    # "old" (baseline) or "new" (scored)
    theme: str = "dark"
    aligned: str | None = None    # None, "true" (inverse(G)) or "error_2px"
    fill: bool = False
    ambient: float = NORMAL_ROOM
    auto_exposure: bool = False
    remove: bool = False          # take the goal down at REMOVE_AT
    seed: int = 1


SCENARIOS = (
    Scenario("old_unaligned_dark", "Proyector anterior, tema oscuro", "old"),
    Scenario("old_unaligned_light", "Proyector anterior, tema claro", "old", theme="light"),
    Scenario("old_unaligned_dark_goal_removed", "Proyector anterior, se saca la llegada", "old",
             remove=True),
    Scenario("new_unaligned_dark", "Sin alinear, solo bordes neutros, tema oscuro", "new"),
    Scenario("new_unaligned_light", "Sin alinear, solo bordes neutros, tema claro", "new", theme="light"),
    Scenario("new_unaligned_dark_goal_removed", "Sin alinear, se saca la llegada", "new", remove=True),
    Scenario("new_aligned_fill_off", "Alineado exacto, sin relleno", "new", aligned="true"),
    Scenario("new_aligned_fill_on", "Alineado exacto, con relleno", "new", aligned="true", fill=True),
    Scenario("new_aligned_error2px_fill_off", "Alineado con 2 px de error, sin relleno", "new",
             aligned="error_2px"),
    Scenario("new_aligned_error2px_fill_on", "Alineado con 2 px de error, con relleno", "new",
             aligned="error_2px", fill=True),
    Scenario("new_aligned_fill_on_goal_removed", "Alineado con relleno, se saca la llegada", "new",
             aligned="true", fill=True, remove=True),
    Scenario("new_aligned_fill_on_dark_room", "Alineado con relleno, sala a oscuras", "new",
             aligned="true", fill=True, ambient=DARK_ROOM),
    Scenario("new_aligned_fill_on_dark_room_goal_removed",
             "Alineado con relleno, sala a oscuras, se saca la llegada", "new",
             aligned="true", fill=True, ambient=DARK_ROOM, remove=True),
    Scenario("new_aligned_fill_on_bright_room", "Alineado con relleno, sala iluminada", "new",
             aligned="true", fill=True, ambient=BRIGHT_ROOM),
    Scenario("new_aligned_fill_on_light_theme", "Alineado con relleno, tema claro (pared inundada)", "new",
             theme="light", aligned="true", fill=True),
    Scenario("new_aligned_fill_on_auto_exposure", "Alineado con relleno, exposición automática", "new",
             aligned="true", fill=True, auto_exposure=True),
)
BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


def _translation(dx: float, dy: float) -> np.ndarray:
    return np.array([[1.0, 0.0, dx], [0.0, 1.0, dy], [0.0, 0.0, 1.0]])


def _transitions(values: list[bool], start: int) -> int:
    return sum(values[index] != values[index - 1] for index in range(start, len(values)))


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


def _stats(values: list[float]) -> tuple[float | None, float | None]:
    """(max, p95) of a list, rounded; (None, None) when empty."""
    if not values:
        return None, None
    return _round(max(values), 3), _round(float(np.percentile(values, 95)), 3)


def _reference(scenario: Scenario, gain: float | None,
               matrix: np.ndarray | None) -> tuple[list[float | None], list[float]]:
    """The same wall seen under the neutral projection (open loop, nothing drawn).

    Returns the median detected area per paper and, for aligned cases, the
    rim corner errors those detections would get: what detection alone
    costs, with no feedback at all.
    """
    wall, projector, camera = standard_rig(ambient=scenario.ambient, seed=scenario.seed + 1000,
                                           fixed_gain=gain)
    clock = SimClock()
    controller = GameController(log_path=None, clock=clock)
    records = run_closed_loop(controller, wall, projector, camera, frames=REFERENCE_FRAMES,
                              rules="neutral", theme=scenario.theme, alignment_matrix=matrix,
                              clock=clock)
    medians = []
    for index in range(len(wall.papers)):
        areas = [record["areas"][index] for record in records if record["areas"][index] is not None]
        medians.append(float(np.median(areas)) if areas else None)
    corners = [error for record in records for error in record["corner_errors_px"] if error is not None]
    return medians, corners


def run_case(name: str) -> dict[str, Any]:
    """One scenario, start to finish, as a JSON-able result."""
    scenario = BY_NAME[name]
    started = time.perf_counter()
    wall, projector, camera = standard_rig(ambient=scenario.ambient, seed=scenario.seed)
    size = (projector.width, projector.height)
    # Exposure: a fixed camera is set by the camera search before play. The
    # old panel searched with the projector black; contract 2.2 searches under
    # the neutral projection (the theme background), like the colors.
    meter_frame = render_scene({}, "dark" if scenario.rules == "old" else scenario.theme, size, "neutral")
    if scenario.auto_exposure:
        camera.gain = camera.metered_gain(wall, projector.light(meter_frame, camera.size))
        gain = None
    else:
        gain = camera.metered_gain(wall, projector.light(meter_frame, camera.size))
        camera.fixed_gain = gain
    truth = normalized_inverse(projector.to_camera)
    matrix = None
    alignment_error = None
    if scenario.aligned == "true":
        matrix = truth
    elif scenario.aligned == "error_2px":
        matrix = _translation(*ALIGNMENT_ERROR) @ truth
        matrix = matrix / matrix[2, 2]
    if matrix is not None:
        corners = np.vstack([paper.polygon() for paper in wall.papers])
        alignment_error = float(np.linalg.norm(apply_homography(matrix, corners)
                                               - apply_homography(truth, corners), axis=1).max())
    removed_index = next(i for i, paper in enumerate(wall.papers) if paper.role == REMOVED_ROLE)
    without = wall.without(removed_index)
    wall_at = (lambda index: without if index >= REMOVE_AT else wall) if scenario.remove else wall
    clock = SimClock()
    controller = GameController(log_path=None, clock=clock)
    controller.action({"action": "projection_open", "width": size[0], "height": size[1]})
    records = run_closed_loop(controller, wall_at, projector, camera, frames=FRAMES,
                              rules=scenario.rules, theme=scenario.theme, alignment_matrix=matrix,
                              fill=scenario.fill, clock=clock)
    reference, reference_corners = _reference(scenario, gain, matrix)
    result = summarize(scenario, records, wall, reference, reference_corners, alignment_error)
    print(f"{name}: {time.perf_counter() - started:.1f} s", file=sys.stderr)
    return result


def summarize(scenario: Scenario, records: list[dict[str, Any]], wall, reference: list[float | None],
              reference_corners: list[float], alignment_error: float | None) -> dict[str, Any]:
    """Metrics over the measured window and the pass verdict for one case."""
    window = records[WARMUP_FRAMES:]
    ready = [record["ready"] for record in records]
    truth_ready = [record["truth_ready"] for record in records]
    ready_changes = _transitions(ready, WARMUP_FRAMES)
    truth_changes = _transitions(truth_ready, WARMUP_FRAMES)
    # A board stuck invalid never flips; count frames that disagree with the
    # wall too, except a removed goal's allowed ghost frames.
    grace = range(REMOVE_AT, REMOVE_AT + LIMITS["ghost_lifetime_frames"]) if scenario.remove else range(0)
    wrong_ready = sum(1 for record in window
                      if record["ready"] != record["truth_ready"] and record["frame"] not in grace)
    removed_index = next(i for i, paper in enumerate(wall.papers) if paper.role == REMOVED_ROLE)
    ratios, neutral_ratios = [], []
    for record in window:
        shifted = scenario.remove and record["frame"] >= REMOVE_AT
        for index, (ratio, area) in enumerate(zip(record["area_ratios"], record["areas"])):
            if ratio is None:
                continue
            ratios.append(ratio)
            # Per-paper lists follow the wall of that frame: after the removal
            # every paper after the goal moved up one place.
            original = index + 1 if shifted and index >= removed_index else index
            if reference[original] is not None:
                neutral_ratios.append(area / reference[original])
    growth = max(ratios) - 1 if ratios else None
    growth_vs_neutral = max(neutral_ratios) - 1 if neutral_ratios else None
    ghost_lifetime = None
    if scenario.remove:
        ghost_frames = [record["frame"] for record in records[REMOVE_AT:] if record["ghosts"]]
        ghost_lifetime = (ghost_frames[-1] - REMOVE_AT + 1) if ghost_frames else 0
    corners = [error for record in window for error in record["corner_errors_px"] if error is not None]
    clearances = [value for record in window for value in record["rim_clearances_px"] if value is not None]
    corner_max, corner_p95 = _stats(corners)
    reference_max, reference_p95 = _stats(reference_corners)
    result: dict[str, Any] = {
        **{key: value for key, value in asdict(scenario).items() if key not in ("name", "seed")},
        "exposure": "auto" if scenario.auto_exposure else (
            "fixed, metered with the projector black" if scenario.rules == "old"
            else "fixed, metered under the neutral projection"),
        "frames": {"warmup": WARMUP_FRAMES, "measured": len(window),
                   "removed_at": REMOVE_AT if scenario.remove else None},
        "flips": max(0, ready_changes - truth_changes),
        "ready_changes": ready_changes,
        "ready_frames": sum(ready[WARMUP_FRAMES:]),
        "wrong_ready_frames": wrong_ready,
        "phantoms": sum(record["phantoms"] for record in window),
        "phantom_frames": sum(1 for record in window if record["phantoms"]),
        "misclassified": sum(record["misclassified"] for record in window),
        "missed": sum(record["missed"] for record in window),
        "max_area_growth": _round(growth),
        "max_area_growth_vs_neutral": _round(growth_vs_neutral),
        "min_area_ratio": _round(min(ratios)) if ratios else None,
        "min_area_ratio_vs_neutral": _round(min(neutral_ratios)) if neutral_ratios else None,
        "ghost_lifetime_frames": ghost_lifetime,
        "alignment_error_px": _round(alignment_error, 3),
        "max_rim_corner_error_px": corner_max,
        "p95_rim_corner_error_px": corner_p95,
        "min_rim_clearance_px": _round(min(clearances), 3) if clearances else None,
        "neutral_reference": {"frames": REFERENCE_FRAMES, "max_rim_corner_error_px": reference_max,
                              "p95_rim_corner_error_px": reference_p95},
        "gain": [records[0]["gain"], records[-1]["gain"]],
        "max_clipped": max(record["clipped"] for record in window),
    }
    failures = []
    if result["flips"] > LIMITS["flips"]:
        failures.append(f"flips {result['flips']} > {LIMITS['flips']}")
    if wrong_ready > LIMITS["wrong_ready_frames"]:
        failures.append(f"wrong_ready_frames {wrong_ready} > {LIMITS['wrong_ready_frames']}")
    if result["phantoms"] > LIMITS["phantoms"]:
        failures.append(f"phantoms {result['phantoms']} > {LIMITS['phantoms']}")
    for key, value in (("max_area_growth", growth), ("max_area_growth_vs_neutral", growth_vs_neutral)):
        if value is None or value > LIMITS["max_area_growth"]:
            failures.append(f"{key} {result[key]} > {LIMITS['max_area_growth']}")
    if scenario.remove and ghost_lifetime > LIMITS["ghost_lifetime_frames"]:
        failures.append(f"ghost_lifetime_frames {ghost_lifetime} > {LIMITS['ghost_lifetime_frames']}")
    if scenario.aligned and (corner_max is None or corner_max > LIMITS["max_rim_corner_error_px"]):
        failures.append(f"max_rim_corner_error_px {corner_max} > {LIMITS['max_rim_corner_error_px']} "
                        f"(no projection: {reference_max})")
    result["pass"] = not failures
    result["failures"] = failures
    return result


def evaluate(names: list[str], jobs: int = 1) -> dict[str, Any]:
    if jobs > 1 and len(names) > 1:
        with ProcessPoolExecutor(max_workers=min(jobs, len(names)), initializer=_worker_init) as pool:
            results = dict(zip(names, pool.map(run_case, names)))
    else:
        results = {name: run_case(name) for name in names}
    baseline = {name: result for name, result in results.items() if BY_NAME[name].rules == "old"}
    cases = {name: result for name, result in results.items() if BY_NAME[name].rules != "old"}
    passed = sum(result["pass"] for result in cases.values())
    return {
        "score": round(passed / len(cases), 4) if cases else None,
        "pass_threshold": PASS_THRESHOLD,
        "limits": LIMITS,
        "setup": {"camera": [1280, 720], "projector": [1920, 1080], "warmup_frames": WARMUP_FRAMES,
                  "measured_frames": MEASURED_FRAMES, "preview_interval_s": 0.25,
                  "jpeg_quality": 85},
        "baseline": baseline,
        "cases": cases,
    }


def _worker_init() -> None:
    import cv2
    cv2.setNumThreads(2)  # several cases at once: avoid oversubscribing the cores


def _summary_line(name: str, result: dict[str, Any]) -> str:
    verdict = "base" if BY_NAME[name].rules == "old" else ("PASS" if result["pass"] else "FAIL")
    corner = result["max_rim_corner_error_px"]
    ghost = result["ghost_lifetime_frames"]
    return (f"{verdict}  {name:44s} flips={result['flips']:<3d} phantoms={result['phantoms']:<4d} "
            f"growth={result['max_area_growth']!s:<8} ghost={'-' if ghost is None else ghost!s:<3} "
            f"corner={'-' if corner is None else corner}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--only", action="append", choices=sorted(BY_NAME), help="run only this case")
    parser.add_argument("--out", help="also write the JSON here")
    args = parser.parse_args(argv)
    names = args.only or [scenario.name for scenario in SCENARIOS]
    report = evaluate(names, jobs=max(1, args.jobs))
    text = json.dumps(report, ensure_ascii=False, indent=1)
    print(text)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as stream:
            stream.write(text + "\n")
    for name in names:
        result = report["baseline"].get(name) or report["cases"][name]
        print(_summary_line(name, result), file=sys.stderr)
    score = report["score"]
    print(f"score {score} (pass threshold {PASS_THRESHOLD})", file=sys.stderr)
    return 0 if score is None or (math.isfinite(score) and score >= PASS_THRESHOLD) else 1


if __name__ == "__main__":
    raise SystemExit(main())
