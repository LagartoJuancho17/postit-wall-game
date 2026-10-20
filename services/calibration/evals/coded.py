"""Realistic eval of the coded dot alignment against ground truth.

Run from the project root:

    python -m services.calibration.evals.coded             # one seed per case
    python -m services.calibration.evals.coded --seeds 10  # robustness sweep

Each case films the 14 contract patterns of a real projector size with a
1280x720 camera model (perspective, gamma 2.2, blur, sensor noise, JPEG,
per-frame exposure drift, post-its, glare) and decodes them with
`decode_alignment`. A run passes when:

- its outcome (`ok` or the failure code) is one the case accepts;
- on `ok`, the decoded homography matches the true camera → projector
  mapping inv(G) on a camera grid inside the projected area: p95 <= 1.5 px
  and max <= 3 px (contract outcome table);
- no identified dot carries a wrong id (`wrong_ids`: a step 8 survivor
  whose nearest true dot, G applied to the layout, has another id or lies
  farther than half the closest dot spacing). A right id with a biased
  centroid (a dot half under saturated glare) is not a wrong id: RANSAC
  and the holdout check deal with those;
- it shows at least the rejection counts the case names.

Prints JSON and exits non-zero when the score (passed runs / runs) is below
the threshold.
"""

import argparse
import json
import sys
import time

import cv2
import numpy as np

from services.calibration import coded
from services.calibration.coded import alignment_layout, decode_alignment
from services.calibration.tests.render import (
    POSTIT_COLORS,
    capture_frames,
    homography_from_quad,
    make_wall,
    truth_errors,
)

PASS_THRESHOLD = 1.0
TRUTH_P95_PX = 1.5
TRUTH_MAX_PX = 3.0
CAMERA = (1280, 720)
FULL_HD = (1920, 1080)
KEYSTONE = [[140, 90], [1150, 60], [1210, 660], [90, 690]]
BASE_SEED = 20261019

# What an iPhone in auto exposure delivers, roughly: gamma-encoded, slightly
# soft, noisy, JPEG 85 from the panel. Cases override what they stress.
CAMERA_OPTIONS = {"gamma": 2.2, "blur": 1.2, "noise": 2.0, "jpeg": 85,
                  "ambient": 0.2, "black_level": 0.03, "gain": 0.75}


