# Alignment and color under projection: design contract

Status: contract for the 2026-10-19 work. Every implementation (Python server,
JS port in `services/browser`, panel, projector, simulator, tests) follows
this document. If code and this document disagree, this document wins until
it is deliberately changed.

## Why

Measured on the real setup (iPhone Continuity camera 1280x720, projector up
to 1920x1080), `runs/web-events.jsonl` on 2026-10-19 shows 730
`live_board_ready`/`live_board_invalid` flips in 26 minutes ("llegada 2",
"llegada 0", "llegada 3", "start overlaps goal"). Two causes:

1. **Projected color feeds back into detection.** The projector paints every
   detected note in its own role color (30% fill + 18 px colored blur halo).
   30% of the goal color #ff9b49 is HSV (13,181,76), inside the goal rule.
   The camera re-detects the halo as a note.
2. **No camera to projector geometry.** The projector fits the camera frame
   with a uniform scale (`containTransform`). Outlines land on the papers only
   if camera and projector frame the wall identically, which never happens.

Plus: colors and camera exposure are sampled with the projector black, while
detection always runs with the projector lit (light theme floods the wall).

## Measurable outcomes (the trace)

| Outcome | Where it is measured | Pass |
|---|---|---|
| Alignment accuracy | `alignment_ready` log: holdout p95/max px; sim eval vs ground truth | holdout p95 <= 1.5 px, max <= 3 px; sim truth error p95 <= 1.5 px |
| Wide lens aligns (1.3.1) | `alignment_ready.lens_k` / `alignment_failed` counts in `runs/web-events.jsonl`; `evals.coded` wide-lens cases vs ground truth | full-frame view through k in [-0.05, 0.10] aligns, fitted k within 0.003, truth max <= 3 px (measured 0.19 px; without step 10b all 4 cases end `inconsistent`) |
| Outline lands on paper | sim eval + Chrome e2e: the outline's rim polygon vs the rim of the true paper, projector px | max corner error <= 4 px (camera-limited: 2.0 px dark theme, 3.0 px light theme with no projection at all), plus any injected alignment error; color clearance >= 0 (the role color never reaches the wall) with a real alignment |
| No feedback | sim eval: static wall, 60 closed-loop frames, both themes | 0 ready/invalid flips, 0 phantom notes, note area growth <= 5% |
| Ghosts die | sim eval: paper removed while projecting | gone within 3 frames |
| Colors picked under play light | sim eval: pick under neutral, detect under live | role counts correct in both themes |
| Real-world flips | `live_board_ready/invalid` count per minute in `runs/web-events.jsonl` | trending to 0 on a static wall |
| Play without the keyboard (3.1) | `attempt_countdown {source}` in `runs/web-events.jsonl`: share of `gesture` and `button`; `evals/handsfree.py` | real covers play >= 97 % (98.5), 0 false plays an hour on an idle wall, walker, moved, removed |
| Ripple never feeds back (3.1) | Chrome closed loop, room `handsfree`: a post-it stuck mid-game | 8/8 samples with exactly the papers on the wall |
| Levels can be won by hand (3.2) | `services/levels/evals/solvable.py`: each level's solution replayed with hand error; `attempt_finished {level, ended_by}` and `level_won` in `runs/web-events.jsonl` | every solution wins, every level but 1 needs its bumpers, each wins >= 50 % with bumpers 8 px / 4 degrees off (min 52 %, level 5) |
| Level light never feeds back (3.2) | Chrome closed loop, room `levels`: boxes, title, banner and spikes projected | 8/8 samples with exactly the papers, a start out of its box refused, level 1 won and level 2 next, spikes end the drop |

## Coordinate systems

- **camera px**: pixels of the JPEG frame the panel sends (`W_c x H_c`).
- **projector px**: CSS pixels of the projector window viewport as reported
  by `projection_open` (`W_p x H_p`). Patterns and overlays are drawn in
  these units (the canvas applies devicePixelRatio itself).
- **H**: 3x3 row-major `[[h00,h01,h02],[h10,h11,h12],[h20,h21,h22]]` mapping
  camera px -> projector px, normalized so `h22 == 1`. Produced by
  `estimate_homography(camera_points, projector_points)`.

## 1. Automatic alignment: binary-coded dot grid with inverse pairs

### 1.1 Layout (pure function, identical in Python and JS)

`alignment_layout(width, height) -> dict` (Python
`services/calibration/coded.py`; JS `services/browser/alignment.mjs`; the
panel/projector never compute it, they read it from state):

```
aspect = width / height
cols = clamp(py_round(sqrt(60 * aspect)), 4, 12)
rows = clamp(py_round(60 / cols), 3, 9)
while cols * rows > 63: rows -= 1
margin = 0.06
x(i) = width  * (margin + (1 - 2*margin) * i / (cols - 1))   i = 0..cols-1
y(j) = height * (margin + (1 - 2*margin) * j / (rows - 1))   j = 0..rows-1
radius = max(4.0, 0.012 * min(width, height))
dot k = j*cols + i (row-major), id = k + 1  (ids 1..N, N = cols*rows <= 63)
code(id) = id | (popcount(id) % 2) << 6        # 6 data bits + even parity bit 6
bits = 7
frames = [{bit: b, inverse: false}, {bit: b, inverse: true}  for b in 0..6]   # 14 frames, this order
```

`py_round` is Python's `round` (half to even); JS uses `pyRound` from
`services/browser/numbers.mjs`.

Layout JSON (in state while collecting):

```json
{"version": 1, "width": 1920, "height": 1080, "cols": 10, "rows": 6,
 "radius": 12.96, "bits": 7,
 "dots": [{"id": 1, "x": 115.2, "y": 64.8}, ...],
 "frames": [{"bit": 0, "inverse": false}, {"bit": 0, "inverse": true}, ...]}
```

Coordinates are full floats (not rounded) in the layout.

### 1.2 What the projector draws

- Pattern frame `{bit, inverse}`: background pure black `#000000` in both
  themes; dot `id` drawn as a filled white (`#ffffff`) circle of `radius` at
  `(x, y)` iff `((code(id) >> bit) & 1) == (inverse ? 0 : 1)`.
- Warm-up frame: identical to frame `{bit: 0, inverse: false}`. Shown before
  the first capture so auto exposure settles at the pattern light level.
- Every dot is lit in exactly one frame of each pair, so the total projected
  light is about constant across the sequence (keeps iPhone auto exposure
  stable).

### 1.3 Decode (Python `decode_alignment(frames, layout)`, JS twin)

Input: 14 grayscale uint8 frames, all the same size, keyed by
`(bit, inverse)`. Grayscale = `cvtColor(BGR2GRAY)` of the decoded JPEG.
Arithmetic in float64. Steps, numbered so the JS port can mirror them:

1. For each bit `b`: `P = frame(b, false)`, `Q = frame(b, true)` as float64.
   Gain `g_b` = median of `P/Q` over the subsample grid `y % 4 == 0 and
   x % 4 == 0` restricted to pixels with `P >= 8 and Q >= 8`; `g_b = 1.0` if
   that set is empty. Median: numpy definition (sort; even count = mean of
   the two middle values).
2. `D_b = P - g_b * Q`.
3. `S = (sum_b |D_b|) / 7`.
4. Threshold `t = max(12.0, 4.0 * median(S_sub) + 6.0, 0.4 * pct(S_sub, 99.9))`
   where `S_sub` is S on the subsample grid `y % 2 == 0 and x % 2 == 0` and
   `pct` is numpy's default linear-interpolation percentile.
5. `mask = (S >= t)` as uint8 (0/1); `cv.connectedComponentsWithStats(mask,
   8 connectivity, CV_32S)`.
6. For each component label >= 1 with area >= 4 whose bounding box does not
   touch the image border (`left > 0`, `top > 0`, `left+width < W`,
   `top+height < H`):
   - weights `w = S` over the component's pixels;
   - centroid `(sum(w*x)/sum(w), sum(w*y)/sum(w))` (pixel centers at integer
     coordinates);
   - for each bit: `v_b = sum(w * D_b) / sum(w)`; `bit_b = 1 if v_b > 0 else 0`;
     confidence `c_b = |v_b| / (sum(w * |D_b|) / sum(w))`;
   - reject if `min_b c_b < 0.35` (reason counter `ambiguous`).
7. `id = code & 63`, parity bit = bit 6; reject if parity is wrong
   (`parity`), if `id < 1 or id > N` (`unknown_id`). If two components decode
   the same id, drop all of them (`duplicate`).
8. Area sanity: `A_med` = median area of the surviving components; drop those
   with `area > 6*A_med or area*6 < A_med` (`area`).
9. Correspondences `camera centroid <-> layout dot (x, y)`. Fit set: ids with
   `id % 5 != 0`; holdout set: `id % 5 == 0`.
10. Require `decoded >= 16` (else failure `few_points`). `H =
    estimate_homography(fit_cam, fit_proj)` (RANSAC 3 px, existing function).
    Fit inliers = fit points with reprojection error <= 3.0 px. Require
    `inliers >= max(10, ceil(0.8 * fit))` (else `inconsistent`).
    **10b.** If steps 10-11 fail with `inconsistent` or `inaccurate`, fit the
    lens of section 1.3.1 and run steps 10-11 again on straightened camera
    points; keep that result if it passes, else report the first failure.
11. Holdout errors = `reprojection_errors(holdout_cam, holdout_proj, H)`.
    Outliers = errors > 5.0 px. Require `holdout >= 4`,
    `outliers <= max(1, floor(0.1 * holdout))`, and p95 of non-outlier
    errors <= 1.5 px and their max <= 3.0 px (else `inaccurate`).
