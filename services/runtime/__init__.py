"""macOS camera, projector, controls, and game session."""

import os

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from .session import Session, SessionConfig

__all__ = ["Session", "SessionConfig"]
