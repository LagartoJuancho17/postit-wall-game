"""Automatic camera → projector alignment from a binary-coded dot grid.

Contract: docs/alignment-and-color.md sections 1.1-1.3. The step numbers in
the comments below are the contract's. The projector shows 14 frames: for
each of the 7 bits of every dot's code, the plain pattern and its inverse.
Subtracting the pair cancels the wall, ambient light and post-its; the sign
of the difference is the bit and its consistency over the dot is the
confidence. Ids 1..N plus an even parity bit identify every dot, so one run
yields up to 63 camera ↔ projector correspondences.

services/browser/alignment.mjs mirrors this module line for line on
opencv.js 4.12 (same OpenCV release as cv2 4.12). To let JavaScript
reproduce the numbers bit for bit, every reduction is spelled out:

- medians and percentiles run on an explicitly sorted float64 array with
  numpy's own formulas (`_sorted_median`, `_sorted_percentile`);
- sums over a component's pixels are sequential in raster order:
  `np.bincount(labels, weights)` adds in input order, exactly like a JS
  `for` loop. `ndarray.sum` would use pairwise summation and differ in the
  last bits;
- elementwise arithmetic (`P / Q`, `P - g * Q`, `w * x`) is one IEEE float64
  operation at a time; neither numpy nor V8 fuses multiply-add;
- correspondences are ordered by dot id, so RANSAC sees the same input
  whatever order connected-component labels come out in.
"""

from collections import Counter
from collections.abc import Mapping
import math
import numbers

import cv2
import numpy as np

from contracts.model import Point
from services.calibration import estimate_homography, reprojection_errors

ALIGNMENT_VERSION = 1
ALIGNMENT_BITS = 7
ALIGNMENT_MARGIN = 0.06
# 14 frames in contract order: bit 0 plain, bit 0 inverse, bit 1 plain, ...
ALIGNMENT_FRAMES = tuple(
    (bit, inverse) for bit in range(ALIGNMENT_BITS) for inverse in (False, True)
)

# Decode thresholds, named after the contract steps that use them.
GAIN_STEP = 4            # step 1: gain subsample grid y % 4 == 0, x % 4 == 0
GAIN_MIN_LEVEL = 8       # step 1: ignore near-black pixels in the gain ratio
THRESHOLD_STEP = 2       # step 4: threshold subsample grid
MIN_THRESHOLD = 12.0     # step 4
MIN_AREA = 4             # step 6
MIN_CONFIDENCE = 0.35    # step 6
AREA_FACTOR = 6          # step 8
MIN_DECODED = 16         # step 10
INLIER_PX = 3.0          # step 10
OUTLIER_PX = 5.0         # step 11
MIN_HOLDOUT = 4          # step 11
MAX_P95_PX = 1.5         # step 11
MAX_ERROR_PX = 3.0       # step 11
MIN_COVERAGE = 0.30      # step 12

# Operator texts, exactly as in the contract table. `code` is the stable key.
ALIGNMENT_REASONS = {
    "few_points": (
        "La cámara vio solo {decoded} de {total} puntos. Revisá que la cámara "
        "vea la proyección completa y que el proyector esté encendido."
    ),
    "inconsistent": (
        "Los puntos detectados no forman una proyección coherente. Evitá "
        "reflejos y volvé a alinear."
    ),
    "inaccurate": (
        "La alineación quedó imprecisa ({p95:.1f} px). Fijá la cámara y el "
        "proyector y volvé a alinear."
    ),
    "low_coverage": (
        "La cámara ve muy poca parte de la proyección. Encuadrala para que vea "
        "toda la pared."
    ),
    "frames": "Faltan cuadros del patrón de alineación.",
}

REJECTION_KEYS = ("ambiguous", "parity", "unknown_id", "duplicate", "area")


def _is_number(value) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _clamp(value: int, low: int, high: int) -> int:
    return min(high, max(low, value))