12. Coverage = area of the convex hull of decoded dots in projector px /
    (W_p * H_p). Require >= 0.30 (else `low_coverage`).

Output (JSON-able, same keys in both backends):

```json
{"ok": true, "reason": null, "code": null, "matrix": [[...],[...],[...]],
 "decoded": 58, "total": 60, "fit": 46, "inliers": 46, "holdout": 12,
 "holdout_p95_px": 0.71, "holdout_max_px": 0.94, "outliers": 0,
 "coverage": 0.81, "threshold": 23.4, "gains": [1.01, ...],
 "rejected": {"ambiguous": 0, "parity": 0, "unknown_id": 0, "duplicate": 0, "area": 0},
 "lens": null}
```

The result also carries `"code"`: `null` on success, else the failure code
of the table below. Callers branch on `code`, never on the Spanish text.

`lens` is `null` unless step 10b fitted one: then `{"k", "cx", "cy", "r"}`
(section 1.3.1) and `matrix` maps straightened camera px.

On failure `ok: false`, `matrix: null`, `lens: null`, `reason` one of the Spanish
messages below, all metrics that were computed so far still filled.

Edge rules (decided while implementing; the JS twin mirrors them exactly):

- A bit whose `sum(w * |D_b|)` is 0 has confidence 0 (never NaN), so the
  component is rejected as `ambiguous`.
- Frames present but of different sizes count as missing: failure `frames`.
  A frame that is not a 2-D uint8 array is a programming error (Python
  `ValueError`, JS `Error`).
- If `estimate_homography` raises on the fit set: failure `inconsistent`
  with `inliers: 0`.
- The `{p95:.1f}` of `inaccurate` is the p95 of the finite non-outlier
  holdout errors, or of all finite holdout errors if none is a non-outlier,
  or `0.0` if none is finite. A holdout point mapped to infinity counts as
  an infinite error, hence an outlier.
- Coverage hull: `convexHull` on float32 (`CV_32F`) projector points, then
  `contourArea`.
- A stale pair usually ends in `few_points`; with real noise it can end
  `ok` with a correct H. Parity stops wrong ids in every case.

| code | reason (exact Spanish text) |
|---|---|
| few_points | `La cámara encontró solo {decoded} de los {total} puntos que proyecta la alineación. Mirá en el video que se vea toda la imagen del proyector, que el proyector esté encendido y que no haya luz fuerte sobre la pared.` |
| inconsistent | `La cámara vio puntos que no encajan entre sí, como pasa con reflejos o algo que brilla en la pared. Sacá o tapá lo que brille.` |
| inaccurate | `La alineación salió poco precisa: los contornos caerían corridos unos {p95:.1f} px de los papeles, o más en algunas partes. Suele ser mucha luz sobre la pared o que se movió la cámara o el proyector. Bajá la luz y no toques los equipos.` |
| low_coverage | `La cámara ve solo una parte chica de la imagen del proyector. Movela o alejala para que en el video entre toda la pared proyectada.` |
| frames | `La alineación se cortó antes de terminar: faltan fotos de los puntos. No cambies de ventana mientras alinea.` |

Messages are numbered so tests can assert them; `{p95:.1f}` uses Python
formatting (JS: `pyRound(p95, 1)` formatted with one decimal).

### 1.3.1 Lens model (step 10b; `services/calibration/lens.py`, JS `services/browser/lens.mjs`)

Why (2026-10-21): with an iPhone framing the whole 1920 x 1080 projection,
all 60 dots decode but the edge dots miss a homography by 3-14 px, so the
alignment failed as `inaccurate` every time and the projector drew with no
mapping at all. A wide lens bends straight lines; no homography follows it.

Model, one parameter, centered, in camera px:

    u = c + (p - c) * (1 + k * |p - c|^2 / r^2)

`c = (W_c / 2, H_c / 2)`, `r = sqrt(c_x^2 + c_y^2)` (half the diagonal), so
`k` is the relative stretch at the corners; `|k| <= 0.25`. `undistort(p)` is
that formula; `distort(u)` its inverse by 12 Newton steps on the radius
`rho (1 + k rho^2) = rho_u` (derivative `1 + 3 k rho^2` > 0.25 inside the
frame). A `null` lens is the identity.

Fit: `cost(k)` = sum over fit points, in id order, of `min(e, 5)^2` where
`e` are the reprojection errors of `estimate_homography(undistort(fit_cam),
fit_proj)` (infinite if it raises). `k` = argmin over the grid
`(i - 25) / 25 * 0.25`, i = 0..50 (strictly lower wins, so ties keep the
smaller `k`), refined by 40 golden-section steps inside one grid cell on
each side, clamped to the limit. `k == 0` means no lens. Both backends run
the same operations, so `k` and `matrix` agree bit for bit (parity).

Every camera -> projector mapping of an aligned run is `H(undistort(p))`;
every projector -> camera one is `distort(H^-1(q))`: the board (1.5), the
overlay probes and their hit test (2.3), and the projector drawing.

### 1.4 Backend API (Python `GameController` and JS port, identical)

State key `alignment` (always present):

```json
{"status": "none" | "collecting" | "ready" | "failed" | "stale",
 "layout": null | Layout,
 "matrix": null | [[...],[...],[...]],
 "lens": null | {"k": 0.05, "cx": 640.0, "cy": 360.0, "r": 734.3},
 "camera": null | {"label": "Cámara de iPhone de Tobias", "width": 1280, "height": 720},
 "projector": null | {"width": 1920, "height": 1080},
 "received": 0, "needed": 14,
 "quality": null | {decoded, total, fit, inliers, holdout, holdout_p95_px,
                    holdout_max_px, outliers, coverage},
 "detail": "texto para el operador",
 "code": null | "few_points" | "inconsistent" | "inaccurate" | "low_coverage" | "frames",
 "overlay": {"status": "unchecked" | "ok" | "fill_off", "fill": false, "detail": "...",
             "probes": [{"role": "start", "points": [{"x": 0, "y": 0}, ...]}]}}
```

Numbers in `quality` rounded to 3 decimals; matrix entries unrounded. `code` is the
decoder's failure code while status is `failed` (null otherwise): the panel's
retry branches on it.

Actions (`POST /api/action`):

- `align_start`: requires projector open (409 `Abrí la ventana del proyector con J.`),
  camera frames seen (`frame_width > 0`, 409 `Primero tiene que llegar imagen de la cámara.`),
  no running unpaused attempt (409 `Terminá o pausá el intento antes de alinear.`).
  Sets status `collecting`, `layout = alignment_layout(W_p, H_p)`,
  `received = 0`, clears stored frames, `projection.mode = "align"`, message
  `Alineando cámara y proyector…`. Logs `alignment_started {width, height, dots}`.
- `align_solve`: requires `collecting` (409 `No hay una alineación en curso.`).
  If fewer than 14 distinct frames: failure `frames`. Otherwise decode.
  Success: status `ready`, `matrix`, `camera` (current label and frame
  size), `projector` (W_p, H_p), `quality`, `overlay` reset to
  `{"status": "unchecked", "fill": false}`, `projection.mode = "live"`,
  `lens` (null without step 10b),
  message `Proyector alineado: error {p95:.1f} px con {decoded} puntos.`
  (with a lens: `... puntos (lente de la cámara corregido).`),
  log `alignment_ready` with quality and `lens_k` (6 decimals, or null). Failure: status `failed`,
  `matrix: null`, `detail` and message = reason, `projection.mode = "live"`,
  log `alignment_failed {code, reason, ...metrics}` (metrics include
  `holdout_max_px` and `outliers`, which tell a lens from a moved camera).
  `web.py` also keeps the run's 14 frames as lossless PNGs plus
  `result.json` (layout, result, camera label) in
  `runs/align-failures/<UTC stamp>/`, newest 5 kept; the log's `frames_dir`
  names the folder (null when nothing was saved, e.g. the browser backend),
  so a failure on the real wall can be replayed exactly.
  Frames are freed.
- `align_cancel`: if collecting, status back to `none`, frames freed,
  `projection.mode = "live"`.
- `align_clear`: status `none`, matrix and lens null.

**Chrome's full-screen bar** (2026-10-26). For a few seconds after the
projector window enters full screen, Chrome draws "press Esc to exit full
screen" over the top of the page. Over the pattern it hid the top row of
dots and, fading, corrupted the second: every run on the real wall failed
as `inaccurate` (3.8-4.6 px) and the projector drew unaligned. The saved
frames of those runs, with that band removed, align with the lens model
(k = 0.020, p95 0.5-1.0 px). The projector reports `fullscreenAgeMs` in its
acks and the run's warm-up lasts until `FULLSCREEN_HINT_MS = 5000` after
it went full screen (`align-run.mjs warmupWait`). Chrome shows the bar
again when the mouse reaches the top of that screen: keep it away.

Frames: `POST /api/align_frame`, `Content-Type: image/jpeg`, headers
`X-Postit-Client: 1`, `X-Postit-Align: <bit>:<0|1>` (bit 0..6, inverse
flag), optional `X-Postit-Camera`. 409 `No hay una alineación en curso.` if
not collecting; 400 `Cabecera X-Postit-Align inválida.` if malformed; 400
`El cuadro de alineación no coincide con el tamaño de la cámara.` if its
size differs from the current camera frame size. Stores the grayscale
frame (replacing a previous one with the same key). Returns
`{"received": n, "needed": 14}`. Memory is bounded: at most 14 frames.

Invalidation (status -> `stale`, `matrix: null`, log `alignment_invalidated
{reason}`), only when status is `ready`:
- `projection_open` with a different size than `alignment.projector`
  (`projector_resized`);
- `camera_changed` (`camera_changed`);
- a preview or scan frame whose label differs from `alignment.camera.label`
  (`camera_label`) or whose size differs (`camera_size`).

