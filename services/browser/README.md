# Browser backend

The static web build mirrors `services/web/backend/server.py` without a Python process. `backend.mjs` handles the HTTP-shaped API; `controller.mjs` owns one shared game for the panel and projector popup.

Every `/api/preview` publishes a deep-copied `live_detection` with camera `width`, `height`, all role polygons, counts, and quality. Exactly one start and one goal prepare an idle board. The full camera rectangle is the playfield. Corner colors and bumpers are optional. No calibration actions, calibration state, or projector targets are used by this controller.

Detections expire after 1500 ms without a new preview. Expiration, an invalid latest frame, or a palette change prevents launching/resetting a stale board. Camera changes discard an attempt because its coordinates belong to the previous camera. Active, paused, and completed attempts retain their original collision board and `game.width`/`game.height`; only the live overlay follows later previews. Reset uses the latest valid camera board. Projector opening and resizing preserve the attempt and choose `projection.mode = "live"`.

`/api/frame` is an optional explicit reread/reset and uses the same camera coordinates. An unpaused running attempt refuses this action. USB camera control is unavailable in the browser build and returns the documented 502 response; palette and camera presets persist through local storage.

The classic detector and homography functions remain available for parity with the standalone Python services. `boardFromNotes` is the separate direct-coordinate web boundary.

## Verification

Regenerate ignored synthetic frames and expected Python answers after changes to the mirrored Python services:

```sh
./.venv/bin/python -m services.browser.parity.export
```

Run route and regression tests, then the full deterministic parity evaluation:

```sh
node --test services/browser/tests/*.test.mjs
node services/browser/evals/parity.mjs
```

The evaluation checks all generated palettes, homographies, trajectories, vision frames, and controller transitions. Its JSON trace names the case count, failures, pass rate, and required threshold of 1. The live regressions cover marker-free boards, missing/duplicate required notes, freshness, camera/palette changes, preserved attempts, and camera/projector resizing.
