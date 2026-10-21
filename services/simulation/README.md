# Simulation service

A deterministic camera / projector / wall model that closes the loop the real
game runs in: the projector draws what the backend detected, the camera
photographs the wall under that light, and the photo goes back into the real
`GameController`. Binding contract: `docs/alignment-and-color.md` section 5.
Nothing here opens a camera or a window, and the same inputs and seed give
byte-identical JPEGs, so evals built on it never flake.

| File | What it does |
| --- | --- |
| `model.py` | `Wall` (reflectance in camera px, ambient light), `Projector` (frame -> wall irradiance through the true homography `G`), `Camera` (gain, white balance, auto exposure, noise, gamma, JPEG), `standard_rig()` (the real setup). |
| `projector.py` | What the projector window draws, in Python: `render_scene(state, theme, size, rules)` with `rules` = `"old"`, `"new"` or `"neutral"`, and `render_pattern(layout, frame)` (contract 1.2). Ports of `offsetConvexPolygon`, `rimPolygon`, `fillPolygon`, `convexHull`, `homographyLocalScale`, `alignedMatrix`. |
| `loop.py` | `run_closed_loop` (snapshot -> render -> capture -> `preview_jpeg`), `frame_record` (compares a preview with the ground truth), `capture_alignment` (the 14 pattern JPEGs the panel would post to `/api/align_frame`). |
| `feedback_eval.py` | The closed-loop feedback eval (below). |
| `tests/test_model.py` | Gate tests, 320x180 at most, 0.3 s. |

## What it models

- **Wall**: light gray paint (reflectance 0.8, +-2% low-frequency
  unevenness) with convex post-it quads of linear RGB reflectance (yellow
  start, orange goal, green bumpers, pink markers). Paper edges get exact
  area coverage (see "Rasterization").
- **Room light**: warm ambient RGB `(1, 0.92, 0.80) * level`; the camera's
  white balance `(0.92, 1, 1.15)` makes that light neutral, so projector
  light is not neutral to it.
- **Projector**: sRGB frame (gamma 2.2) -> Gaussian blur (0.8 projector px:
  lens defocus plus the camera pixel footprint) -> `cv2.warpPerspective`
  through `G` into camera px -> black level 0.003 inside the footprint ->
  3x3 color response matrix (channel crosstalk, white point
  `(1.00, 1.00, 1.08)`). Full white = irradiance 1.0.
