"""Detect the physical Post-it board in one webcam frame."""

from .detector import DetectionError, detect_board

__all__ = ["DetectionError", "detect_board"]