A check frame whose size differs from `alignment.camera` is refused (400,
`El cuadro de verificación no coincide con el tamaño de la cámara.`). A
`camera_changed` bumps a camera epoch; a preview or scan measured across it
(measured outside the lock, or decoded asynchronously in the browser) is not
stored, like one measured across a palette change (scan: 409 `La cámara
cambió durante la lectura. Volvé a escanear.`).

### 1.5 Game geometry when aligned

When `alignment.status == "ready"`, the live board is mapped into projector
px: `board = transform_board(camera_board, H)` with
`playfield = (0,0),(W_p,0),(W_p,H_p),(0,H_p)`, validated with
`ready_state`. `game.space = "projector"`, `game.width/height = W_p/H_p`,
radius 10 projector px. When not aligned:
today's behavior, `game.space = "camera"`. Snapshot `game` gains the key
`space: "camera" | "projector"`. `camera_board` stays in camera px always.
`board` is in `game.space` px.

**Gravity** (2026-10-21, Tobias: "la pelota tiene que caer para abajo y
tener gravedad"). In either space, with `S = min(game.width, game.height)`:
launch speed `1.7 S` px/s, gravity `1.6 S` px/s^2 toward +y (down the wall),
so a 45 degree shot rises ~0.45 S and lands ~1.8 S away after ~1.5 s. A
bounce keeps 80% of the normal speed; one weaker than 2.5 steps of gravity
rests and slides; an attempt is lost after 20 s.

**Drop, no aim** (2026-10-26, Tobias: "quiero que la pelota CAIGA por la
gravedad, quitale la parte de dirección"). `launch` drops the ball: it
starts at rest straight below the start note's centroid, 2 px under its
lowest edge (`drop_point`; 1 px lower at a time while a slanted edge still
touches it), and only gravity moves it. The player steers by moving the
post-its. The `aim`, `aim_left` and `aim_right` actions, the panel's
direction control and the projector's aim line are gone; the snapshot
`game` keeps `gravity_px_s2` (no `aim_degrees`, no `launch_speed_px_s`).
A ball slower than 5 px/s for 1.5 s has stopped (resting on a flat note or
wedged in a V) and the attempt is lost then. Contacts with several notes
at the same instant are answered together, and a contact the step's
gravity alone brings in is a resting one: before, a V crease let the ball
through one face and a rounded corner made `step` raise. Both backends:
`services/physics/engine.py`, `services/browser/physics.mjs`
(`services.physics.evals.gravity`: a drop follows y0 + g t^2 / 2 to 1e-12
px; 60/60 goals under the start won; 40/40 off-side goals won with one
tilted note; 1095 random ramp drops and 150 drops with bumpers end without
an error).

Physics validation messages reach the operator in Spanish (both backends map
the English `ValueError` texts):

| physics text | operator text |
|---|---|
| `start centroid must fit inside playfield with ball radius` | `El inicio está demasiado cerca del borde del área de juego.` |
| `start overlaps goal` | `El inicio toca la llegada. Separalos.` |
| `start overlaps a bumper` | `El inicio toca un rebote. Separalos.` |
| anything else from physics | `Un post-it detectado no tiene una forma válida.` |

### 1.6 Touch alignment (after NUI Group's CCV / tbeta)

2026-10-27, Tobias: build "otra forma de calibración" like the touch
calibration of NUI Group's TUIO tracker, with a grid of crosses, "porque si
yo tengo la cámara a 2 mts por debajo del proyector, se va a ver distinto".

**Source read.** NUI Group's Community Core Vision (`github.com/nuigroup/ccv15`,
`nuigroup/ccv-tbeta`; `addons/ofxNCore/src/Calibration/Calibration.cpp` and
`CalibrationUtils.cpp`). CCV projects a `GRIDX x GRIDY` cell grid
(`calibration.xml`, 3 x 3 cells = 4 x 4 points by default) inside an
adjustable bounding box. The operator touches the red circle target and
holds; a loader arc grows with the blob's `sitting` time and the blob's
centroid is stored when the finger lifts; `[b]` recaptures the background,
`[r]` goes back a point. A camera point maps to the screen by barycentric
interpolation inside the touched triangle of the mesh (two triangles per
cell); outside the mesh it returns `(0, 0)` (`// FIXME`).

**What we keep.** The row-major grid inset from the edges, touch and hold
with a filling ring, `B` and `R`. The "touch" is a post-it held centered on
the cross: the detector already finds post-its by color, also with room
light on the wall, where the white coded dots of 1.3 lose contrast (the
failed runs of 2026-10-27 in `runs/align-failures/`).

**What we change, and why.** A camera far off the projector's axis sees
each grid cell as a perspective quad, and CCV's mesh is linear inside a
triangle. On a flat wall one homography is exact everywhere, also outside
the grid. `services.calibration.evals.touch` (a physical scene in meters:
projection 2.4 m wide at 3 m, post-its placed within 2 mm, center noise
0.3 camera px, 4 x 4 grid, 40 runs per row; error over the whole projection,
projector px, p95, median of the runs):

| camera | one homography (ours) | CCV mesh |
|---|---|---|
| beside the projector, straight lens | 1.42 | 4.69 |
| beside the projector, barrel lens (k = -0.05) | 2.48 | 4.59 |
| 1 m below, straight lens | 1.55 | 6.83 |
| 1 m below, barrel lens | 2.55 | 7.55 |
| 2 m below, straight lens | 1.60 | 10.98 |
| 2 m below, barrel lens | 2.27 | 11.39 |
| 2 m below and 1 m aside, straight lens | 1.54 | 10.19 |
| 2 m below and 1 m aside, barrel lens | 2.07 | 10.13 |

Every careful run is accepted (320/320), the accepted ones are within 2.95
px p95, a touch 25 mm off is named for a retake or, when it barely bends
the outline, passes accurate (314/320), and touches taken column by column
are refused (10/10). A camera 2 m below is not a problem for a homography:
it is a problem for the mesh. No lens model here: 16 hand-placed touches
cannot fit `k` reliably, and the barrel rows above show what that costs
(about 1 px).

**Real hands (2026-10-27).** The first run on Tobias's wall (FaceTime camera
1280 x 720, projector 1920 x 1080, 4 x 4; `REAL_2026_10_27` in
`services/calibration/tests/test_touch.py`) was refused whole by the first
solver (leave-one-out p95 30 px against a bar of 6), so the outline stayed
on an older alignment. Measured on it: the touches that agree scatter ~4 px
per axis, three (11, 15, 16) are 16-24 px off, and least squares without
those three would have put the outline ~3 px off. The bar had been tuned on
2 mm hands. The eval now has that hand too (`real_hand`: 1920 x 1080 over 2
m, 4 mm per axis, 15 % of touches slipping 15-30 mm, the operator retaking
what the solve names, at most `TOUCH_MAX_ROUNDS = 3` rounds; error over the
whole projection, projector px):

| hand | aligned | first try | retaken per run | p95 of the runs | median |
|---|---|---|---|---|---|
| 2 mm, no slips | 100 % | 96 % | 0.04 | 4.42 | 2.80 |
| 4 mm, no slips (his) | 98 % | 83 % | 0.31 | 7.59 | 5.28 |
| 4 mm, 15 % slips | 84 % | 8 % | 2.55 | 11.02 | 5.67 |
| 10 mm | never accepted | | | | |

A camera bumped 40 px halfway through is never accepted off (40/40). The
floor of a 4 mm hand over 16 touches is ~5.4 px median / ~8.6 p95 whatever
the solver does: the corners of the projection are extrapolated. Two
designs measured worse and were dropped: one fixed px bar for every run
(a 25 mm slip is 13 px at 1280 px but 24 at 1920), and dropping the touches
that disagree (each good touch dropped, a corner above all, costs more than
it saves: 2 mm runs went from 4.4 to 6.5 px p95).

**Grid** (`touch_targets(W_p, H_p, cols, rows)`, Python
`services/calibration/touch.py`, JS `services/browser/touch.mjs`): target
`(i, j)`, row-major, at `((m + (1 - 2m) i / (cols - 1)) W_p, (m + (1 - 2m) j / (rows - 1)) H_p)`
with `m = TOUCH_MARGIN = 0.12`. Default 4 x 4; any grid from 3 x 3 to 8 x 6.

**Hold** (`touch_update(run, centers, camera_size, now)`, on every live
frame, pure): `centers` are the vertex means of every detected note, role
by role. A center within `0.03 min(camera)` of a background center is that
note. With exactly one new note the hold runs: a center farther than
`0.012 min(camera)` from the running mean restarts it. After `TOUCH_HOLD_S
= 1.0` s and at least 3 frames, the mean of the hold is captured at
`touch_step(run)` (the first of `retake` when there are retakes, else the
next new cross; a retake replaces that capture, the others stay), and the
phase is `lift` until no new note is seen (the next cross does not arm while
the same post-it is still on the wall). Two or more new notes: no hold,
`many: true`. `touch_background` makes the next frame's centers the
background; `touch_back` drops the last capture (during retakes it only
restarts the hold). `touch_retake(run, indices)` keeps the phase, sets
`retake` and counts a round.

**Solve** (`solve_touch(camera, targets, W_p, H_p)`, once every cross is
captured). The mapping is the least-squares homography (`findHomography`,
method 0) of every touch: none is ever dropped. A robust fit only says which
touches to take again:

1. Least median of squares (Rousseeuw 1984): every 4-touch subset (when
   there are at most `TOUCH_MAX_SUBSETS = 4096`; else one touch from each
   quarter of the grid, evenly thinned to that many), the exact homography
   through it by Gaussian elimination with partial pivoting in plain
   floats (same bits in JS), and its residuals at every touch; the subset
   with the smallest `(n + 5) // 2`-th squared residual wins, the first on a
   tie. `sigma = sqrt(that) / 1.18` per axis.