def alignment_layout(width: float, height: float) -> dict:
    """Dot grid for a projector viewport of `width` x `height` projector px.

    Pure function of the size; the panel and projector read the result from
    state instead of recomputing it. `round` is Python's (half to even), which
    is why 787 x 752 gives 8 columns (round(7.92)) and 8 rows (round(7.5)),
    trimmed to 7 by the 63-dot cap.
    """

    if not _is_number(width) or not _is_number(height):
        raise ValueError("width and height must be numbers")
    if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
        raise ValueError("width and height must be positive and finite")
    aspect = width / height
    cols = _clamp(round(math.sqrt(60 * aspect)), 4, 12)
    rows = _clamp(round(60 / cols), 3, 9)
    while cols * rows > 63:
        rows -= 1
    margin = ALIGNMENT_MARGIN
    dots = []
    for j in range(rows):
        for i in range(cols):
            # Same operation order as the contract formula, for the JS twin.
            dots.append({
                "id": j * cols + i + 1,
                "x": width * (margin + (1 - 2 * margin) * i / (cols - 1)),
                "y": height * (margin + (1 - 2 * margin) * j / (rows - 1)),
            })
    return {
        "version": ALIGNMENT_VERSION,
        "width": width,
        "height": height,
        "cols": cols,
        "rows": rows,
        "radius": max(4.0, 0.012 * min(width, height)),
        "bits": ALIGNMENT_BITS,
        "dots": dots,
        "frames": [{"bit": bit, "inverse": inverse} for bit, inverse in ALIGNMENT_FRAMES],
    }


def alignment_code(dot_id: int) -> int:
    """7-bit code of a dot: 6 id bits plus even parity in bit 6."""

    if isinstance(dot_id, bool) or not isinstance(dot_id, numbers.Integral):
        raise ValueError("dot id must be an integer")
    dot_id = int(dot_id)
    if not 0 <= dot_id <= 63:
        raise ValueError("dot id must fit in 6 bits")
    return dot_id | ((dot_id.bit_count() % 2) << 6)


def _check_bit(bit: int) -> int:
    if isinstance(bit, bool) or not isinstance(bit, numbers.Integral):
        raise ValueError("bit must be an integer")
    if not 0 <= int(bit) < ALIGNMENT_BITS:
        raise ValueError("bit must be between 0 and 6")
    return int(bit)


def pattern_dots(layout: Mapping, bit: int, inverse: bool) -> list[int]:
    """Ids of the dots drawn white in pattern frame `{bit, inverse}`.

    Contract 1.2: dot `id` is lit iff ((code(id) >> bit) & 1) == (inverse ? 0 : 1),
    so every dot is lit in exactly one frame of each pair.
    """

    bit = _check_bit(bit)
    lit = 0 if inverse else 1
    # Explicit parentheses: in JavaScript `==` binds tighter than `&`.
    return [
        dot["id"] for dot in layout["dots"]
        if ((alignment_code(dot["id"]) >> bit) & 1) == lit
    ]


def _sorted_median(ordered: np.ndarray) -> float:
    """numpy.median of a sorted, non-empty 1-D float64 array.

    numpy takes the middle value, or for an even count the mean of the two
    middle values, computed as (a + b) / 2.
    """

    count = len(ordered)
    half = count // 2
    if count % 2 == 1:
        return float(ordered[half])
    return float((float(ordered[half - 1]) + float(ordered[half])) / 2.0)


def _sorted_percentile(ordered: np.ndarray, q: float) -> float:
    """numpy.percentile(..., q) (default 'linear') of a sorted, non-empty array.

    Mirrors numpy 2.x exactly: virtual index (n - 1) * (q / 100); the last
    value when that index reaches n - 1; otherwise numpy's `_lerp`, which
    interpolates from the upper neighbour when the fraction is >= 0.5.
    """

    count = len(ordered)
    virtual = (count - 1) * (q / 100.0)
    if virtual >= count - 1:
        return float(ordered[-1])
    lower = math.floor(virtual)
    gamma = virtual - lower
    below = float(ordered[lower])
    above = float(ordered[lower + 1])
    difference = above - below
    if gamma >= 0.5:
        return above - difference * (1 - gamma)
    return below + difference * gamma


