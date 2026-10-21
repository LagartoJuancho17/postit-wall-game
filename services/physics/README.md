# Physics service

This service advances a projected ball through a board that has already been
mapped into **projector pixel coordinates**. Positive `x` points right;
positive `y` points down. `launch(board, 0, speed)` aims right, while 90° aims
down. The yellow polygon supplies the start centroid, orange is the goal,
green polygons are bumpers, and the playfield is the playable boundary.

The ball radius is `BALL_RADIUS_PX = 10`. A ball wins when its circle first
touches orange, loses when its circle first touches the edge of the playfield,
and bounces off green.

**Gravity.** `launch(board, angle, speed, gravity_px_s2)` pulls the ball
toward +y, down the wall. The web game launches at `LAUNCH_SPEED_SHARE = 1.7`
and `GRAVITY_SHARE = 1.6` times the field's shorter side (px/s, px/s²): a 45°
shot rises ~0.45 of the side and lands ~1.8 sides away after ~1.5 s. Each
step sweeps collisions along the chord with the mean velocity, which ends
exactly on the parabola (`x + v t + g t² / 2`, whatever `dt_s`) and departs
from it by at most `g (dt/2)² / 2` (0.01 px at 120 Hz). Under gravity a bounce
keeps `RESTITUTION = 0.8` of its normal speed; one weaker than `REST_STEPS`
steps of gravity rests and the ball slides along the surface (no endless
micro-bounces, one event per step); an attempt is lost after
`MAX_ATTEMPT_S = 20`. `ballistic_aim_degrees(dx, dy, speed, gravity)` gives the
lower arc through a point. With `gravity_px_s2 = 0` (the classic game) the
motion is straight and elastic, bit for bit as before. The service calculates the time of each
edge or corner impact, including multiple contacts during one `step`. The
returned immutable `GameState` records position, velocity, elapsed simulation
time, rebound count, and status. These fields make the outcome observable
without relying on rendered frames.

```python
from services.physics import launch, step

state = launch(projector_board, angle_degrees=20, speed_px_s=220)
state = step(projector_board, state, dt_s=1 / 60)
```

Keep the board fixed while `status == RUNNING`. To move notes between attempts,
scan and calibrate a new board, then call `ready_state` or `launch` again. The
service assumes each polygon is convex and ordered around its perimeter;
`ready_state` rejects invalid polygons and a start that overlaps an obstacle
or does not fit within the playfield. It does not resolve overlapping green
notes. Extremely high speed and duration combinations exceeding 1,024
contacts in one call raise an error so simulation work cannot grow without
bound; use shorter `dt_s` in that case.

From the project root, with Python 3.10 or newer (`python3.12` on the tested
macOS machine):

```sh
python3.12 -m unittest discover -s services/physics/tests
python3.12 -m services.physics.evals.scenarios
python3.12 -m services.physics.evals.gravity
```

The first command is the local gate suite. The second produces a JSON quality
score for direct shots, rebounds, misses, fast targets, and corner impacts;
its pass threshold is 100%. The third measures gravity: the share of random
boards some 5° aim wins (79/80, threshold 95%), the error against the
analytic parabola (3e-12 px), and that every attempt with bumpers ends.