2. `TOUCH_REFITS = 2` times: keep the touches within
   `bar = max(TOUCH_OUTLIER_SIGMA * sigma, TOUCH_OUTLIER_MIN_PX)`
   (`4.5`, `8.0` px; at least 9, the closest), least squares over them,
   residuals of every touch against it, `sigma = min(sigma, sqrt(SS / (2m -
   8)))` (never up: slips that got in would raise their own bar).
3. Off: residual over the final bar. `shift_px`: the largest distance, at
   the touches, between the mapping of every touch and the robust one.
4. Pass when `model_rms_px = sqrt(8 SS / ((2n - 8) n))` (the mapping's own
   error at the touches, least squares with 8 unknowns over 2n coordinates)
   is at most `TOUCH_MAX_MODEL_PX = 3.5` and either none is off or
   `shift_px <= TOUCH_MAX_SHIFT_PX = 2.0` (retaking them would not move the
   outline). Otherwise, with none off, the `TOUCH_RETAKE_LOOSE = 2`
   farthest from the robust fit are named; up to half of the touches named
   is `touch_retake` with `retake` (0-based, ascending); more is
   `touch_inaccurate`.

The Jacobian of the mapping at the touches' mean must be positive: the
camera does not mirror the wall, so a negative one means touches in another
order (`touch_invalid`, as fewer than 9 touches or no finite fit). The
result has the decoder's shape (`decoded`, `total`, `fit`, `inliers` = not
off, `holdout = fit`, `holdout_p95_px`, `holdout_max_px` from leave one out,
`outliers` = off, `coverage`) plus `retake`, `fit_rms_px`, `model_rms_px`,
`outlier_bar_px` and `shift_px`. `touch_give_up(result)` turns a
`touch_retake` into `touch_inaccurate` (the backends do it after
`TOUCH_MAX_ROUNDS = 3` rounds).

| code | reason (exact Spanish text) |
|---|---|
| touch_retake | `Algunos toques no coinciden con los demás: repetí {points}. Pegá el post-it centrado en la cruz y sacá la mano.` |
| touch_inaccurate | `Los toques están muy dispersos: el contorno quedaría a unos {error:.1f} px de cada papel. Pegá cada post-it centrado en su cruz y no muevas la cámara ni el proyector.` |
| touch_invalid | `Los toques no forman la grilla de la proyección. Poné el post-it sobre la cruz que marca el círculo, en orden.` |

`{points}` is `points_text`: `el punto 7`, `los puntos 3, 4 y 10` (1-based);
`{error}` is `model_rms_px`.

**Backend** (Python `GameController` and the JS port, identical; golden
scripts `touch_ok`, `touch_slip`, `touch_transposed`, `touch_controls`):

- `touch_start {cols?, rows?}`: 409 without the projector, without live
  notes, during a moving attempt or its end effect; 400 for a grid outside 3
  x 3 .. 8 x 6. The notes now on the wall become the background;
  `alignment.status = "touching"`, `projection.mode = "touch"`, the mapping
  is dropped.
- While touching: every preview frame feeds the hold. `touch_background`,
  `touch_back` (409 when not touching); `align_cancel` ends the run;
  `launch`, `projection_black`, `projection_neutral` and scans are refused
  (409 `Terminá la calibración por toques o cancelala con Esc.`); closing
  the projector cancels it; a camera or size change makes it `stale`; no
  frame for `ALIGN_STALL_S` cancels it.
- The last capture solves. `touch_retake` (before round 3) keeps the run
  going with `touch_retake(run, retake)`: status stays `touching`, the
  projector shows the named crosses one by one and the message is the
  reason. Success adopts the homography exactly as a dot run does (`lens:
  null`, `quality.method = "touch"`, overlay probes, `projection.mode =
  "live"`; detail `Proyector alineado por toques: el contorno cae a unos
  {model_rms_px:.1f} px de cada papel ({fit} toques medidos[, {rounds}
  vuelta(s) de repetición]).`); the panel then runs the overlay check (2.3).
  Failure: `status = "failed"`, `code` from the table. `R` during retakes:
  `Mientras se repiten toques, R no vuelve atrás: repetí el punto {n}.`
- Snapshot: `alignment.touch` is null except while touching:
  `{cols, rows, targets: [{x, y}], step, captured, retake: [0-based],
  rounds, phase: "waiting" | "holding" | "lift", many, progress: 0..1}`
  (coordinates and progress rounded to 3 decimals; `step` is
  `touch_step`). `alignment.quality.method` is `"dots"` or `"touch"`.
- Log: `touch_started`, `touch_captured {step (1-based), x, y, retake}`,
  `touch_background_taken`, `touch_back`, `touch_background`,
  `touch_retake {points (1-based), round, ...}`, `touch_solved {rounds,
  ...}` / `touch_failed {code, rounds, ...}` with `decoded, total,
  holdout_p95_px, holdout_max_px, fit_rms_px, model_rms_px, outlier_bar_px,
  coverage`, `touch_cancelled {reason}`, then `alignment_ready {method:
  "touch"}`. Every capture is logged, so a refused run can be solved again
  from `runs/web-events.jsonl` (that is how `REAL_2026_10_27` was found).

**Projector** (`drawTouchGrid`): black, then the crosses in white and grays
only (no projected stroke may read as a post-it, 2.1): the active one with
arms of `0.1 min(W_p, H_p)` that stick out past a held post-it and a ring of
radius `0.075 min(W_p, H_p)` that fills with `progress` (full in `lift`),
taken ones (below `captured` and not in `retake`) with a small check, the
rest, retakes included, small and dim. The three lines of instructions sit
at the top, or at the bottom while a cross of the upper half is active;
during retakes they read `REPITIENDO · punto {n} · faltan {k}` (`es el
último` for one) and the hint drops `R`.

**Panel**: **Calibrar por toques** (`T`) with the grid (3 x 3, 4 x 4, 5 x 4,
6 x 5); while touching the same button cancels, and `B`, `R`, `Esc` are
CCV's keys (`touchCommand`); the setup line says the point and what to do,
and during retakes `Toques: repetí el punto {n} (faltan {k}). ...`.

## 2. Color under projection

### 2.1 What the projector may emit

Aligned, the projector lights each paper from inside in its role color and
draws nothing on the wall around it. Unaligned it draws only neutral rims.

- **Aligned** (`alignment.status == "ready"` and the projector viewport equals
  `alignment.projector`): each detected note (camera px) is mapped through H.
  - Fill: polygon offset **inward** by `FILL_INSET_PX = 3` projector px,
    `globalAlpha 0.35`, the role's palette hex, skipped if the inset polygon
    degenerates (non-convex or area < 25 px²), no shadow, no blur.
  - Border: the same role color stroked just inside the fill's edge: the
    polygon offset inward by `FILL_INSET_PX + BORDER_WIDTH_PX / 2`, `lineWidth
    BORDER_WIDTH_PX = 4`, `globalAlpha BORDER_ALPHA = 0.35`, miter join, miter
    limit 2, skipped if that polygon degenerates. Its outer edge is the fill's
    edge, so nothing is drawn beyond `FILL_INSET_PX` inside the paper.
  - No rim: no light at all outside the paper.
  - The color is drawn whatever `alignment.overlay` says; the check (2.3) is
    a diagnostic.

  Why (2026-10-21): on Tobias's wall the white rim read as a dark ring
  around each paper and the color was off (the check said `fill_off`).
  Tobias asked for the paper to glow in its color up to the edge with a stronger
  border of the same color, and no rim. The cost is the feedback risk this
  contract was written against: role-colored light that spills on bare wall
  can be detected as a paper. Measured in `services.simulation.feedback_eval`
  with perfect alignment:
  - inset 0-1 px spills: outlines grow 5-7% a frame and keep growing;
  - inset 2 px with the border at alpha 0.9: in a dark room the bright border
    washes the paper's saturation edge out under the high gain, a corner
    lands 18 px off and a removed paper leaves a ghost for 9 frames;
  - inset 3 px with the border at alpha 0.35 (adopted): every case passes,
    outline area within -2.7% to +2.7% of the paper, ghosts 0-2 frames
    (stress: 1).
  A removed paper's color stays until its detection lapses, and it shrinks
  by the inset each frame, so a ghost dies in a few frames. The overlay
  check's fill_off text says this.
- **Not aligned**: rims only, never fills, never a role hue. Each note goes
  through the old contain transform; rim: neutral stroke, `lineWidth 3`,
  along the polygon offset **outward** by `RIM_OFFSET_PX = 6` projector px
  (miter join, miter limit 2, convex hull for a non-convex outline). Color:
  dark theme `#ffffff`, light theme `#2b2f2e`. No shadow, no blur. Each rim
  is clipped out of every *other* note offset outward by `RIM_CLEAR_PX = 3`
  (canvas `clip("evenodd")`), so our white light never lands on another
  paper: two papers a few px apart never cut each other, and two pieces of
  one paper heal on the next frame. A neutral hint `Sin alinear · pulsá A en
  el panel` at the bottom-left in the rim color, 16 px.
- Ball and result text: neutral only (dark theme `#f6fbfc`, light
  theme `#17211f`). The ball is a shaded sphere: a 4 px glow at alpha 0.22,
  then a radial gradient from a highlight up and left (`#ffffff` /
  `#5d6b68`) through the ball color to a shade at the edge (`#a3afb3` /
  `#050807`), all neutral. The won/lost texts (`¡LLEGASTE!`, `FUERA DEL CAMPO`)
  use the same neutral color. Apart from the aligned papers' own color,
  nothing projected uses a role hue.
