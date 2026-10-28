# Levels service

Contract: `docs/alignment-and-color.md` section 3.2. A level says where the
start and goal notes go (a box each), how many bumpers it allows, and what
it projects: walls (bounce), spikes (lose) and portals (the ball comes out
at the exit with the same velocity). `levels.json` holds them in shares of
the playfield; `libre` is the old game.

- `services/levels/__init__.py`: load and check the file, `level_geometry`
  (a level in one space's px), `with_obstacles` (onto a physics board),
  `level_check` / `level_problem` (the live notes against the level, the
  operator's words). Ported to `services/browser/levels.mjs`.
- `design.py`: plays a level with simulated notes and searches bumper
  placements that win, ranked by robustness.

```sh
./.venv/bin/python -m unittest services.levels.tests.test_levels
./.venv/bin/python -m services.levels.evals.solvable
./.venv/bin/python -m services.levels.design 4
```

The eval passes when every stored solution wins, every level except one
with `bumpers_max: 0` needs its bumpers, and each level still wins at least
50% of 100 throws with the bumpers stuck by hand (8 px, 4 degrees off).