def _collect_frames(frames: Mapping) -> dict | None:
    """The 14 frames by key, or None when one is missing or sized differently.

    A frame that is not a 2-D uint8 image is a programming error, not a
    capture problem, and raises ValueError.
    """

    images = {}
    shape = None
    for key in ALIGNMENT_FRAMES:
        image = frames.get(key)
        if image is None:
            return None
        if (
            not isinstance(image, np.ndarray)
            or image.dtype != np.uint8
            or image.ndim != 2
            or image.size == 0
        ):
            raise ValueError("alignment frames must be non-empty 2-D uint8 grayscale images")
        if shape is None:
            shape = image.shape
        elif image.shape != shape:
            # A camera resolution change mid-run leaves frames unusable together.
            return None
        images[key] = image
    return images


def _signal(images: Mapping) -> tuple[list[float], list[np.ndarray], np.ndarray]:
    """Steps 1-3: per-bit gains, signed differences D_b and strength S."""

    gains: list[float] = []
    differences: list[np.ndarray] = []
    strength = None
    for bit in range(ALIGNMENT_BITS):
        plain = images[(bit, False)].astype(np.float64)
        inverse = images[(bit, True)].astype(np.float64)
        # Step 1: exposure ratio between the two captures, from the wall itself.
        p = plain[::GAIN_STEP, ::GAIN_STEP]
        q = inverse[::GAIN_STEP, ::GAIN_STEP]
        usable = (p >= GAIN_MIN_LEVEL) & (q >= GAIN_MIN_LEVEL)
        ratios = p[usable] / q[usable]
        gain = _sorted_median(np.sort(ratios)) if ratios.size else 1.0
        # Step 2.
        difference = plain - gain * inverse
        gains.append(gain)
        differences.append(difference)
        # Step 3: summed left to right (0 + |D_0| + |D_1| ... in JS).
        magnitude = np.abs(difference)
        strength = magnitude if strength is None else strength + magnitude
    return gains, differences, strength / ALIGNMENT_BITS


def _threshold(strength: np.ndarray) -> float:
    """Step 4: noise floor or 40% of the brightest dots, whichever is higher."""

    sample = np.sort(strength[::THRESHOLD_STEP, ::THRESHOLD_STEP], axis=None)
    return max(
        MIN_THRESHOLD,
        4.0 * _sorted_median(sample) + 6.0,
        0.4 * _sorted_percentile(sample, 99.9),
    )


