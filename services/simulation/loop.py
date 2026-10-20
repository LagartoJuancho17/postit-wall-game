"""Closed loop: controller snapshot -> projector frame -> camera -> controller.

Drives the real ``GameController`` the way the panel and the projector
window do: the projector draws the latest snapshot, the camera photographs
the wall under that light, and the JPEG goes through ``preview_jpeg``. One
frame per panel preview (``PREVIEW_EVERY_MS`` = 250 ms in ``app.js``), so the
projector is always one preview behind the camera, like the real loop.

The backend has no alignment endpoints yet, so for the aligned rules the
loop hands the projector ``alignment_matrix`` itself (the true
``inverse(G)``, or a perturbed one to model alignment error). Once the
backend publishes ``state["alignment"]``, pass ``alignment_matrix=None`` and
``render_scene`` reads it from the snapshot instead.

Every frame record is compared with the ground truth on the wall, which the
controller never sees: phantom notes, ghosts of removed papers, area per
paper and, when aligned, where the projected rim lands in projector px.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Sequence

import cv2
import numpy as np

from .model import Camera, Paper, Projector, Wall, apply_homography, normalized_inverse
from .projector import (RIM_WIDTH_PX, ROLES, offset_polygon, render_pattern, render_scene,
                        rim_polygon, signed_area)

PREVIEW_INTERVAL_S = 0.25
CAMERA_LABEL = "Cámara simulada"
TOUCH_AREA_PX2 = 1.0  # less overlap than this is "not touching" (anti-aliasing slack)


class SimClock:
    """Injectable ``GameController`` clock that only moves when told."""

    def __init__(self, start: float = 0.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _convex(points: np.ndarray) -> np.ndarray:
    return cv2.convexHull(np.asarray(points, np.float32).reshape(-1, 1, 2)).reshape(-1, 2)


def overlap_area(first: np.ndarray, second: np.ndarray) -> float:
    """Intersection area of two convex polygons, any winding."""
    area, _ = cv2.intersectConvexConvex(_convex(first), _convex(second))
    return max(0.0, float(area))


def polygon_area(points: np.ndarray) -> float:
    return float(abs(cv2.contourArea(np.asarray(points, np.float32))))


def vertex_distance(first: np.ndarray, second: np.ndarray) -> float:
    """Symmetric vertex Hausdorff distance: every corner to its nearest corner."""
    distances = np.linalg.norm(first[:, None, :] - second[None, :, :], axis=2)
    return float(max(distances.min(axis=1).max(), distances.min(axis=0).max()))


def clearance(points: np.ndarray, polygon: np.ndarray) -> float:
    """Smallest signed distance from ``points`` to a convex polygon's edges,
    positive inside: >= 0 means every point lies inside the polygon."""
    sign = np.sign(signed_area(polygon))
    edges = np.roll(polygon, -1, axis=0) - polygon
    lengths = np.linalg.norm(edges, axis=1)
    relative = points[:, None, :] - polygon[None, :, :]
    distances = sign * (edges[None, :, 0] * relative[..., 1] - edges[None, :, 1] * relative[..., 0]) / lengths
    return float(distances.min())


def rim_error(alignment_matrix: np.ndarray, truth_to_projector: np.ndarray,
              detected: np.ndarray, truth: np.ndarray) -> tuple[float, float] | None:
    """(corner error, clearance) of the rim drawn for one detection, projector px.

    Corner error: the drawn rim polygon vs the rim the true outline would get
    (both ``rim_polygon``). Clearance: how far the true paper stays inside
    the rim stroke's inner edge; negative means the rim lights the paper.
    """
    drawn = rim_polygon(apply_homography(alignment_matrix, detected))
    paper = apply_homography(truth_to_projector, truth)
    wanted = rim_polygon(paper)
    if drawn is None or wanted is None:
        return None
    inner = offset_polygon(drawn, -RIM_WIDTH_PX / 2)
    return vertex_distance(drawn, wanted), (clearance(paper, inner) if inner is not None else -np.inf)


def detections(preview: dict[str, Any]) -> list[tuple[str, np.ndarray]]:
    """Every note of a preview as (role, (N, 2) camera px), in role order."""
    found = []
    for role in ROLES:
        for note in preview["roles"][role]["notes"]:
            found.append((role, np.array([(p["x"], p["y"]) for p in note["points"]], np.float64)))
    return found


def frame_record(index: int, preview: dict[str, Any], wall: Wall,
                 removed: Sequence[Paper] = (), *, alignment_matrix: np.ndarray | None = None,
                 truth_to_projector: np.ndarray | None = None) -> dict[str, Any]:
    """Compare one preview with the wall that produced it.

    A detection that touches a paper on the wall belongs to it; one that
    touches only a removed paper's old place is a ghost; anything else is a
    phantom. Per-paper lists (``areas``, ``area_ratios``, ``ious``,
    ``corner_errors_px``, ``rim_clearances_px``) follow ``wall.papers`` and
    hold None for a paper with no same-role detection. Rim metrics compare
    the rim the projector draws for the detection (through
    ``alignment_matrix``) with the true outline (through
    ``truth_to_projector``), see ``rim_error``.
    """
    found = detections(preview)
    papers = [(paper, paper.polygon(), paper.area()) for paper in wall.papers]
    gone = [paper.polygon() for paper in removed]
    phantoms = ghosts = misclassified = 0
    for role, shape in found:
        touching = [paper for paper, polygon, _ in papers if overlap_area(shape, polygon) > TOUCH_AREA_PX2]
        if not touching:
            if any(overlap_area(shape, polygon) > TOUCH_AREA_PX2 for polygon in gone):
                ghosts += 1
            else:
                phantoms += 1
        elif all(paper.role != role for paper in touching):
            misclassified += 1
    areas: list[float | None] = []
    ratios: list[float | None] = []
    ious: list[float | None] = []
    corners: list[float | None] = []
    clearances: list[float | None] = []
    for paper, polygon, area in papers:
        best, best_iou = None, 0.0
        for role, shape in found:
            if role != paper.role:
                continue
            inter = overlap_area(shape, polygon)
            iou = inter / (polygon_area(shape) + area - inter) if inter > 0 else 0.0
            if iou > best_iou:
                best, best_iou = shape, iou
        if best is None:
            for values in (areas, ratios, ious, corners, clearances):
                values.append(None)
            continue
        areas.append(polygon_area(best))
        ratios.append(areas[-1] / area)
        ious.append(best_iou)
        rim = None
        if alignment_matrix is not None and truth_to_projector is not None:
            rim = rim_error(alignment_matrix, truth_to_projector, best, polygon)
        corners.append(rim[0] if rim else None)
        clearances.append(rim[1] if rim else None)
    truth_counts = Counter(paper.role for paper in wall.papers)
    present_corners = [value for value in corners if value is not None]
    present_ious = [value for value in ious if value is not None]
    return {
        "frame": index,
        "counts": {role: preview["roles"][role]["count"] for role in ROLES},
        "truth_counts": {role: truth_counts.get(role, 0) for role in ROLES},
        "ready": bool(preview["quality"]["ready"]),
        "truth_ready": truth_counts.get("start", 0) == 1 and truth_counts.get("goal", 0) == 1,
        "phantoms": phantoms,
        "ghosts": ghosts,
        "misclassified": misclassified,
        "missed": sum(area is None for area in areas),
        "areas": areas,
        "area_ratios": ratios,
        "ious": ious,
        "corner_errors_px": corners,
        "rim_clearances_px": clearances,
        "min_iou": min(present_ious) if present_ious else None,
        "max_corner_error_px": max(present_corners) if present_corners else None,
        "detections": [(role, shape.round(2).tolist()) for role, shape in found],
    }


def run_closed_loop(controller: Any, wall: Wall | Callable[[int], Wall], projector: Projector,
                    camera: Camera, frames: int = 70, rules: str = "new", theme: str = "dark",
                    alignment_matrix: np.ndarray | None = None, *, fill: bool | None = None,
                    clock: SimClock | None = None, label: str = CAMERA_LABEL) -> list[dict[str, Any]]:
    """Run ``frames`` closed-loop frames and return one record per frame.

    ``wall`` may be a callable ``frame -> Wall`` to change the wall mid-run
    (a paper taken down at frame 20). Papers present at frame 0 and missing
    later are "removed": detections on their old place count as ghosts, not
    phantoms. ``rules`` is ``"old"``, ``"new"`` or ``"neutral"`` (projector
    shows only the theme background: an open-loop reference). ``fill`` is the
    contract's ``alignment.overlay.fill`` (None: read it from the snapshot)
    and only ever applies when aligned. ``clock`` advances one preview
    interval per frame, so the controller's 1.5 s detection expiry behaves
    as in the real loop.
    """
    wall_at = wall if callable(wall) else (lambda _index: wall)
    initial = wall_at(0).papers
    truth_to_projector = normalized_inverse(projector.to_camera)
    size = (projector.width, projector.height)
    records = []
    for index in range(frames):
        current = wall_at(index)
        removed = [paper for paper in initial if paper not in current.papers]
        image = render_scene(controller.snapshot(), theme, size, rules,
                             matrix=alignment_matrix, fill=fill)
        shot = camera.capture(current, image, projector)
        preview = controller.preview_jpeg(shot.jpeg, label)
        record = frame_record(index, preview, current, removed, alignment_matrix=alignment_matrix,
                              truth_to_projector=truth_to_projector)
        record.update(gain=round(shot.gain, 4), mean_luma=round(shot.mean_luma, 4),
                      clipped=round(shot.clipped, 4))
        records.append(record)
        if clock is not None:
            clock.advance(PREVIEW_INTERVAL_S)
    return records


def capture_alignment(layout: dict[str, Any], wall: Wall, projector: Projector,
                      camera: Camera) -> dict[tuple[int, bool], bytes]:
    """JPEGs of the contract 1.2 pattern sequence, keyed ``(bit, inverse)``.

    Shows the warm-up frame first (auto exposure settles on it, like the
    panel's ``warmupMs``), then every frame of ``layout["frames"]`` in
    order. What the panel would ``POST /api/align_frame``; once the backend
    has that endpoint the closed loop sends these.
    """
    size = (projector.width, projector.height)
    camera.capture(wall, render_pattern(layout, "warmup", size), projector)
    return {(frame["bit"], frame["inverse"]): camera.capture(
                wall, render_pattern(layout, frame, size), projector).jpeg
            for frame in layout["frames"]}
