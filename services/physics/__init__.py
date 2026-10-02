"""Continuous, deterministic ball physics in projector pixel coordinates.

Angles follow screen coordinates: 0° points right and 90° points down.
Call :func:`step` with elapsed seconds between rendered frames. The board is
fixed for a run; scan a new board and call :func:`ready_state` between runs.
"""

from .engine import BALL_RADIUS_PX, GameState, GameStatus, launch, ready_state, step

__all__ = [
    "BALL_RADIUS_PX",
    "GameState",
    "GameStatus",
    "launch",
    "ready_state",
    "step",
]
