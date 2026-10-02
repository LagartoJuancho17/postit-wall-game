# Physics service

This service advances a projected ball through a board that has already been
mapped into **projector pixel coordinates**. Positive `x` points right;
positive `y` points down. `launch(board, 0, speed)` aims right, while 90° aims
down. The yellow polygon supplies the start centroid, orange is the goal,
green polygons are bumpers, and the playfield is the playable boundary.

The ball radius is `BALL_RADIUS_PX = 10`. A ball wins when its circle first
touches orange, loses when its circle first touches the edge of the playfield,
and bounces elastically off green. The service calculates the time of each
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
```

The first command is the local gate suite. The second produces a JSON quality
score for direct shots, rebounds, misses, fast targets, and corner impacts;
its pass threshold is 100%.