- The panel's own overlay on the camera video (not projected) keeps colored
  halos; it is drawn by a separate function.
- Exceptions of 3.1, both neutral or while the camera is paused: the ripple
  around a post-it just stuck (neutral, from `RIM_OFFSET_PX` out, every
  paper clipped out like the rims, 750 ms) and the hands-free hint at the
  bottom (neutral text). The countdown, the bounce sparks and the flashes
  of a hit bumper use color only while `game.camera_paused` is true, like
  the drop and goal effects.

### 2.2 Sampling colors and exposure under the play light

New projection mode `neutral`: the projector draws only the theme
background (dark `#000000`, light `#f3f2ec`), no notes, no ball.

**Operator background** (2026-10-26, Tobias: "que yo pueda ir editando el
FONDO manualmente"). The panel's color picker sets any `#rrggbb` as the
projector background (localStorage `postit-wall:projector-backdrop`,
BroadcastChannel `{type: "backdrop", hex | null}`); it replaces the theme
background everywhere the theme one was used, neutral mode included, but
never the pure black of a scan or a pattern. The theme follows its
brightness (`themeForBackdrop`: the higher WCAG contrast of black or
white) so the ball, rims and texts stay readable. A color one of the
palette's rules matches (`backdropConflict`: same OpenCV HSV window,
saturation and value floors as `detector._mask`) is refused: the lit wall
would read as that note. `F`, Oscuro or Claro drop it.

