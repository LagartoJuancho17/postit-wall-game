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
| Outline lands on paper | sim eval + Chrome e2e: projected polygon vs true note in projector px | max corner error <= 3 px |
| No feedback | sim eval: static wall, 60 closed-loop frames, both themes | 0 ready/invalid flips, 0 phantom notes, note area growth <= 5% |
| Ghosts die | sim eval: paper removed while projecting | gone within 3 frames |
| Colors picked under play light | sim eval: pick under neutral, detect under live | role counts correct in both themes |
| Real-world flips | `live_board_ready/invalid` count per minute in `runs/web-events.jsonl` | trending to 0 on a static wall |

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
 "rejected": {"ambiguous": 0, "parity": 0, "unknown_id": 0, "duplicate": 0, "area": 0}}
```

The result also carries `"code"`: `null` on success, else the failure code
of the table below. Callers branch on `code`, never on the Spanish text.

On failure `ok: false`, `matrix: null`, `reason` one of the Spanish
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
| few_points | `La cámara vio solo {decoded} de {total} puntos. Revisá que la cámara vea la proyección completa y que el proyector esté encendido.` |
| inconsistent | `Los puntos detectados no forman una proyección coherente. Evitá reflejos y volvé a alinear.` |
| inaccurate | `La alineación quedó imprecisa ({p95:.1f} px). Fijá la cámara y el proyector y volvé a alinear.` |
| low_coverage | `La cámara ve muy poca parte de la proyección. Encuadrala para que vea toda la pared.` |
| frames | `Faltan cuadros del patrón de alineación.` |

Messages are numbered so tests can assert them; `{p95:.1f}` uses Python
formatting (JS: `pyRound(p95, 1)` formatted with one decimal).

### 1.4 Backend API (Python `GameController` and JS port, identical)

State key `alignment` (always present):

```json
{"status": "none" | "collecting" | "ready" | "failed" | "stale",
 "layout": null | Layout,
 "matrix": null | [[...],[...],[...]],
 "camera": null | {"label": "Cámara de iPhone de Tobias", "width": 1280, "height": 720},
 "projector": null | {"width": 1920, "height": 1080},
 "received": 0, "needed": 14,
 "quality": null | {decoded, total, fit, inliers, holdout, holdout_p95_px,
                    holdout_max_px, outliers, coverage},
 "detail": "texto para el operador",
 "overlay": {"status": "unchecked" | "ok" | "fill_off", "fill": false, "detail": "..."}}
```

Numbers in `quality` rounded to 3 decimals; matrix entries unrounded.

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
  message `Proyector alineado: error {p95:.1f} px con {decoded} puntos.`,
  log `alignment_ready` with quality. Failure: status `failed`,
  `matrix: null`, `detail` and message = reason, `projection.mode = "live"`,
  log `alignment_failed {code, reason, ...metrics}`. Frames are freed.
- `align_cancel`: if collecting, status back to `none`, frames freed,
  `projection.mode = "live"`.
- `align_clear`: status `none`, matrix null.

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

### 1.5 Game geometry when aligned

When `alignment.status == "ready"`, the live board is mapped into projector
px: `board = transform_board(camera_board, H)` with
`playfield = (0,0),(W_p,0),(W_p,H_p),(0,H_p)`, validated with
`ready_state`. `game.space = "projector"`, `game.width/height = W_p/H_p`,
speed `0.4 * min(W_p, H_p)` px/s, radius 10 projector px. When not aligned:
today's behavior, `game.space = "camera"`. Snapshot `game` gains the key
`space: "camera" | "projector"`. `camera_board` stays in camera px always.
`board` is in `game.space` px.

Physics validation messages reach the operator in Spanish (both backends map
the English `ValueError` texts):

| physics text | operator text |
|---|---|
| `start centroid must fit inside playfield with ball radius` | `El inicio está demasiado cerca del borde del área de juego.` |
| `start overlaps goal` | `El inicio toca la llegada. Separalos.` |
| `start overlaps a bumper` | `El inicio toca un rebote. Separalos.` |
| anything else from physics | `Un post-it detectado no tiene una forma válida.` |

## 2. Color under projection

### 2.1 What the projector may emit

The projector never puts role-colored light on bare wall.

- **Aligned** (`alignment.status == "ready"` and the projector viewport equals
  `alignment.projector`): each detected note (camera px) is mapped through H.
  - Rim: neutral stroke, `lineWidth 3`, along the polygon offset **outward**
    by `RIM_OFFSET_PX = 5` projector px (miter join, miter limit 2). Color:
    dark theme `#ffffff`, light theme `#2b2f2e`. No shadow, no blur.
  - Fill: only if `alignment.overlay.fill` is true. Role palette hex at
    `globalAlpha 0.35`, polygon offset **inward** by `FILL_INSET_PX = 5`;
    skipped if the inset polygon degenerates (non-convex or area < 25 px²).
    No shadow, no blur. Because the fill is strictly inside the paper, a
    ghost (fill seen on bare wall after a paper is removed) is re-detected
    smaller every frame and disappears.