- **Camera**: `pixel = gain * white_balance * reflectance * (ambient +
  projector_light)`, seeded shot and read noise, clip, gamma 1/2.2, JPEG
  quality 85 (OpenCV default 4:2:0 chroma, like Chrome's encoder). Exposure
  is fixed (`fixed_gain`, what the camera search sets) or automatic: every
  frame the gain moves 50% of the way toward the gain that puts the frame's
  mean linear luminance (Rec. 709) at 0.18.
- **The real setup** (`standard_rig`): camera 1280x720, projector
  1920x1080 landing on the camera image at
  `(96,52) (1190,74) (1166,668) (112,646)` (keystone plus rotation, about
  1.75 projector px per camera px), the camera itself slightly tilted.
  `scale=` shrinks everything alike for gate tests.
- **Projector rules**, mirroring `services/web/ui/live-view.mjs`:
  - `"old"`: the projector before the contract (commit 8238278):
    `drawLiveDetection` through the contain transform (role fill alpha .3,
    18 px colored shadow, white 2 px rim that still casts the colored
    shadow) plus the old `drawLiveGame`.
  - `"new"`: `drawProjectorScene` (contract 2.1). Aligned: no rim; the
    role color inset 3 px at alpha .35 plus a 4 px border of the same color
    at alpha .35 just inside it (whatever the overlay check said), the
    check's probe squares when forced, ball and aim line through H with H's
    local scale, horizon side taken from the camera center. Unaligned:
    neutral rims on the polygon offset 6 px outward (width 3, miter limit 2,
    convex hull fallback, clipped out of the other notes) through the
    contain transform, no fill, the `Sin alinear · pulsá A en el panel`
    hint. `matrix=` overrides
    `state["alignment"]`, which is read like the projector window reads it.
  - `"neutral"`: the theme background only (contract 2.2).
- **Rasterization**: shapes get exact area coverage (exact along x, 8
  sub-rows along y), the way a canvas anti-aliases; the old shadow is a
  Gaussian of sigma `shadowBlur / 2` (HTML spec). `cv2.fillPoly` was
  dropped because it also paints the pixels its edges touch: +1.5% to +4.7%
  area on a 190 px² note. Area error now < 0.03%, centroid < 0.003 px.

Pixel centers are at integer coordinates in both images; the canvas's +0.5
is a constant the alignment absorbs, so the true camera -> projector matrix
is exactly `inverse(G)`. Checked end to end: pattern frames rendered here,
photographed by this camera and decoded by `services.calibration.coded`
land within 0.02-0.08 px (p95) of `inverse(G)` at ambient 0.01, 0.1 and 1.0,
fixed or automatic exposure.

## What it does not model

- **Real projector color response**: the 3x3 matrix, black level and blur
  are plausible values, not a measurement of the projector in use. Nor its
  brightness falloff across the image (keystone, inverse square).
- **Rolling shutter and timing**: every capture sees one whole projector
  frame. A real camera can expose across a projector refresh (half old, half
  new frame) and runs unsynchronized; the loop here is exactly one preview
  behind, every 250 ms.
- **The iPhone pipeline**: local tone mapping, HDR merging, auto white
  balance drift, sharpening, lens distortion, demosaicing and optical blur.
  Only global gain, fixed white balance, noise, gamma and JPEG are modeled.
- **The physical scene**: glossy paper (specular hot spots), paper curl and
  shadows, people walking in front, flicker from the room lights.
- **Text**: the hint and result texts use a Hershey font at the right size
  and place, not the browser's glyphs.
- **Drift from the JS**: `projector.py` re-implements `live-view.mjs`; a
  change there needs the same change here. No automatic parity check covers
  this pair; `tests/closed-loop-browser.mjs` runs the real renderer in Chrome
  instead.

## Run

```sh
.venv/bin/python -m unittest services.simulation.tests.test_model -v   # gate, ~0.3 s
.venv/bin/python -m services.simulation.feedback_eval                  # eval, ~45 s with 4 jobs
.venv/bin/python -m services.simulation.feedback_eval --only new_aligned_fill_on --jobs 1 --out /tmp/feedback.json
```

The eval prints `{score, pass_threshold, limits, setup, baseline, cases}` as
JSON on stdout and a one-line summary per case on stderr; it exits non-zero
when a NEW case fails. `--jobs` does not change the numbers (checked
byte-identical with 1 and 2 jobs).

## Feedback eval

Each case: 10 warm-up frames, then 60 measured frames (15 s of previews) of
the real `GameController.preview_jpeg`. Renderer-isolating cases (`new_*`)
give the backend and the projector the same known H (`inverse(G)`, or that
plus a 2.0 px translation) without decoding; `system_*` cases run the whole
system: the 14 coded frames through `/api/align_frame`, `align_solve`, then
the overlay check with its probes, and play with the fill the check allowed.
Fixed exposure is metered once before play: under the neutral projection for
NEW cases (contract 2.2), with the projector black for OLD ones (what the old
panel did). Removal cases take the goal down at frame 20.

Scored (every NEW case): 0 flips, 0 wrong-ready frames, 0 phantoms, area
growth <= 5% against the true paper, ghost lifetime <= 3 frames, rim corner
error <= 4 projector px plus any injected alignment error (the outline's
rim polygon against the true paper's: how well the outline lands), color
clearance >= 0 with a real alignment (the role color, `fillPolygon` of the
detected outline, never reaches past the true paper). `fill` and the
overlay check no longer change what an aligned projector draws, so the
`fill_off`/`fill_on` pairs give the same numbers; they stay to prove it.
Scenarios marked `stress=True` are reported, not scored; there are
none since 2026-10-21.

Numbers from 2026-10-21, color inside the paper (`score 1.0`, 19 of 19):

| case | flips | wrong ready | phantoms | growth vs paper | ghost | rim corner max | p95 | color clearance | overlay |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| old_unaligned_dark (baseline) | 12 | 6 | 0 | +1260.0% |  |  |  |  |  |
| old_unaligned_light (baseline) | 0 | 60 | 0 | -2.1% |  |  |  |  |  |
| old_unaligned_dark_goal_removed (baseline) | 4 | 5 | 7 | +1178.3% | 1 |  |  |  |  |
| new_unaligned_dark | 0 | 0 | 0 | -1.0% |  |  |  |  |  |
| new_unaligned_light | 0 | 0 | 0 | -3.0% |  |  |  |  |  |
| new_unaligned_dark_goal_removed | 0 | 0 | 0 | -1.0% | 0 |  |  |  |  |
| new_aligned_fill_off | 0 | 0 | 0 | -0.1% |  | 1.29 | 1.12 | 2.57 |  |
| new_aligned_fill_on | 0 | 0 | 0 | -0.1% |  | 1.29 | 1.12 | 2.57 |  |
| new_aligned_error2px_fill_off | 0 | 0 | 0 | +0.0% |  | 3.24 | 3.12 | 0.37 |  |
| new_aligned_error2px_fill_on | 0 | 0 | 0 | +0.0% |  | 3.24 | 3.12 | 0.37 |  |
| new_aligned_fill_on_goal_removed | 0 | 0 | 0 | -0.0% | 0 | 1.29 | 1.12 | 2.57 |  |
| new_aligned_fill_on_dark_room | 0 | 0 | 0 | +1.4% |  | 2.77 | 1.92 | 1.05 |  |
| new_aligned_fill_on_dark_room_goal_removed | 0 | 0 | 0 | +1.6% | 1 | 2.05 | 1.19 | 1.31 |  |
| new_aligned_fill_on_bright_room | 0 | 0 | 0 | -0.7% |  | 1.64 | 1.52 | 2.56 |  |
| new_aligned_fill_on_light_theme | 0 | 0 | 0 | -3.6% |  | 3.88 | 3.56 | 2.96 |  |
| new_aligned_fill_on_auto_exposure | 0 | 0 | 0 | -0.1% |  | 1.27 | 1.13 | 2.55 |  |
| system_dark | 0 | 0 | 0 | -0.1% |  | 1.27 | 1.12 | 2.55 | ok |
| system_light | 0 | 0 | 0 | -3.6% |  | 3.85 | 3.58 | 3.02 | ok |
| system_dark_room | 0 | 0 | 0 | +1.6% |  | 2.44 | 1.96 | 0.97 | fill_off |
| system_dark_room_goal_removed | 0 | 0 | 0 | +1.6% | 2 | 2.11 | 1.17 | 1.22 | fill_off |
| system_bright_room | 0 | 0 | 0 | -0.7% |  | 1.65 | 1.53 | 2.56 | ok |
| system_auto_exposure | 0 | 0 | 0 | -0.1% |  | 1.28 | 1.12 | 2.54 | ok |

Rim corner errors and color clearance are projector px; a negative
clearance would mean the role color reaches the wall.

Reading:

- **The old projector feeds back**, as measured on the wall: 12 ready/invalid
  flips in 15 s in the dark theme (the field log had 28 per minute), the
  goal's own halo merging into it (area x13). In the light theme the old
  exposure (metered with the projector black) clips most of the frame and
  the board is never ready.
- **The new rules remove the feedback** in every regime: 0 flips, 0
  wrong-ready frames, 0 phantoms, outlines within a few % of the paper.
- **The outline lands where it should.** On 2026-10-19 the rim was 6-12 px
  off and lay on the paper (clearance down to -8 px): detections ran 1-3
  camera px inside the paper. Sub-pixel outline refinement on the
  saturation channel in two passes (contract 2.4) closed it. Unaligned, the
  projector keeps each rim off every other note (2.1), and the rim bridge
  (2.5) joins a strip our rim cut off a paper back to it, never two notes
  and never moving an outline.
- **The color stays on the paper** (2026-10-21, contract 2.1). Aligned, the
  paper glows in its role color up to 3 projector px from its edge, with a
  4 px border of the same color at alpha .35, and no rim. Closer to the edge
  the color spills: inset 0-1 px grows the outlines 5-7%. A brighter border
  (alpha .9, inset 2) washed the paper's saturation edge out in a dark room
  under its high gain: a corner 18 px off and a 9-frame ghost. With the
  adopted values every case passes, the outline stays within -3.6% to
  +1.6% of the paper, the color at least 0.37 px off the wall even with a
  2 px alignment error, and a removed goal's color dies in 1-2 frames as it
  shrinks by the inset (`*_dark_room_goal_removed`).
- **The light theme is the floor** (3.9 px): its flood lowers the paper's
  saturation edge contrast; with no projection at all detection alone gives
  ~3 px there.
- **The overlay check is a diagnostic.** In a dark room it reports
  `fill_off` (a probe on bare wall reads as a post-it), and the color stays
  on: `system_dark_room` passes with it.