**White edge** (2026-10-26, Tobias: "el reborde negro, que sea con un fill
blanco ... siempre, aunque la detección quede menos precisa"). Aligned, the
strip between the fill's edge and the paper's (the FILL_INSET_PX the inset
leaves unlit, which read as a black border) is stroked white
(`NEUTRAL[theme].edge`, `EDGE_ALPHA = 0.8`, 3 px wide), under the fills and
never past the paper; a paper too small for the fill gets none. Measured in
`services.simulation.feedback_eval`: at 80% every case with room light keeps
the original limits (corners <= 4 px, no flips, no phantoms, no color on
the wall); 100% puts corners 5-11 px off; a white ring past the paper put
them 30-70 px off. In a dark room the lit strip reads as paper at any
strength (wrong-ready frames, corners 40-110 px, color 2-4 px on the wall):
those four scenarios are reported as `known_limits`, not scored. Keep some
room light.

**Camera paused during an attempt; effects** (2026-10-26, Tobias: "cuando
se lance la pelota, que la cámara deje de leer los cuadrados, porque quiero
agregar efectos con colores"). From `launch` until `END_EFFECT_S = 2.5` s
after the attempt ends (won or lost), the backend does not read camera
frames: `preview_jpeg` returns the view frozen at the drop without
detecting, the frozen view never expires, and `game.camera_paused` is
true. A frame from another camera label or size is still read (and still
ends the attempt). `reset` reads the camera again at once; a scan during
the end effect is refused (409 `Esperá a que termine el efecto antes de
escanear.`). While `camera_paused` is true the projector may light the
wall in any color (`services/web/ui/effects.mjs`): on the drop a white
flash over the start paper, two rings and twelve sparks in the start
color (0.7 s); a trail from the start color to the goal color while it
falls; on the goal a pulsing flash over the goal paper, rings, rays and
falling sparks in the role and party colors (160 sparks, 14 turning rays, four rings), and the result text cycling
those colors (2.4 s); out of the field a short gray puff (0.9 s). With
`camera_paused` false nothing colored is drawn outside a paper, as
before. The simulated projector does not draw the effects: the camera is
not read while they play.

**Sounds** (2026-10-26, Tobias: "sonidos al lanzar, cuando rebota, y cuando
llega y gana"). `services/web/ui/sounds.mjs`, Web Audio, no files. The
projector window sees every state (~30/s) and turns consecutive `game`
snapshots into events (`soundEvents`: `launch` on a new attempt, one
`bounce` per new bounce up to 3 per state, `win` / `lost` once); it posts
`{type: "sound", name}` on the BroadcastChannel and the panel plays it,
because Chrome lets a page sound only after a gesture there (the panel's
AudioContext starts on its first key or click). A window that just opened
plays nothing it missed. The panel's **Sonido** button turns them off
(localStorage `postit-wall:sound`).

**Ball before the drop**: not drawn while `game.status == "ready"`; it
appears when it is released.

- Action `projection_neutral` (same 409 rule as `projection_black`), sets
  `projection.mode = "neutral"`. `projection_restore` returns to `live`.
- BroadcastChannel `{type: "neutral", id}` -> projector draws neutral, waits
  two rAF, replies `{type: "neutral_ack", id}`. `{type: "resume"}` ends it.
- The eyedropper (`pickColor`) and the camera search (`tuneCamera`) use
  neutral instead of black. Settle times unchanged.
- `projection_black` remains for compatibility; nothing in the panel uses it.

### 2.3 Overlay check after alignment

After `align_solve` succeeds the panel verifies two things with the camera:
that the colored fill does not change how the papers are detected, and that
the fill's light on bare wall is not itself taken for a post-it. The second
matters in a dark room, where the projector is the only light: there the
fill seen on the wall after a paper is removed was re-detected as that paper
for 4 frames (`services.simulation.feedback_eval`, 2026-10-20).

**Probes.** On success `align_solve` also places one test square per role on
bare wall, stored as `alignment.overlay.probes` (list, role order, possibly
shorter than 4): `[{"role": "start", "points": [{x, y} x 4]}]` in projector
px, clockwise from top-left. Deterministic placement (both backends):

- side `s = max(32, py_round(0.05 * min(W_p, H_p)))`;
- keep-out boxes: every note of the current `live_detection` (all roles).
  The panel pauses previews for the whole run, which outlasts the 1.5 s
  life of a detection, so the backend **holds** it: `align_start`, every
  `align_frame`, a successful `align_solve` and every `check_frame` push the
  hold to now + 20 s (`ALIGN_HOLD_S`); `check_solve` and every drop of the
  alignment release it. Without it (review, 2026-10-21) the probes were
  placed as if the wall were bare, one landed on a paper, and the overlay
  frame drew no fill because there were no notes to fill;
  mapped through H, its axis-aligned bounding box grown by `s` on every side;
- candidate centers `(W_p * i / 10, H_p * j / 10)` for `j = 1..9` (outer),
  `i = 1..9` (inner); square `[cx - s/2, cx + s/2] x [cy - s/2, cy + s/2]`;
- a candidate is free when its square intersects no keep-out box and no
  already chosen probe grown by `s` (boxes intersect when `a.x0 < b.x1 and
  b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1`), and its 4 corners mapped
  through `H^-1` land in `[8, W_c - 9] x [8, H_c - 9]` camera px (a corner H^-1
  cannot map makes the candidate not free);
- keep-out notes are the rounded points of `live_detection` (the snapshot's
  3-decimal values, identical in both backends); a note H cannot map is
  skipped;
- roles in order start, goal, bumper, marker take the first free candidate.

**Sequence.**

1. Projector neutral, capture frame N -> `POST /api/check_frame` with
   `X-Postit-Check: neutral`.
2. `{type: "overlay_test", id}` -> the projector draws the aligned overlay
   with the fill forced on regardless of `overlay.fill`, plus each probe as a
   square filled with its role's palette hex at `globalAlpha 0.35` (no rim),
   acks `overlay_test_ack`; capture frame F -> `X-Postit-Check: overlay`.
3. Action `check_solve` (409 `Primero alineá el proyector.` unless alignment
   is `ready`; 409 `Faltan los cuadros de verificación.` without both frames;
   frames are cleared afterwards). Both frames through `find_notes` with the
   current palette.
   - Probe hits: a note of F (any role) whose centroid (the mean of its
     vertices), mapped through H, lies inside a probe square grown by `s/2` on
     every side (inclusive bounds). A centroid H cannot map is not a hit. Hits are left
     out of the paper matching below.
   - Paper matching: for every role, the notes of N and of F (minus hits) are
     matched greedily by IoU of their convex polygons (`intersectConvexConvex`
     area / union area; greedy = repeatedly take the highest remaining IoU
     pair). The papers pass when every role has the same count in N and F and
     every matched pair has IoU >= 0.85.
   - Pass (no hit and papers pass) -> `overlay = {"status": "ok", "fill": true,
     "detail": "Relleno de color verificado con la cámara."}`.
   - A hit -> `{"status": "fill_off", "fill": false, "detail": "La cámara toma
     el color proyectado sobre la pared por un post-it de {label}. Si sacás
     un papel, su luz puede tardar unos segundos en apagarse."}` (label of the
     first probe hit, in role order).
   - Otherwise -> `{"status": "fill_off", "fill": false, "detail": "El color
     proyectado cambiaba la detección ({label}: {before} → {after}). Si sacás
     un papel, su luz puede tardar unos segundos en apagarse."}` for the
     first failing role (counts in N and F; `{label}: forma` when the counts
     match but IoU fails). Both texts end in `FILL_OFF_FADE`, identical in
     both backends.
   - `{label}` is the role's Spanish label (`ROLE_LABELS`: inicio, llegada,
     rebotes, esquinas).
   - Logs `overlay_check {result: "ok" | "fill_off", fill, probes_hit: [role],
     roles: {role: {neutral, overlay, min_iou}}}` (`min_iou` null without
     pairs).
   `/api/check_frame` returns `{"received": ["neutral", ...]}` (sorted);
   400 `Cabecera X-Postit-Check inválida.` for a bad header value; 409
   `Primero alineá el proyector.` unless alignment is `ready`.

**Diagnostic only** (since 2026-10-21): the projector draws the color when
aligned whatever the result (2.1). `fill` stays in the state for the panel
and the logs. `fill_off` tells the person that the wall or the light can make
a removed paper's color linger on the wall for a moment.

Until the check passes, `overlay = {"status": "unchecked", "fill": false,
"detail": "El relleno de color todavía no se verificó.", "probes": [...]}`.
Before any alignment `probes` is `[]`.

### 2.4 Sub-pixel outlines (vision, both backends)

Measured in `services.simulation.feedback_eval` (wave 1): with no projection
and a perfect alignment, detected outlines are 11-12% smaller than the paper
(20% in a dark room) and their corners land 6-12 projector px off. The mask
boundary sits inside the blurred paper edge and approxPolyDP rounds the
corners. Alignment itself is accurate to ~0.1 px, so the outline is the
limit. Every 4-vertex candidate polygon is therefore refined in
`find_notes` (Python `services/vision/detector.py`, JS `vision.mjs`):

1. `F` = the HSV saturation channel of the frame (the same `cvtColor
   BGR2HSV` the detector already computes) as float64. Pixel centers are at
   integer coordinates; bilinear sampling clamps coordinates to
   `[0, W-1] x [0, H-1]`.
2. Coarse polygon `P` (4 vertices, from approxPolyDP or minAreaRect),
   centroid `c` = mean of the 4 vertices. If any side is shorter than 8 px,
   keep `P` unrefined.
3. For side `k` from `a = P[k]` to `b = P[(k+1) % 4]`: unit outward normal
   `n` = perpendicular to `b - a`, oriented away from `c`.
   `K = clamp(py_round(|b - a| / 4), 5, 25)` samples at
   `t_j = 0.15 + 0.7 * j / (K - 1)`, `p_j = a + t_j (b - a)`.
4. Profile at `p_j + s n` for `s = -6.0, -5.75, ..., 6.0` (49 values).
   Inside level `I` = median of the profile at `s <= -3.0`, outside level `O`
   = median at `s >= 3.0` (numpy median). Skip the sample if `I - O < 20`.
5. Crossing level `m = (I + O) / 2`. Scan consecutive profile pairs with
   `-3.0 <= s_i` and `s_{i+1} <= 3.0` in increasing `s`; the first pair with
   `F(s_i) > m >= F(s_{i+1})` gives `s* = s_i + 0.25 * (F(s_i) - m) /
   (F(s_i) - F(s_{i+1}))`. Skip the sample if there is none. Edge point
   `e_j = p_j + s* n`.
6. A side needs at least 3 edge points, else keep `P` unrefined. Line fit by
   total least squares: mean `(mx, my)`, `sxx, syy, sxy` (mean-centered,
   divided by the count), direction angle `theta = 0.5 * atan2(2 sxy, sxx -
   syy)`, direction `(cos theta, sin theta)`.
7. Refined corner `k` = intersection of the lines of sides `k-1` and `k`.
   If `|cross(d_{k-1}, d_k)| < 0.2` for any corner, keep `P` unrefined.
8. Accept the refined quad only if every refined corner lies within
   `max(4 px, 0.15 * shortest coarse side)` of its coarse corner (measured: a
   noisy coarse corner can be 5-6 px off on a 50 px note while the fitted
   lines are right), it is convex with the same orientation as `P`, and its
   area is within `[0.85, 1.25]` of `P`'s area. Otherwise keep `P`.
9. Second pass: run steps 2-8 again, sampling along the sides of the
   step-8 result `Q` (its own centroid and side lengths in steps 2-3), with
   step 8 still judged against `P` (corners, reach, orientation, area).
   Keep its result if it passes, else `Q`. Where `P` is several px off at
   one end of a side, that end's profiles miss the edge (`I - O` below 20)
   and the line is fitted to half the side; along `Q` the whole side is
   seen. Measured 2026-10-20 (`system_dark_room`, the paper's border ring
   is darker than the fill-lit center): one corner 6.3 px off after one
   pass, 1.6 px after two; a synthetic two-tone note, 1.26 -> 0.87 px.

Everything downstream (centers, boards, previews, overlays, the panel's
halos) uses the refined polygon. Measured by `services.simulation.feedback_eval` with perfect alignment
(2026-10-20): rim corner error 2.1-2.7 px in the dark theme (normal, bright
room, auto exposure, fill on/off), 3.2 px in a very dark room, 4.0 px under
the light theme (3.0 px with no projection at all: the flood itself is the
floor); outline area within 1-4% of the paper (was 11-20% small). Two
alternative features were measured and rejected: alpha between wall and
paper color in linear light (our own white rim pulls the edge out: 7-35 px)
and linear-light chroma (noise at low light: 11-19 px). Saturation is immune
to the neutral rim because white light adds no saturation. A fill inset of
8 px instead of 5 helped the light theme (3.6 px) but hurt the dark room
(4.3 px), so it stayed 5 until the color moved to the paper's edge (2.1:
inset 3, with a border).

### 2.5 The detector discounts the projector's own rims

Measured in a dark room (`system_dark_room`, 2026-10-20): the camera runs
at a high gain, a white rim that lands on a paper saturates it, and a
saturated pixel has no color, so the rim cut the paper in two. The
projector then drew the rim around the detected half, which kept cutting
it: a stable wrong state, seeded by any rim that once crossed a paper (for
example before alignment, when rims land anywhere).

Both backends therefore pass `find_notes` a `bridge` mask: 255 where the
projector draws its own rims in this camera frame, 0 elsewhere. The bridge
joins, it never draws. For every role, with `raw` the role's color mask and
`clean(m) = close(open(m, 3x3), 3x3)` (the usual morphology):

1. The contours are the external contours of `clean(raw)`, as without a
   bridge.
2. `joined = clean(raw | (bridge & dilate(raw, 5x5 ones)))`: a rim pixel
   next to the role's color (within 2 px) counts as that color, so the
   pieces of a paper our rim cut share a component of `joined`
   (`connectedComponents`, 8-connected). Opening and closing are
   increasing, so every contour pixel lies in `joined`.
3. Contours are grouped by the `joined` label of their first point, groups
   in the order of their first contour. A contour alone in its group is
   kept as is. A group with two or more contours that are notes by
   themselves (they pass every candidate filter: area, short side, aspect,
   rectangularity) is kept apart: that is two papers with our rim between
   them. Any other group becomes the `convexHull` of all its points: a
   strip our rim cut off a note (in a dark room, the part of the paper our
   light does not reach) joins it. The usual filters, polygon and
   refinement then run per group.

Joining two notes was the first rule here. The review of 2026-10-21 found
two bumpers 5-8 px apart counting 2, 1, 2, 1 on every frame: joined across
the rim, the next rim went around the hull, they split, and so on. The rim
clipping (2.1) keeps two notes' light off each other instead.

So a rim beside a paper never changes its outline, coarse or refined, and
bare wall never changes. Measured, 2026-10-20: the first version added the
bridge pixels to the mask itself. Where the band reached the paper the
mask grew; where it only grazed the dilation, it left a jagged rim of
pixels, the coarse quad came out up to 7 px off at a corner and the
refinement kept a corner 3.7 px off (`new_aligned_fill_on`, corner 8.7 px
against a 4 px limit). Now the band's margin does not affect any outline
(the same case gives 2.36 px with a margin of 2 or 4).

The footprint (both backends, identical): only while `projection.mode ==
"live"`, the projector is open and alignment is **not** `ready` (aligned, the
projector draws no rim, 2.1, so there is nothing to discount), from the
current `live_detection` (its rounded points) if its frame size equals this
frame's. Mapping camera -> projector: the contain fit the unaligned
projector uses (`scale = min(W_p / W_c, H_p / H_c)`, centered). Each note
goes to projector px, is offset outward by `RIM_OFFSET_PX` with
`offset_convex_polygon` (contracts/geometry.py; miter joins, no limit), and
comes back to camera px; vertices rounded with `floor(v + 0.5)`; drawn as a
closed `polylines` stroke (8-connected) of thickness
`max(3, ceil((RIM_WIDTH_PX + 2 * 4) / stretch))`, where `stretch` is the
projector distance of one camera px to the right of the note's mean vertex.

## 3. Panel and projector protocol

BroadcastChannel `postit-wall` additions:

| from -> to | message | reply |
|---|---|---|
| panel -> projector | `{type:"align_pattern", id, layout, frame: {bit, inverse} or "warmup"}` | `{type:"align_ack", id, width, height, dpr, fullscreen}` after drawing + 2 rAF |
| panel -> projector | `{type:"neutral", id}` | `{type:"neutral_ack", id}` |
| panel -> projector | `{type:"overlay_test", id}` | `{type:"overlay_test_ack", id}` |
| panel -> projector | `{type:"resume"}` (existing) | none: back to state-driven drawing |
| projector -> panel | `{type:"visibility", hidden}` on `visibilitychange` | none |

Panel alignment run (`alignProjector()` in `app.js`):

1. Preconditions: camera streaming, projector ready (`popupReady` and
   `projection.open`), no running attempt, not already aligning. The panel
   pauses its preview loop while aligning.
2. `align_start`. Send warm-up; wait `warmupMs` (1000 automatic camera, 300
   fixed camera) after the ack.
3. For each frame of `layout.frames`: send `align_pattern`, wait for the ack,
   then wait until `settleMs` (350 automatic, 150 fixed) has passed **and**
   at least 2 new camera frames were presented after the ack
   (`video.requestVideoFrameCallback`; fallback: settle time only). Capture
   JPEG quality 0.92 and `POST /api/align_frame`.
4. `{type:"resume"}`, `align_solve`. On `ok` run the overlay check (2.3).
   On failure with reason `inaccurate` or `inconsistent`, retry the whole run
   once with doubled settle times; otherwise show the reason.
5. Automatic start: when the projector becomes ready and the camera streams
   and `alignment.status` is `none` or `stale` (not `failed`, to avoid loops)
   and no attempt runs. Manual start: key `A` or the button `Alinear
   proyector` (id `align-projector`).
6. Panel status line shows `alignment.detail` and the overlay detail. The
   setup guide gains a step `align` between `projector` and `play`.
7. A run owns the camera and the projector until it ends: the eyedropper,
   the color swatches, scanning, the camera search, the camera selector,
   launch, reset, aim, resume and the theme (`F`, in both windows) wait.
   The backend refuses `launch`, `resume`, `projection_black` and
   `projection_neutral` while collecting and keeps `projection.mode` at
   `align` (`Esperá a que termine la alineación.`).
8. Every ack carries the projector's size; one that differs from the
   layout's voids the run (`Cambió el tamaño de la proyección durante la
   alineación.`). After a failed run the panel shows the backend's own
   reason when the backend ended the run (stale, cancelled).
9. The overlay check settles as slowly as the run that succeeded (a slower
   retry doubles both).
10. Leaving the panel mid-run (`pagehide`) resumes the projector and sends
    `align_cancel` (keepalive). A panel that loads while the backend is
    collecting cancels that run. The backend itself cancels a run that has
    received no frame for 20 s (`ALIGN_STALL_S`): the wall never keeps
    patterns.

Projector:

- Draws from local pattern state while in an alignment run; otherwise from
  `state.projection.mode` (`live`, `black`, `neutral`, `align` = black until a
  pattern message arrives).
- If `state.projection.open` is false while the window is alive (backend
  restarted, panel reloaded), it re-sends `projection_open` (at most once per
  second).
- Uses H only when `alignment.status == "ready"` and its viewport equals
  `alignment.projector`; otherwise the unaligned rendering.
- Opens as a named popup again: `window.open("/projector",
  "postit-wall-projector", "popup=yes,width=1280,height=720")`, so a reload
  reuses it and Chrome gives it its own window to move to the projector
  screen. It reports `visibility` changes; the panel warns
  `La ventana de proyección está oculta: movela a su propia ventana en el proyector.`

## 3.1 Hands-free play and wall feedback (package 1, 2026-10-27)

Tobias: "feedback en la pared al pegar post-its, cuenta regresiva, juice de
rebotes y soltar sin teclado". Whoever plays stands at the wall, not at the
Mac. Python `services/web/backend/handsfree.py`, JS
`services/browser/handsfree.mjs`, same operations (parity scripts
`play_countdown`, `cover_gesture`).

**`play {source?: "panel" | "button"}`** (the panel's button, Space and the
remote's button; the gesture calls it with `gesture`): with the game ready,
it starts a countdown of `COUNTDOWN_S = 2.4` s (3 beats); after a won or
lost attempt it first reads the board again (as `reset`, logged
`attempt_reset {source}`) and then counts down. Refused (409) while a run
collects or touches, during a countdown (`La pelota está por salir: esperá
la cuenta o reiniciá con R.`), with an attempt running or paused (`Hay un
intento en marcha.`; the panel resumes a paused one), or without a playable
board. Another source is 400. `launch` keeps dropping at once.

**Countdown.** `phase = "countdown"`, `game.status` stays `ready`,
`game.countdown_s` (seconds left, 3 decimals) and `game.countdown_total_s`.
The camera is not read (`camera_paused` is true: the countdown lights the
wall) and the board is the one at `play`. The first tick or snapshot at or
after the end drops the ball (`attempt_started {attempt, gravity_px_s2,
source}`). `reset` cancels it, so does closing the projector or losing the
board (`attempt_countdown_cancelled {reason}`); `align_start`,
`projection_black` and `projection_neutral` are refused during it.

**Cover gesture** (on every stored live frame, while
`game.hands_free.enabled`, the projector is open, no countdown, no run
collecting or touching and no attempt running): `start_note(notes)` is the
one start note's vertex mean and area. `cover_update` starts a cover when
the start goes missing while seen before; it plays (`fire`) when the start
comes back within `COVER_NEAR_SHARE = 0.05` of the camera's short side of
where it was, after `COVER_MIN_S = 1.0` to `COVER_MAX_S = 4.0` s, unless the
goal went missing `COVER_DARK_FRAMES = 2` frames in a row meanwhile
(somebody in front of the wall). At `COVER_MIN_S` it is `armed`
(`game.hands_free.armed`, message `Sacá la mano del inicio: la pelota sale.`,
log `hands_free_armed`). A start under `COVER_SHRINK = 0.85` of its usual
area (slow average while seen) counts as covered: the projector keeps
lighting the start's place on the hand and the camera reads that ghost as
the start until it shrinks away (1.9 s in the closed loop). A refused play
is logged `hands_free_refused {reason}`. `hands_free {enabled: bool}` turns
it on or off (on by default; log `hands_free`).

`services/web/evals/handsfree.py` (4 frames a second, unevenly; 5 % missed
notes; 2 px and 4 % outline wobble):

| case | result | bound |
|---|---|---|
| hand 1.2-3 s, lifted: plays once | 98.5 % | >= 97 % |
| hand 0.6-1 s (reported) | 43 % | |
| idle wall, one hour of flicker | 0 plays | 0 |
| walker, moved note, note away 5-30 s, swipe 0.1-0.3 s | 0 % each | 0 (swipe <= 50 %) |
| ghost on the hand: hand to "armed", p95 | 1.80 s (3.73 without the shrink check) | <= 2.0 s |

The first bounds were 0.4 s and a goal flicker resetting the cover: 30
false plays an hour on an idle wall and 7 % on a note stuck back later.

**Panel.** The release button and Space send `play` (`launchLabel`: `Soltar
pelota`, `Jugar otra vez` after an attempt, `Saliendo…` during the
countdown); during the countdown only `reset` is enabled. `Soltar tapando
el inicio con la mano (un segundo)` toggles `hands_free`. A Bluetooth remote
is a keyboard with one key: **Asignar botón** learns the next key's
`KeyboardEvent.code` (not a panel shortcut: `remoteKeyAllowed`), kept in
`localStorage` (`postit-wall:remote-key`); that key resumes a paused
attempt or plays (`remoteCommand`).

**Projector** (`effects.mjs`, `live-view.mjs`):

- Ripple: `trackAppearances` keeps the notes seen (camera px) within
  `APPEAR_FORGET_MS = 1500`; a note of a role with none of that role within
  `APPEAR_NEAR_SHARE = 0.04` of the camera's short side is new and ripples
  for `APPEAR_EFFECT_MS = 750`: two neutral outlines growing from
  `RIM_OFFSET_PX` to +60 px out of its convex hull, every paper clipped out.
  The first detection a window sees is what was already on the wall: no
  ripples. Not tracked while the camera is paused.
- Countdown (camera paused only): at the drop point, the beat's number in
  the start color pops, a ring closes on the drop point, the start paper
  flashes white on each beat (`countdownBeat`; 1.6 s of 2.4 is beat 2, not
  3, despite 1.6 / 0.8 = 2.0000000000000004). Sound `tick` per beat, a step
  higher each.
- Bounce: `trackEffects` records a hit when `game.bounces` grows, at the
  ball, with its speed over the last state interval; `drawHitEffect` flashes
  the nearest bumper paper white and bursts a ring and 6-16 sparks in the
  bumper color for `HIT_EFFECT_MS = 450`, bigger for harder hits (speed over
  `sqrt(gravity * height)`); the bounce sound gets louder and higher with it.
- Hint (`handsFreeLine`, neutral, bottom center): `Tapá el inicio con la mano
  un segundo y sacala para soltar` when the game is ready or an attempt
  ended and the camera reads again; `Sacá la mano: ¡la pelota sale!` when
  armed.

### 2.6 Mark colors (2026-10-27)

Tobias: "quiero que debajo de eso, haya para elegir el color que se marca
cuando se Detecta el color/post it". Each role has a mark color apart from
its detection color: the projector fills a detected paper with it (2.1, still
strictly inside the paper), the panel's halo, the countdown and the hit
sparks use it. `palette.roles[role]` gains `mark` (the mark color, else the
detection `hex`) and `mark_custom`; `palette.custom` counts marks too.

- Action `mark_set {role, hex}`: `hex` `#rrggbb` sets it, `null` (or
  missing) goes back to the detection color; an unknown role or a bad hex is
  400. Saved with the palette (`runs/palette.json` key `marks`; the browser
  backend's `postit-wall:palette`); a file without `marks` still loads.
  `palette_reset` clears marks too. Log `mark_changed {role, hex}`.
- A new mark puts the overlay check (2.3) back to `unchecked`: the check
  measured the old color on the wall, and a mark that pushes a paper's look
  toward another role's hue is exactly what it exists to catch. Run A again
  to verify the new color.

## 3.2 Levels (2026-10-27)

Tobias: "Haya un area definida para poner el bloque de inicio, el juego solo
puede comenzar si se coloca el post it sobre ese AREA. Lo mismo que para la
llegada. Ademas que sea por niveles, ejemplo cantidad de post it limitados de
rebotes, paredes, pinchos, trampas". `services/levels` (Python) and
`services/browser/levels.mjs` (port, same operations).

**Data.** `services/levels/levels.json`, one level per line, in shares of
the playfield (x of its width, y of its height), so a level fits any
projector or camera. A level: `id`, `name`, `hint`, `start_zone` and
`goal_zone` (`[x0, y0, x1, y1]`, both or neither), `bumpers_max` (int or
null), `walls`, `spikes` (shapes: `{"rect": [...]}` or `{"points": [[x, y],
...]}` convex; spikes add `facing` for drawing), `portals` (`{entry: shape,
exit: [x, y]}`) and `solution` (bumpers `{center, angle}` that win it, for
the eval). `libre` (no boxes, no limit, nothing projected) is always there
and is the old game exactly.

**Physics** (`services/physics`, both backends). `Board` gains `walls`,
`spikes` and `portals` (default empty: a board without them moves bit for
bit as before). Walls bounce like bumpers and count as a bounce; touching
spikes loses; touching a portal's entry moves the ball to its exit with the
same velocity (a diagonal entry flies on diagonally). Simultaneous contacts:
goal, then spikes, playfield edge, portal, bounce. `GameState.ended_by`:
`goal`, `exit`, `spikes`, `stall`, `timeout` (`""` before the end); the
snapshot carries it as `game.ended_by`. The drop point may not touch a
wall, spikes or a portal entry (`La pelota sale encima de ...`).

**Gating.** In the game's space (projector px aligned, camera px not), for
the live notes: `check.start` / `check.goal` (exactly one such note with its
vertex mean inside its box), `check.bumpers` against `bumpers_max`,
`check.covered` (a note over a wall, spikes or a portal entry, or within
ball radius + 4 px of a portal exit: covering them would be cheating). The
first failure, in this order, is the board's reason and `level.problem`:
`Poné el post-it de inicio dentro de su recuadro.`, `... de llegada ...`,
`Este nivel deja usar N rebote(s): sacá K.` (`se juega sin rebotes` for 0),
`Un post-it tapa {una pared|los pinchos|un portal}: movelo.` With a
problem there is no board: nothing can be played.

**Flow.** `level {id}` picks a level (refused while counting down, in the
air or paused, or during a dot or touch run; 400 for an unknown id); the live notes
are read against it again and a finished attempt is cleared. A won level is
added to `level.done`; `LEVEL_ADVANCE_S = 3.5` s after the win the next
level is selected by itself (`level.advance_s` counts down; `play` or
`reset` before that replays the level instead). After the last level:
`¡Completaste todos los niveles!`. Progress (`level`, `done`) is saved in
`runs/progress.json` (browser: `postit-wall:progress`); `web.py` starts at
the saved level, else level 1, `--level` picks one; the static build reads
`?nivel=`. A new `GameController` without those starts in `libre`. Logs:
`level_selected {level, source}`, `level_won {level, next}`,
`attempt_finished {..., ended_by, level}`.

**Snapshot** `level`: `id`, `name`, `hint`, `number`, `count`, `next`,
`bumpers_max`, `done`, `list` (`{id, name, done}`), `geometry` (`space`,
`width`, `height`, the boxes, walls, spikes with `facing`, portals; null
before there is a space), `check`, `problem`, `advance_s`.

**Wall** (`services/web/ui/level-view.mjs`, 2.1 holds): every shape is the
theme's neutral color, clipped off every detected paper grown by
`RIM_CLEAR_PX`, never in the overlay check's capture. Boxes dashed with
their label above (`INICIO`, `LLEGADA`), solid with a check once met; walls
hatched; spikes as teeth (red only while the camera is paused and the
attempt not paused); portals as an outline with a ring at the exit (blue
only in the air). Bottom right: `NIVEL N · NAME` and `Rebotes k / max`.
Bottom center: `level.problem` instead of the play hint. A level change
shows a banner (`NIVEL N`, name, hint) for `LEVEL_BANNER_MS = 3500`, not
for the level the window opened on. Result texts say why: `¡PINCHOS!`,
`SE QUEDÓ QUIETA`, `SIN TIEMPO`, `FUERA DEL CAMPO`, `¡LLEGASTE!` with
`Sigue el nivel N: name`, or `¡COMPLETASTE TODOS LOS NIVELES!`.

**Panel.** The `Nivel` picker in the Intento section (a check on won
levels), enabled by `controlsFor().level` (the state matrix pins it to the
backend: nothing in the air, not paused, no dot or touch run; it needs neither the
camera nor the projector), and a status line (`Inicio ✓ · Llegada ✗ ·
Rebotes 1 de 2. <problem>`).

**Designing a level.** `python -m services.levels.design <id>` drops the
ball from the zone centers on a 1920 x 1080 wall with 76 mm notes, searches
bumper placements that win (beam search along the ball's path) and ranks
them by robustness; paste the best as `solution`. The eval then holds it.

## 4. Hot path fixes (both backends)

- `frame_quality` no longer runs `detect_board`; it takes `ready` as an
  argument (the live board decides it). Same output keys.
- Python: detection (`find_notes`, `frame_quality`) runs outside the game
  lock; only storing the result takes the lock, so `/api/tick` never waits
  for a detection.
- A frame with a different camera label that discards an attempt logs
  `attempt_discarded {reason: "camera_label"}` and sets the message
  `Cambió la cámara: el intento se descartó.`.

## 5. Simulation (services/simulation)

A deterministic camera/projector/wall model for evals and tests:

- Wall: reflectance image in camera px (gray wall plus convex post-it quads
  with RGB reflectance), plus ambient light RGB.
- Projector: renders the overlay per section 2.1 and patterns per 1.2 in
  projector px (Python re-implementation of the drawing rules with
  `cv2.fillPoly`/`cv2.polylines`, anti-aliased), then light reaches the wall
  through the true projector -> camera homography `G` (`cv2.warpPerspective`),
  with a 3x3 projector color response matrix, a black level, and a Gaussian
  blur.
- Camera: `pixel = gain * reflectance * (ambient + projector_light)`, white
  balance gains, auto exposure (gain moves 50% toward the value that puts the
  frame's mean luminance at a target each frame), sensor noise (seeded),
  gamma 1/2.2, clip, JPEG quality 85.
- Closed loop: snapshot -> render projector -> capture -> real controller
  (`preview_jpeg`, `align_frame`, ...) -> repeat.

## 6. Tests and evals (where each guarantee lives)

- Gate (pre-commit, < 2 s total): small-frame unit tests of layout, code,
  decode (320x240 synthetic), offsets, renderer rules, state transitions.
- Full lane (`scripts/verify.sh`): Python unit tests; `services.calibration.
  evals.coded` (realistic decode: perspective, partial view, blur, JPEG,
  per-frame gain drift, post-its in view, wide and pincushion lenses);
  `services.simulation.eval`
  (closed loop, both themes, outcomes table above); browser parity golden
  (coded decode including lens fits, controller scripts with alignment and
  with a wide lens, overlay check and probes through a lens) replayed in JS;
  existing evals.
- Lens gates: `services/calibration/tests/test_lens.py`,
  `services/browser/tests/lens.test.mjs`, the lens cases of
  `services/web/tests/test_alignment_api.py` (including the regression: the
  same wide-lens run fails with step 10b off) and of
  `services/web/tests/live-projection.test.mjs`.
- Touch alignment (1.6): `services/calibration/evals/touch.py` (ours vs.
  the CCV mesh, acceptance, a slipped touch, wrong order, and `real_hand`:
  careful, 4 mm, 4 mm with slips and sloppy hands with retakes, a bumped
  camera), `services/calibration/tests/test_touch.py` (the solver, retakes
  in the run, and `REAL_2026_10_27`, the first real run, which must ask for
  cross 15 and pass after at most two rounds),
  `services/web/tests/test_touch_api.py` (full runs with a camera below the
  projector, hold, lift, `B`, `R`, cancel, stall, refusals, a slip retaken,
  giving up after three rounds), `services/web/tests/touch-ui.test.mjs`
  (drawing, texts, keys, retakes), the `touching` phase of the state
  matrix, and the `touch` parity cases (solves on 3 x 3 to 8 x 6 grids and
  the real run, the retake trace).
- Hands-free play (3.1): `services/web/tests/test_handsfree.py` (cover
  timing, flicker, walker, moved, ghost; play, countdown, refusals, gesture
  in the backend), `services/web/evals/handsfree.py`,
  `services/web/tests/handsfree-ui.test.mjs` (phase, labels, remote, hint,
  ripple clipped off every paper, hits, countdown, sounds, wiring), the
  `countdown` phase of the state matrix, parity scripts `play_countdown`
  and `cover_gesture`.
- Chrome closed loop (`tests/closed-loop-browser.mjs`, Playwright from
  `POSTIT_PLAYWRIGHT_MODULE`): fake camera composited from a wall image plus
  the projector canvas warped by a known `G`; runs against the static build
  (browser backend) and against `web.py` (Python backend). Rooms `touch`
  (a careful hand: no retake) and `touch-slip` (cross 7 lands 45 px off:
  the panel says `repetí el punto 7`, the hand retakes it, the run aligns).
  Room `handsfree`: a post-it stuck mid-game ripples and is never doubled,
  then a hand covers the start, is lifted, and the ball counts down and
  drops with no key pressed (armed after 1.7 s). Room `levels` (both
  backends): level 1's boxes refuse a start outside them, the papers moved
  in play and win, level 2 follows; level 4's spikes end the bare drop; 8/8
  samples with exactly the papers while boxes, banner and spikes are lit.
  Every room's attempt goes through `play`, so through the countdown.
- Levels (3.2): `services/levels/tests/test_levels.py` (file, geometry,
  checks, every solution wins, the bare drop needs bumpers),
  `services/physics/tests/test_engine.py` `LevelObstacleTests`,
  `services/web/tests/test_levels_api.py` (gating, win and advance, replay,
  spikes, last level, refusals, progress file; marks),
  `services/web/tests/level-view.test.mjs` (drawing rules, texts, banner,
  picker, mark color), `services/levels/evals/solvable.py`, the `level`
  control of the state matrix, parity physics with obstacles and scripts
  `levels`, `level_replay`, `marks`.

Names: nothing new may match `/calibr/i` (existing regression tests forbid
the removed manual calibration); use `align*`.
