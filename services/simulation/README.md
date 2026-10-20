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
- **Projector rules**, mirroring `services/web/ui/live-view.mjs` as of
  2026-10-19:
  - `"old"`: the projector before the contract (commit 8238278):
    `drawLiveDetection` through the contain transform (role fill alpha .3,
    18 px colored shadow, white 2 px rim that still casts the colored
    shadow) plus the old `drawLiveGame`.
  - `"new"`: `drawProjectorScene` (contract 2.1). Aligned: neutral rim on
    the polygon offset 5 px outward (width 3, miter limit 2, convex hull
    fallback), optional fill inset 5 px at alpha .35, ball and aim line
    through H with H's local scale, horizon side taken from the camera
    center. Unaligned: rims through the contain transform, no fill, the
    `Sin alinear · pulsá A en el panel` hint. `matrix=` overrides
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
  change there needs the same change here (no automatic parity check yet).

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
the real `GameController.preview_jpeg`. Aligned cases hand the projector
`inverse(G)` (or that plus a 2.0 px translation) because the backend has no
alignment endpoints yet. Fixed exposure is metered once before play: under
the neutral projection for NEW cases (contract 2.2), with the projector black
for OLD ones (what the old panel did). Removal cases take the goal down at
frame 20. Scored (NEW cases only): 0 flips, 0 wrong-ready frames, 0
phantoms, area growth <= 5% both vs the true paper and vs the same paper
detected under the neutral projection, ghost lifetime <= 3 frames, rim
corner error <= 3 projector px.

Numbers from 2026-10-19 (score 0.2308 = 3 of 13 NEW cases pass):

| case | flips | wrong ready | phantoms | growth vs neutral | min area vs neutral | ghost | rim corner max | no projection | rim clearance |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| old_unaligned_dark (baseline) | 10 | 5 | 0 | +1354% | 0.980 | | | | |
| old_unaligned_light (baseline) | 0 | 60 | 0 | +2.7% | 0.988 | | | | |
| old_unaligned_dark_goal_removed (baseline) | 4 | 4 | 6 | +1150% | 0.980 | 1 | | | |
| new_unaligned_dark | 0 | 0 | 0 | +1.7% | 0.980 | | | | |
| new_unaligned_light | 0 | 0 | 0 | +2.4% | 0.988 | | | | |
| new_unaligned_dark_goal_removed | 0 | 0 | 0 | +1.7% | 0.980 | 0 | | | |
| new_aligned_fill_off | 0 | 0 | 0 | +1.3% | 0.980 | | 5.93 | 5.93 | -2.24 |
| new_aligned_fill_on | 0 | 0 | 0 | +2.4% | 0.977 | | 6.38 | 5.93 | -2.57 |
| new_aligned_error2px_fill_off | 0 | 0 | 0 | +2.4% | 0.980 | | 9.37 | 6.90 | -5.45 |
| new_aligned_error2px_fill_on | 0 | 0 | 0 | +2.4% | 0.962 | | 9.38 | 6.90 | -5.45 |
| new_aligned_fill_on_goal_removed | 0 | 0 | 0 | +2.4% | 0.977 | 0 | 6.38 | 5.93 | -2.57 |
| new_aligned_fill_on_dark_room | 0 | 0 | 0 | +1.2% | 0.886 | | 11.93 | 5.93 | -8.01 |
| new_aligned_fill_on_dark_room_goal_removed | 0 | 0 | 0 | +1.2% | 0.886 | 1 | 11.93 | 5.93 | -8.01 |
| new_aligned_fill_on_bright_room | 0 | 0 | 0 | +0.3% | 0.978 | | 6.72 | 5.93 | -2.80 |
| new_aligned_fill_on_light_theme | 0 | 0 | 0 | +4.7% | 0.969 | | 10.76 | 6.02 | -6.93 |
| new_aligned_fill_on_auto_exposure | 0 | 0 | 0 | +1.3% | 0.977 | | 6.38 | 5.93 | -2.57 |

Rim corner errors and clearance are projector px; "no projection" is the
same metric for detections under the neutral projection (detection alone,
no feedback); a negative clearance means the rim stroke lands on the paper.

Reading:

- **The old projector feeds back**, as measured on the wall: 10 ready/invalid
  flips in 15 s in the dark theme (the field log had 28 per minute), the
  goal's own halo merging into it (area x14.5). In the light theme the old
  exposure (metered with the projector black) clips 69% of the frame and the
  board is never ready.
- **The new rules remove the feedback** in every regime: 0 flips, 0
  wrong-ready frames, 0 phantoms, growth <= +4.7% against the open-loop
  detection, a removed goal gone within 1 frame (0 in normal light).
- **The rim corner check fails everywhere aligned**, and mostly not because
  of the rules: with no projection at all, detections already put the rim
  5.93 px off. `find_notes` returns integer contour vertices on boundary
  pixel centers after a 3x3 open/close, so detected notes run 1-3 camera px
  inside the paper (areas 8-12% small). Even a perfect binary mask through
  that contour path is 1.2 camera px off at the corners (2.1 projector px),
  and the 3x3 open/close makes it 2.1 camera px (3.7 projector px): over
  the 3 px budget before color thresholds or JPEG.
- **Where the rule set itself adds error**: the rim's inner edge lands on
  the paper at the corners (clearance < 0), and where the rim contrasts
  strongly with the paper it eats the edge and pulls itself further in. In
  a dark room (white rim, gain 20) notes shrink to 0.886 of the open-loop
  area and the corner error doubles (11.9 px); in the light theme (dark rim
  on a flooded wall) it reaches 10.8 px. Stable (no flips), but the outline
  no longer lands on the paper.
