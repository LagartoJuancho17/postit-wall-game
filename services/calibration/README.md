# Calibration service

Converts webcam coordinates to projector coordinates for a flat wall. This
service has no camera or window dependency: the application supplies captured
frames and projector positions, then stores the returned `Matrix3` while the
equipment stays fixed.

Two ways to get the camera → projector homography `H` live here:

- **Coded dot alignment** (`coded.py`, used by the game): one run of 14
  projected frames yields up to 63 correspondences. Binding contract:
  `docs/alignment-and-color.md` sections 1.1-1.3. The JavaScript twin is
  `services/browser/alignment.mjs`.
- **Single dots** (`detect_projected_dot`, below): one dot per dark/lit
  frame pair. Kept for reference and its tests.

Shared helpers: `estimate_homography` (OpenCV RANSAC, 3 px), `transform_point`,
`transform_board`, `reprojection_errors`.

## Coded dot alignment

### Layout and patterns (contract 1.1, 1.2)

`alignment_layout(width, height)` is a pure function of the projector viewport
in projector px. It returns the layout JSON the backends keep in state
(`version`, `width`, `height`, `cols`, `rows`, `radius`, `bits`, `dots`,
`frames`). The grid has `cols = clamp(round(sqrt(60 * aspect)), 4, 12)` and
`rows = clamp(round(60 / cols), 3, 9)`, trimmed to at most 63 dots, with a 6%
margin. `round` is Python's (half to even): 169 x 240 gives 6 columns where
JavaScript's `Math.round` would give 7, which is why the JS port uses `pyRound`.

| projector | grid | dots | radius px |
|---|---|---|---|
| 1920 x 1080 | 10 x 6 | 60 | 12.96 |
| 1280 x 720 | 10 x 6 | 60 | 8.64 |
| 1080 x 767 | 9 x 7 | 63 | 9.204 |
| 787 x 752 | 8 x 7 | 56 | 9.024 |
| 320 x 240 | 9 x 7 | 63 | 4.0 |

Dot `id` (1..N, row-major) carries `alignment_code(id) = id | parity << 6`,
an even parity bit on top of the 6 id bits, so any single wrong bit is caught.
The projector shows 14 frames, `(bit, inverse)` for bit 0..6, plain then
inverse (`ALIGNMENT_FRAMES`). `pattern_dots(layout, bit, inverse)` lists the
dots drawn white in a frame: every dot is lit in exactly one frame of each
pair, so the projected light is the same in all 14 frames and the iPhone's
auto exposure stays put.

### Decode (contract 1.3)

`decode_alignment(frames, layout)` takes the 14 grayscale uint8 frames keyed
by `(bit, inverse)` and runs the contract's 12 steps:

1. per pair, gain = median of `P / Q` on the 4-pixel grid where both are >= 8;
2. `D_b = P - gain * Q`: wall, ambient light and post-its cancel;
3. strength `S` = mean of `|D_b|` over the 7 bits;
4. threshold = max(12, 4 x median + 6, 40% of the 99.9th percentile) of `S`
   on the 2-pixel grid;
5. connected components of `S >= threshold` (8-connectivity, CV_32S);
6. components of area >= 4 that do not touch the frame border get an
   `S`-weighted centroid (pixel centers at integer coordinates), a bit per
   pair from the sign of the weighted mean of `D_b`, and a confidence
   `|mean| / mean |D_b|`; below 0.35 on any bit: `ambiguous`;
7. wrong parity: `parity`; id outside 1..N: `unknown_id`; an id claimed twice
   drops every claimant: `duplicate`;
8. area more than 6x or less than 1/6 of the median: `area`;
9. ids with `id % 5 == 0` are held out, the rest fit;
10. at least 16 dots (`few_points`); RANSAC homography on the fit set; at
    least `max(10, ceil(0.8 * fit))` fit points within 3 px (`inconsistent`);