def _components(
    strength: np.ndarray, differences: list[np.ndarray], threshold: float
) -> list[dict]:
    """Steps 5-6: bright blobs away from the border, with code and confidence.

    Returned in label order; each has camera centroid `x`, `y`, `area`, the
    bounding box, the 7-bit `code` and the smallest per-bit `confidence`.
    """

    height, width = strength.shape
    mask = (strength >= threshold).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        mask, connectivity=8, ltype=cv2.CV_32S
    )
    flat_labels = labels.ravel()
    index = np.flatnonzero(flat_labels)  # ascending = raster order
    label = flat_labels[index]
    weight = strength.ravel()[index]
    xs = (index % width).astype(np.float64)   # pixel centers at integer coordinates
    ys = (index // width).astype(np.float64)

    def per_label(values: np.ndarray) -> np.ndarray:
        # Sequential raster-order sum per component (see module docstring).
        return np.bincount(label, weights=values, minlength=count)

    weight_sum = per_label(weight)
    x_sum = per_label(weight * xs)
    y_sum = per_label(weight * ys)
    signed_sums = []
    absolute_sums = []
    for difference in differences:
        values = difference.ravel()[index]
        signed_sums.append(per_label(weight * values))
        absolute_sums.append(per_label(weight * np.abs(values)))

    components = []
    for component in range(1, count):
        left, top, box_width, box_height, area = (int(v) for v in stats[component, :5])
        # Step 6: a dot cut by the frame edge has a biased centroid.
        if area < MIN_AREA or left <= 0 or top <= 0:
            continue
        if left + box_width >= width or top + box_height >= height:
            continue
        total = float(weight_sum[component])
        code = 0
        confidences = []
        for bit in range(ALIGNMENT_BITS):
            value = float(signed_sums[bit][component]) / total
            spread = float(absolute_sums[bit][component]) / total
            if value > 0:
                code |= 1 << bit
            # spread == 0 (both frames identical, e.g. saturated) carries no
            # information; JS would get NaN from 0 / 0, so define it as 0.
            confidences.append(abs(value) / spread if spread > 0 else 0.0)
        components.append({
            "x": float(x_sum[component]) / total,
            "y": float(y_sum[component]) / total,
            "area": area,
            "left": left,
            "top": top,
            "width": box_width,
            "height": box_height,
            "code": code,
            "confidence": min(confidences),
        })
    return components


def _identify(components: list[dict], total: int) -> tuple[list[dict], dict]:
    """Steps 6 (ambiguous), 7 and 8: name components by dot id, drop doubtful ones.

    Survivors come back sorted by id, each a component dict plus `id`.
    """

    rejected = dict.fromkeys(REJECTION_KEYS, 0)
    named = []
    for component in components:
        if component["confidence"] < MIN_CONFIDENCE:
            rejected["ambiguous"] += 1
            continue
        code = component["code"]
        dot_id = code & 63
        if ((code >> 6) & 1) != dot_id.bit_count() % 2:
            rejected["parity"] += 1
            continue
        if dot_id < 1 or dot_id > total:
            rejected["unknown_id"] += 1
            continue
        named.append({**component, "id": dot_id})

    # Step 7: two blobs claiming one id are both suspect.
    claims = Counter(component["id"] for component in named)
    unique = [component for component in named if claims[component["id"]] == 1]
    rejected["duplicate"] = len(named) - len(unique)

    # Step 8: drop blobs far larger or smaller than a typical dot.
    kept = unique
    if unique:
        areas = np.sort(np.array([component["area"] for component in unique], dtype=np.float64))
        median_area = _sorted_median(areas)
        kept = [
            component for component in unique
            if not (component["area"] > AREA_FACTOR * median_area
                    or component["area"] * AREA_FACTOR < median_area)
        ]
        rejected["area"] = len(unique) - len(kept)
    kept.sort(key=lambda component: component["id"])
    return kept, rejected


def _errors(camera: list[Point], projector: list[Point], matrix) -> list[float]:
    """Per-pair reprojection error; a point H sends to infinity counts as inf."""

    errors = []
    for source, target in zip(camera, projector):
        try:
            errors.append(reprojection_errors([source], [target], matrix)[0])
        except ValueError:
            errors.append(math.inf)
    return errors


def _empty_result(total: int) -> dict:
    # None means "the step that computes it did not run".
    return {
        "ok": False,
        "reason": None,
        "code": None,
        "matrix": None,
        "decoded": None,
        "total": total,
        "fit": None,
        "inliers": None,
        "holdout": None,
        "holdout_p95_px": None,
        "holdout_max_px": None,
        "outliers": None,
        "coverage": None,
        "threshold": None,
        "gains": None,
        "rejected": None,
    }


def _fail(result: dict, code: str, **values) -> dict:
    result["ok"] = False
    result["matrix"] = None
    result["code"] = code
    result["reason"] = ALIGNMENT_REASONS[code].format(**values)
    return result


def _solve(found: list[dict], layout: Mapping, result: dict) -> dict:
    """Steps 9-12 on identified dots (sorted by id); fills `result`."""

    targets = {int(dot["id"]): (float(dot["x"]), float(dot["y"])) for dot in layout["dots"]}
    # Step 9.
    fit = [dot for dot in found if dot["id"] % 5 != 0]
    holdout = [dot for dot in found if dot["id"] % 5 == 0]
    result["decoded"] = len(found)
    result["fit"] = len(fit)
    result["holdout"] = len(holdout)

    def camera(dots):
        return [Point(dot["x"], dot["y"]) for dot in dots]

    def projector(dots):
        return [Point(*targets[dot["id"]]) for dot in dots]

    # Step 10.
    if len(found) < MIN_DECODED:
        return _fail(result, "few_points", decoded=len(found), total=result["total"])
    try:
        matrix = estimate_homography(camera(fit), projector(fit))
    except ValueError:
        # No homography at all: no fit point can agree with one.
        result["inliers"] = 0
        return _fail(result, "inconsistent")
    fit_errors = _errors(camera(fit), projector(fit), matrix)
    inliers = sum(1 for error in fit_errors if error <= INLIER_PX)
    result["inliers"] = inliers
    # 0.8 * fit is float math on purpose (JS: Math.ceil(0.8 * fit)); it is
    # exact for every fit size up to 63.
    if inliers < max(10, math.ceil(0.8 * len(fit))):
        return _fail(result, "inconsistent")

    # Step 11.
    holdout_errors = _errors(camera(holdout), projector(holdout), matrix)
    good = np.sort(np.array([e for e in holdout_errors if e <= OUTLIER_PX], dtype=np.float64))
    outliers = len(holdout_errors) - len(good)
    p95 = _sorted_percentile(good, 95) if len(good) else None
    worst = float(good[-1]) if len(good) else None
    result["outliers"] = outliers
    result["holdout_p95_px"] = p95
    result["holdout_max_px"] = worst
    if (
        len(holdout) < MIN_HOLDOUT
        or outliers > max(1, math.floor(0.1 * len(holdout)))
        or p95 is None
        or p95 > MAX_P95_PX
        or worst > MAX_ERROR_PX
    ):
        # The message needs a number even when no holdout error is usable
        # (not specified by the contract): then the p95 of the finite holdout
        # errors, all of them outliers, or 0.0 when there is none at all.
        shown = p95
        if shown is None:
            finite = np.sort(np.array(
                [e for e in holdout_errors if math.isfinite(e)], dtype=np.float64))
            shown = _sorted_percentile(finite, 95) if len(finite) else 0.0
        return _fail(result, "inaccurate", p95=shown)

    # Step 12: share of the projector frame the decoded dots span.
    points = np.array([targets[dot["id"]] for dot in found], dtype=np.float32)
    hull = cv2.convexHull(points)
    coverage = float(cv2.contourArea(hull)) / (layout["width"] * layout["height"])
    result["coverage"] = coverage
    if coverage < MIN_COVERAGE:
        return _fail(result, "low_coverage")

    result["ok"] = True
    result["matrix"] = [list(row) for row in matrix]
    return result


def decode_alignment(frames: Mapping, layout: Mapping) -> dict:
    """Camera → projector homography from the 14 captured pattern frames.

    `frames` maps `(bit, inverse)` to the grayscale uint8 camera frame
    (`cvtColor(BGR2GRAY)` of the decoded JPEG). Returns the contract 1.3
    dict plus `code` (None on success, else one of ALIGNMENT_REASONS).
    Floats are not rounded; the server rounds for the snapshot.
    """

    total = len(layout["dots"])
    result = _empty_result(total)
    images = _collect_frames(frames)
    if images is None:
        return _fail(result, "frames")
    gains, differences, strength = _signal(images)
    result["gains"] = gains
    threshold = _threshold(strength)
    result["threshold"] = threshold
    components = _components(strength, differences, threshold)
    found, rejected = _identify(components, total)
    result["rejected"] = rejected
    return _solve(found, layout, result)


__all__ = [
    "ALIGNMENT_FRAMES",
    "ALIGNMENT_REASONS",
    "alignment_code",
    "alignment_layout",
    "decode_alignment",
    "pattern_dots",
]
