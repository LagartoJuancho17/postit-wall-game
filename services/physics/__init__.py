"""Continuous, deterministic ball physics in projector pixel coordinates.

Angles follow screen coordinates: 0° points right and 90° points down.
Gravity, when given to :func:`launch`, pulls toward +y (down the wall). The
web game uses :func:`drop`: the ball starts at rest under the start note
and falls.
Call :func:`step` with elapsed seconds between rendered frames. The board is
fixed for a run; scan a new board and call :func:`ready_state` between runs.
"""

from .engine import (BALL_RADIUS_PX, DROP_GAP_PX, GRAVITY_SHARE, ballistic_aim_degrees, LAUNCH_SPEED_SHARE,
                     MAX_ATTEMPT_S, GameState, GameStatus, drop, drop_point, drop_ready_state, launch, ready_state,
                     step)

__all__ = [
    "BALL_RADIUS_PX",
    "DROP_GAP_PX",
    "ballistic_aim_degrees",
    "GRAVITY_SHARE",
    "LAUNCH_SPEED_SHARE",
    "MAX_ATTEMPT_S",
    "GameState",
    "GameStatus",
    "drop",
    "drop_point",
    "drop_ready_state",
    "launch",
    "ready_state",
    "step",
]
