# Browser backend

The static web build (Vercel) runs without a Python process: these modules
port the Python services to JavaScript on opencv.js 4.12, the same OpenCV
release as `opencv-python` in `requirements.txt`, and answer the panel's
HTTP calls inside the page. The projector window reaches the panel's backend
through `window.opener`.

| Module | Port of |
| --- | --- |
| `backend.mjs` | `create_server` routes: `/api/state`, `/api/tick`, `/api/action`, `/api/preview`, `/api/frame`, `/api/align_frame`, `/api/check_frame`, `/api/camera`, with the same checks and statuses. Header checks (`X-Postit-Camera`, `X-Postit-Align`, `X-Postit-Check`) run before decoding, as in Python. |
| `controller.mjs` | `GameController`: live detection, boards, attempts, palette, camera presets, the alignment state machine, the overlay check and its probes, the rim footprint (contract 2.5), the game in projector px once aligned. |
| `alignment.mjs` | `services/calibration/coded.py`: layout, codes, pattern frames and the decoder (contract 1.1-1.3). |
| `vision.mjs` | `services/vision/detector.py` + `quality.py`: color masks, candidates, two-pass sub-pixel refinement (2.4), the joining rim bridge (2.5), boards and frame quality. |
| `calibration.mjs` | `estimate_homography` (OpenCV's own `findHomography`), point and board mapping, `offset_convex_polygon`. |
| `physics.mjs`, `palette.mjs`, `numbers.mjs` | The physics engine, the palette rules, and Python's `round` (half to even on the exact binary value) and `%`. |

What the browser cannot do is said where it happens: there is no USB camera
control (`/api/camera` answers the documented 502), and the palette, camera
presets and the event log live in the page's storage (`postitEvents()` in
the console). Every 25 previews log their detection time
(`preview_timing`).

## Parity

The port must give Python's answers. `parity/export.py` asks the real Python
code and writes `parity/golden/` (gitignored); the tests replay every case:

- palettes (hex to HSV, picked-color rules, 401 random builds);
- homographies and mapped points; ball trajectories step by step;
- note detection, boards and frame quality on 27 frames;
- sub-pixel refinement: 73 cases, every outcome, corners within 1e-9 px
  (measured 1.4e-14);
- the rim bridge: a cut paper joined, a ring beside a paper changing
  nothing, bare wall;
- polygon offsets; alignment codes, layouts and pattern frames;
- 10 full decodes covering success and every failure code (`few_points`,
  `inconsistent`, `inaccurate` and `low_coverage` from lens distortion and
  framing, `frames`); homographies within 2e-11;
- probe placement for the overlay check;
- 15 controller scripts (406 steps), including a whole alignment with the
  overlay check, every refusal and every invalidation reason.

2065 of 2065 cases agree. To get the same bits, Python and JS avoid the few
operations whose last bit differs between them: Python 3.12's compensated
`sum()` (both sum left to right), `hypot` (both use `sqrt(dx*dx + dy*dy)`
where it decides a pixel), and numpy's pairwise reductions (spelled out as
loops). OpenCV calls are the same calls with the same arguments.

## Verification

Regenerate the golden answers after changing a mirrored Python service:

```sh
./.venv/bin/python -m services.browser.parity.export
node --test services/browser/tests/*.test.mjs
node services/browser/evals/parity.mjs
```

The eval's JSON names the case count, failures, pass rate and the required
threshold of 1. `tests/closed-loop-browser.mjs` runs this backend in Chrome
with the real panel and projector (see `services/web/README.md`).