- **Not aligned**: rims only (neutral, through the old contain transform),
  never fills. A neutral hint `Sin alinear · pulsá A en el panel` at the
  bottom-left in the rim color, 16 px.
- Ball, aim line and result text: neutral only (dark theme `#f6fbfc`, light
  theme `#17211f`). The won/lost texts (`¡LLEGASTE!`, `FUERA DEL CAMPO`)
  use the same neutral color. Nothing projected uses a role hue.
- The panel's own overlay on the camera video (not projected) keeps colored
  halos; it is drawn by a separate function.

### 2.2 Sampling colors and exposure under the play light

New projection mode `neutral`: the projector draws only the theme
background (dark `#000000`, light `#f3f2ec`), no notes, no ball.

- Action `projection_neutral` (same 409 rule as `projection_black`), sets
  `projection.mode = "neutral"`. `projection_restore` returns to `live`.
- BroadcastChannel `{type: "neutral", id}` -> projector draws neutral, waits
  two rAF, replies `{type: "neutral_ack", id}`. `{type: "resume"}` ends it.
- The eyedropper (`pickColor`) and the camera search (`tuneCamera`) use
  neutral instead of black. Settle times unchanged.
- `projection_black` remains for compatibility; nothing in the panel uses it.

### 2.3 Overlay check after alignment

After `align_solve` succeeds the panel verifies that the colored fill does
not change what the camera detects:

1. Projector neutral, capture frame N -> `POST /api/check_frame` with
   `X-Postit-Check: neutral`.
2. Projector live with fill forced on (`{type: "overlay_test", id}` ->
   projector draws the aligned overlay with fill regardless of
   `overlay.fill`, acks `overlay_test_ack`), capture frame F ->
   `X-Postit-Check: overlay`.
3. Action `check_solve`: both frames through `find_notes` with the current
   palette. For every role, notes are matched greedily by IoU of their convex
   polygons (`cv.intersectConvexConvex` area / union area). Pass when every
   role has the same count in both frames and every matched pair has
   IoU >= 0.85. Pass -> `overlay = {"status": "ok", "fill": true, "detail":
   "Relleno de color verificado con la cámara."}`. Fail ->
   `{"status": "fill_off", "fill": false, "detail": "El color proyectado
   cambiaba la detección ({role}: {before} → {after}); se proyectan solo
   bordes."}` (first failing role, counts before/after; when counts match but
   IoU fails, `{role}: forma` instead of counts). Logs `overlay_check
   {result, fill, roles: {role: {neutral, overlay, min_iou}}}`. Requires
   both frames (409 `Faltan los cuadros de verificación.`), clears them.
   `/api/check_frame` returns `{"received": ["neutral", ...]}`; 400 for a
   bad header value (`Cabecera X-Postit-Check inválida.`).

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
  per-frame gain drift, post-its in view); `services.simulation.eval`
  (closed loop, both themes, outcomes table above); browser parity golden
  (coded decode, controller scripts with alignment, overlay check) replayed
  in JS; existing evals.
- Chrome closed loop (`tests/closed-loop-browser.mjs`, Playwright from
  `POSTIT_PLAYWRIGHT_MODULE`): fake camera composited from a wall image plus
  the projector canvas warped by a known `G`; runs against the static build
  (browser backend) and against `web.py` (Python backend).

Names: nothing new may match `/calibr/i` (existing regression tests forbid
the removed manual calibration); use `align*`.
