"""Detect the physical Post-it board in one webcam frame."""

from .detector import EXPECTED_NOTES, DetectionError, detect_board, find_notes
from .palette import DEFAULT_PALETTE, build_palette, hsv_from_hex, rule_from_hex
from .quality import frame_quality

__all__ = ["DEFAULT_PALETTE", "EXPECTED_NOTES", "DetectionError", "build_palette",
           "detect_board", "find_notes", "frame_quality", "hsv_from_hex", "rule_from_hex"]
