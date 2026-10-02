# Vision service

`detect_board(frame_bgr, palette=DEFAULT_PALETTE) -> contracts.Board` reads
one BGR, `uint8` webcam frame. Capture it with the projector displaying black
and the post-its stationary. It returns camera-pixel polygons for one start,
one goal, zero or more bumpers and four markers, as colored by `palette`.
The `playfield` is the four marker **centers** (diagonal intersections),
ordered top-left, top-right, bottom-right, bottom-left in the webcam image.
The full marker outlines are also returned in `markers`. The calibration
service maps this board to projector pixels; vision does not modify projector
settings.

`find_notes(frame_bgr, palette)` runs the same filters but requires no
count. It returns every candidate per role, largest first, so a live preview
can show a partial or ambiguous wall.

## Colors

A `contracts.palette.Palette` holds one `ColorRule` per role (`start`,
`goal`, `bumper`, `marker`): a hue band on OpenCV's 0–179 scale (a band with
`hue_low > hue_high` wraps through red), a saturation floor and a value
floor. No two roles may share a hue.

`DEFAULT_PALETTE` keeps the hand-tuned bands this detector always used:
orange goal 5–19, yellow start 20–39, green bumpers 40–90, fuchsia/violet
markers 125–179, with saturation ≥75 and value ≥55.

`build_palette(choices, changed=None)` turns operator-picked colors
(`{"start": "#3a6df0", ...}`; `None` keeps a default) into a palette:

- A picked color becomes a band of ±10 around its hue.
- Its floors are scaled from the sample: saturation `clamp(0.5·S, 45, 120)`
  and value `clamp(0.4·V, 40, 120)`. A pale note still passes, but the white
  wall does not.
- Samples with saturation below 45 (almost gray) or value below 40 (too
  dark) are refused.
- Between a picked role and any other role, every hue goes to the role whose
  color is circularly closer. A hue exactly halfway goes to neither.
- Colors closer than 8 hue units (16°) are refused, with a message that
  names the role the operator just `changed`.
- Two default roles keep their bands.
- The result depends only on the choices, never on the order they were made.

## Filters and errors

The detector uses HSV thresholding per role, 3 px morphological cleanup,
contour area and rectangle-shape filters. A blob must cover at least
`max(80, 0.00012 × frame area)` pixels, so notes need to be visibly separated
and much larger than camera noise. Even lighting and locked camera exposure
improve reliability.

On any missing or duplicate required note, `DetectionError` exposes `role`,
`expected` and `found`, and its Spanish user-facing message names the role
("Post-its de inicio: se esperaba 1; se detectaron 0..."), never a fixed
color. Bumpers may be added or removed **between** attempts, followed by a
new scan.

## Checks

From the project root:

```sh
python -m unittest services.vision.tests.test_detector services.vision.tests.test_palette
python -m services.vision.eval
```

The first command is the local gate suite. `test_palette` covers:

- the hex → HSV conversion against OpenCV;
- bands that wrap through red;
- refusals;
- 400 random palettes, checked for disjoint, contiguous bands that keep their
  own hue;
- detection with picked colors: a recolored wall that the default palette
  misreads, and pastel notes below the default saturation floor.

The eval reports center error across seven synthetic scenes. Four use default
colors under different lighting, perspective and noise. Three repaint the
wall and build the palette by sampling one note per role, as the panel's
eyedropper does. Every case must pass within 6 camera pixels.

These tests do not establish real-camera color accuracy. On a real wall, pick
the colors from the live image and check that the panel's lights match the
notes.
