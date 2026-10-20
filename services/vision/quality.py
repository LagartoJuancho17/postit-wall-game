"""How well one frame suits detection, to compare fixed camera settings.

The panel tries several exposures, white balances and focus distances and
keeps the one whose frame scores best. These numbers are the evidence: they
come from the same detector and palette the scan uses.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np

from contracts.palette import ROLES, Palette

from .detector import detect_board, find_notes
from .palette import DEFAULT_PALETTE

CLIPPED_LEVEL = 250  # any channel this bright has lost its color
DARK_LEVEL = 25      # every channel this dark is mostly sensor noise


def frame_quality(frame_bgr: np.ndarray, palette: Palette = DEFAULT_PALETTE,
                  notes: dict[str, tuple] | None = None,
                  ready: bool | None = None) -> dict[str, Any]:
    """Detection readiness and exposure, color and focus measures of a frame.

    ``ready``: a scan of this frame would succeed. A caller that already
    knows (the live server decides it from its own board) passes it, which
    skips a second full detection on the hot path; otherwise it is the
    4-marker ``detect_board`` verdict, as before. ``clipped`` / ``dark``:
    fraction of over- and underexposed pixels. ``brightness``: median of the
    brightest channel, 0..1; the wall fills most of the frame, so this reads
    its exposure. ``sharpness``: variance of the
    Laplacian of the lightly blurred gray image (higher is sharper).
    ``note_saturation``: mean HSV saturation inside the detected notes, or
    None when nothing is detected.
    """
    notes = find_notes(frame_bgr, palette) if notes is None else notes
    # cv2.max of the three planes: the same values as frame_bgr.max(axis=2),
    # which numpy reduces over a 3-wide axis ~20x slower (11 ms per frame).
    blue, green, red = cv2.split(frame_bgr)
    brightest = cv2.max(cv2.max(blue, green), red)
    gray = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
    sharpness = cv2.Laplacian(cv2.GaussianBlur(gray, (3, 3), 0), cv2.CV_64F).var()
    shapes = [np.array([(point.x, point.y) for point in polygon.points], np.int32)
              for role in ROLES for polygon in notes[role]]
    note_saturation = None
    if shapes:
        mask = np.zeros(gray.shape, np.uint8)
        cv2.fillPoly(mask, shapes, 255)
        saturation = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2HSV)[..., 1]
        note_saturation = round(float(saturation[mask > 0].mean()), 1)
    if ready is None:
        try:
            detect_board(frame_bgr, palette)
            ready = True
        except ValueError:
            ready = False
    return {
        "ready": ready,
        "clipped": round(float((brightest >= CLIPPED_LEVEL).mean()), 4),
        "dark": round(float((brightest < DARK_LEVEL).mean()), 4),
        "brightness": round(float(np.median(brightest)) / 255, 3),
        "sharpness": round(float(sharpness), 2),
        "note_saturation": note_saturation,
    }
