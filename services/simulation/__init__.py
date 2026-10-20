"""Deterministic camera/projector/wall simulator for closed-loop evals.

See ``docs/alignment-and-color.md`` section 5 and ``README.md`` here.
"""

from .model import (Camera, Capture, Paper, Projector, Wall, apply_homography,
                    homography_from_corners, normalized_inverse, standard_rig)
from .projector import (aligned_matrix, contain_transform, fill_polygon, offset_polygon,
                        render_pattern, render_scene, rim_polygon)
from .loop import SimClock, capture_alignment, frame_record, run_closed_loop

__all__ = ["Camera", "Capture", "Paper", "Projector", "SimClock", "Wall", "aligned_matrix",
           "apply_homography", "capture_alignment", "contain_transform", "fill_polygon",
           "frame_record", "homography_from_corners", "normalized_inverse", "offset_polygon",
           "render_pattern", "render_scene", "rim_polygon", "run_closed_loop", "standard_rig"]