11. at least 4 holdout points, at most `max(1, floor(0.1 * holdout))` beyond
    5 px, and the rest with p95 <= 1.5 px and max <= 3 px (`inaccurate`);
12. the convex hull of the decoded dots covers >= 30% of the projector
    frame (`low_coverage`).

Missing frames (or frames of different sizes) fail with `frames` before any
step runs. The Spanish operator texts are `ALIGNMENT_REASONS`, copied from
the contract table; a gate test checks them against the document.

Output: the contract's keys (`ok`, `reason`, `matrix`, `decoded`, `total`,
`fit`, `inliers`, `holdout`, `holdout_p95_px`, `holdout_max_px`, `outliers`,
`coverage`, `threshold`, `gains`, `rejected`) plus `code`: `None` on success,
else the failure code (`few_points`, `inconsistent`, `inaccurate`,
`low_coverage`, `frames`). The server logs it in `alignment_failed {code, ...}`
and the panel retries on `inaccurate` / `inconsistent` from it, without
parsing the Spanish text. Metrics a failed run never reached are `None`.
Floats are not rounded here; the server rounds `quality` for the snapshot.

### Where the contract is silent

Decisions the JS twin must copy:

- A bit with no signal anywhere on a component (`sum(w * |D_b|) == 0`, e.g. a
  stale pair with identical frames) has confidence 0, so the dot is
  `ambiguous`. The formula would give 0 / 0; NaN would slip through
  `min(c) < 0.35` in both languages.
- Frames present but of different sizes count as missing (`frames`).
- `estimate_homography` raising on the fit set is `inconsistent`, with
  `inliers: 0`.
- `inaccurate` needs a number for `{p95:.1f}` even when every holdout error
  is an outlier: then it shows the p95 of the finite holdout errors, or 0.0
  when there are none. A holdout point that `H` sends to infinity counts as
  an infinite error, hence an outlier.
- Coverage uses `cv2.convexHull` on float32 projector points and
  `cv2.contourArea` (same OpenCV release in both backends).

### Bit-for-bit parity with JavaScript

Medians and percentiles run on an explicitly sorted float64 array with
numpy's own formulas (`_sorted_median`, `_sorted_percentile`, including
numpy's halfway rule that interpolates from the upper value). Component sums
are sequential in raster order (`np.bincount` with weights adds in input
order, like a JS loop; `ndarray.sum` would sum pairwise). Correspondences are
sorted by id before RANSAC, so label order never matters. Gate tests compare
every one of these against numpy directly.

### Measured

Gate tests (`tests/test_coded.py`, 40 tests, 320x240 frames, about 0.17 s):
a clean 320x240 decode recovers the true homography with truth error p95
0.036 px and max 0.046 px; a stale pair rejects all 63 dots as `ambiguous`
and fails with `few_points`; a saturated glare under 20% exposure drift
produces no component; a light in sync with the plain frames decodes as code
127 and is stopped by parity; dots cut by the frame border are left out.
All 23 hand-made mutations of `coded.py` (border rule, connectivity,
thresholds, grids, percentile rule, parity, duplicates, rounding, ...) fail
the gate suite.

Eval (`evals/coded.py`, camera 1280x720 with gamma 2.2, blur, noise, JPEG,
per-frame exposure drift; truth = the true projector → camera map `G`):