def _postits():
    # Five notes in camera px, several of them under projected dots.
    rects = [(250, 230, 90), (560, 140, 80), (840, 420, 100), (380, 520, 85), (1010, 230, 75)]
    return [
        ([[x - s // 2, y - s // 2], [x + s // 2, y - s // 2 + 6],
          [x + s // 2 - 4, y + s // 2], [x - s // 2 + 3, y + s // 2 - 5]], color)
        for (x, y, s), color in zip(rects, POSTIT_COLORS)
    ]


CASES = [
    {"name": "fullhd_keystone", "projector": FULL_HD, "quad": KEYSTONE,
     "options": {"drift": 0.05}},
    {"name": "gain_drift_20", "projector": FULL_HD, "quad": KEYSTONE,
     "options": {"drift": 0.20}},
    {"name": "gain_drift_35", "projector": FULL_HD, "quad": KEYSTONE,
     "options": {"drift": 0.35}},
    {"name": "overhang_partial_view", "projector": FULL_HD,
     "quad": [[-200, -120], [1400, -80], [1450, 820], [-150, 790]],
     "options": {"drift": 0.10}},
    {"name": "small_projected_area", "projector": (1080, 767),
     "quad": [[420, 220], [860, 210], [870, 520], [410, 530]],
     "options": {"drift": 0.10}},
    {"name": "windowed_787x752", "projector": (787, 752),
     "quad": [[200, 60], [1000, 80], [980, 690], [220, 660]],
     "options": {"drift": 0.10}},
    {"name": "heavy_blur_jpeg60", "projector": FULL_HD, "quad": KEYSTONE,
     "options": {"drift": 0.10, "blur": 2.5, "jpeg": 60}},
    {"name": "postits_and_glossy_reflection", "projector": FULL_HD, "quad": KEYSTONE,
     "options": {"drift": 0.20, "postits": True,
                 "reflections": [(700, 330, 22.0, 2.5), (980, 560, 8.0, 1.5)]}},
    {"name": "ambient_high_low_contrast", "projector": FULL_HD, "quad": KEYSTONE,
     "options": {"drift": 0.10, "ambient": 1.0, "brightness": 0.35, "gain": 0.55}},
    {"name": "transition_blend_40", "projector": FULL_HD, "quad": KEYSTONE,
     "options": {"drift": 0.10, "blend": {(2, False): 0.4, (5, True): 0.4}}},
    {"name": "camera_rotated_180", "projector": FULL_HD,
     "quad": [[1210, 660], [90, 690], [140, 90], [1150, 60]],
     "options": {"drift": 0.10}},
    {"name": "combined_worst", "projector": FULL_HD,
     "quad": [[60, 40], [1240, 20], [1290, 700], [20, 715]],
     "options": {"drift": 0.25, "blur": 2.0, "jpeg": 70, "postits": True,
                 "reflections": [(700, 330, 22.0, 2.5)],
                 "blend": {(1, True): 0.3, (4, False): 0.3}}},
    # A stale pair (both frames of bit 3 show the plain pattern) leaves that
    # bit without signal: dots are rejected as ambiguous (contract 1.3 step
    # 6) or by parity, usually leaving too few (step 10). With noise, some
    # dots keep a confident bit 3 by chance; the parity bit stops every wrong
    # id, so when enough survive the alignment is still correct. What must
    # never happen is a wrong `ok`, which wrong_ids and the truth check catch.
    {"name": "stale_frame", "projector": FULL_HD, "quad": KEYSTONE,
     "options": {"drift": 0.10, "stale": (3, True)},
     "expect": ("few_points", "inaccurate", "inconsistent", "ok"),
     "expect_rejected": {"ambiguous": 5}},
    {"name": "corner_view", "projector": FULL_HD,
     "quad": [[368, 26], [2192, 40], [2180, 1052], [380, 1040]],
     "options": {"drift": 0.10}, "expect": ("low_coverage",)},
]


def _round(value, digits=3):
    return None if value is None else round(float(value), digits)


def wrong_ids(frames: dict, layout: dict, G: np.ndarray) -> int:
    """Identified dots (contract step 8 survivors) not at their own dot."""

    _, differences, strength = coded._signal(frames)
    components = coded._components(strength, differences, coded._threshold(strength))
    found, _ = coded._identify(components, len(layout["dots"]))
    ids = [dot["id"] for dot in layout["dots"]]
    projector = np.array([[dot["x"], dot["y"]] for dot in layout["dots"]], dtype=np.float64)
    truth = cv2.perspectiveTransform(projector.reshape(-1, 1, 2), G).reshape(-1, 2)
    gaps = np.hypot(*(truth[:, None, :] - truth[None, :, :]).transpose(2, 0, 1))
    reach = 0.5 * gaps[gaps > 0].min()
    wrong = 0
    for dot in found:
        distance = np.hypot(truth[:, 0] - dot["x"], truth[:, 1] - dot["y"])
        nearest = int(np.argmin(distance))
        if ids[nearest] != dot["id"] or distance[nearest] > reach:
            wrong += 1
    return wrong


def run_case(case: dict, seed: int) -> dict:
    projector = case["projector"]
    layout = alignment_layout(*projector)
    G = homography_from_quad(projector, case["quad"])
    options = {**CAMERA_OPTIONS, **case["options"]}
    if options.pop("postits", False):
        options["wall"] = make_wall(CAMERA, postits=_postits())
    frames = capture_frames(layout, G, CAMERA, seed=seed, **options)
    started = time.perf_counter()
    result = decode_alignment(frames, layout)
    decode_ms = (time.perf_counter() - started) * 1000.0
    accepted = tuple(case.get("expect", ("ok",)))
    outcome = "ok" if result["ok"] else result["code"]
    report = {
        "name": case["name"],
        "seed": seed,
        "projector": list(projector),
        "expect": list(accepted),
        "outcome": outcome,
        "decoded": result["decoded"],
        "total": result["total"],
        "inliers": result["inliers"],
        "fit": result["fit"],
        "holdout": result["holdout"],
        "holdout_p95_px": _round(result["holdout_p95_px"]),
        "holdout_max_px": _round(result["holdout_max_px"]),
        "outliers": result["outliers"],
        "coverage": _round(result["coverage"]),
        "threshold": _round(result["threshold"], 1),
        "rejected": result["rejected"],
        "wrong_ids": wrong_ids(frames, layout, G),
        "decode_ms": round(decode_ms, 1),
    }
    accurate = True
    if result["ok"]:
        errors = truth_errors(result["matrix"], G, CAMERA, projector)
        report["truth_points"] = int(len(errors))
        if len(errors):
            p95, worst = float(np.percentile(errors, 95)), float(errors.max())
            report["truth_p95_px"] = _round(p95)
            report["truth_max_px"] = _round(worst)
            accurate = p95 <= TRUTH_P95_PX and worst <= TRUTH_MAX_PX
        else:
            accurate = False
    rejected = result["rejected"] or {}
    report["pass"] = bool(
        outcome in accepted
        and accurate
        and report["wrong_ids"] == 0
        and all(rejected.get(key, 0) >= count
                for key, count in case.get("expect_rejected", {}).items())
    )
    return report


def run(seeds: int = 1) -> dict:
    # Run k of case i uses seed BASE_SEED + i + 1000 * k; k = 0 is the default run.
    reports = [
        run_case(case, seed=BASE_SEED + index + 1000 * k)
        for index, case in enumerate(CASES)
        for k in range(seeds)
    ]
    passed = sum(1 for report in reports if report["pass"])
    times = [report["decode_ms"] for report in reports]
    ok_runs = [report for report in reports if report["outcome"] == "ok"]
    return {
        "eval": "coded_alignment",
        "score": round(passed / len(reports), 4),
        "pass_threshold": PASS_THRESHOLD,
        "passed": passed,
        "total": len(reports),
        "seeds_per_case": seeds,
        "wrong_ids_total": sum(report["wrong_ids"] for report in reports),
        "truth_p95_px_max": max((r["truth_p95_px"] for r in ok_runs if "truth_p95_px" in r),
                                default=None),
        "truth_max_px_max": max((r["truth_max_px"] for r in ok_runs if "truth_max_px" in r),
                                default=None),
        "decode_ms_max": max(times),
        "decode_ms_mean": round(sum(times) / len(times), 1),
        "cases": reports,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, default=1, help="runs per case (default 1)")
    args = parser.parse_args(argv)
    if args.seeds < 1:
        parser.error("--seeds must be at least 1")
    summary = run(args.seeds)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 0 if summary["score"] >= PASS_THRESHOLD else 1


if __name__ == "__main__":
    sys.exit(main())