| case | projector | outcome | decoded | holdout p95 / max px | truth p95 / max px |
|---|---|---|---|---|---|
| fullhd_keystone | 1920x1080 | ok | 60/60 | 0.116 / 0.120 | 0.064 / 0.076 |
| gain_drift_20 | 1920x1080 | ok | 60/60 | 0.124 / 0.126 | 0.070 / 0.104 |
| gain_drift_35 | 1920x1080 | ok | 60/60 | 0.109 / 0.145 | 0.063 / 0.073 |
| overhang_partial_view | 1920x1080 | ok | 32/60 | 0.025 / 0.027 | 0.033 / 0.048 |
| small_projected_area | 1080x767 | ok | 63/63 | 0.146 / 0.157 | 0.055 / 0.065 |
| windowed_787x752 | 787x752 | ok | 56/56 | 0.074 / 0.084 | 0.029 / 0.042 |
| heavy_blur_jpeg60 | 1920x1080 | ok | 60/60 | 0.152 / 0.178 | 0.051 / 0.061 |
| postits_and_glossy_reflection | 1920x1080 | ok | 59/60 | 0.294 / 0.492 | 0.091 / 0.149 |
| ambient_high_low_contrast | 1920x1080 | ok | 60/60 | 0.173 / 0.185 | 0.061 / 0.077 |
| transition_blend_40 | 1920x1080 | ok | 60/60 | 0.105 / 0.132 | 0.068 / 0.081 |
| camera_rotated_180 | 1920x1080 | ok | 60/60 | 0.088 / 0.089 | 0.066 / 0.077 |
| combined_worst | 1920x1080 | ok | 60/60 | 0.375 / 0.671 | 0.078 / 0.109 |
| stale_frame | 1920x1080 | few_points | 12/60 | - | - |
| corner_view | 1920x1080 | low_coverage | 20/60 | 0.041 / 0.042 | - |

Score 14/14 (threshold 1.0), 0 wrong ids, decode 25-40 ms per run on a
1280x720 camera (38.7 ms mean over the sweep below, which ran while other
work loaded the machine). Robustness sweep `--seeds 10`: 140/140 runs pass, 0 wrong
ids, worst truth error p95 0.106 px and max 0.169 px (contract: p95 <= 1.5,
max <= 3). The stale pair ended `few_points` in 8 of 10 seeds and `ok` in 2:
noise sometimes leaves enough dots with a confident bit, and parity stopped
every wrong id, so those two alignments were correct (truth p95 <= 0.099 px).

### Verification

From the project root:

```sh
.venv/bin/python -m unittest services.calibration.tests.test_coded -v
.venv/bin/python -m services.calibration.evals.coded
.venv/bin/python -m services.calibration.evals.coded --seeds 10   # about 2 min
```

`tests/render.py` is the shared synthetic projector + camera used by the gate
tests and the eval (anti-aliased dots at exact fractional centers, homography
warp, wall reflectance with post-its, glare, gamma, noise, JPEG, exposure
drift, stale and mid-transition frames).

## Single-dot procedure (reference)

1. Freeze webcam focus, exposure and white balance if the camera allows it.
2. Display a black projector frame and capture `dark_bgr`.
3. Project one small white dot at a known projector pixel. Capture `lit_bgr`.
4. Call `detect_projected_dot(dark_bgr, lit_bgr)` to get its webcam pixel.
5. Repeat at well-spread projector positions, at least four and preferably
   8–12. Pair the points in the same order and call `estimate_homography`.
6. Calculate `reprojection_errors` and project a separate check grid. Keep
   the setup only if check points land within half the game ball's radius.
7. Detect the four fuchsia post-its and game post-its in webcam pixels. Call
   `transform_board` before passing the board to physics.

The four fuchsia post-its mark the playable quadrilateral. Their positions
alone cannot determine the camera/projector mapping: the white dots establish
which webcam pixels correspond to known projector pixels. Moving the webcam,
projector, focus, or resolution requires a new alignment. A non-flat wall
cannot be described exactly by one homography.

An unclear or missing projected dot raises `ValueError`; the application should
recapture the frame instead of accepting an uncertain correspondence. OpenCV
RANSAC rejects mismatched observations when more than four pairs are supplied.

```sh
.venv/bin/python -m unittest services.calibration.tests.test_calibration
.venv/bin/python -m services.calibration.evals.synthetic
```

The synthetic eval prints measured error as JSON and fails unless the 95th
percentile held-out mapping error and dot center error are each at most one
pixel.
