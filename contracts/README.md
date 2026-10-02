# Coordinate contract

`services.vision` returns a `Board` in webcam pixels. `services.calibration`
maps every polygon vertex through a 3 × 3 homography and returns a `Board`
in projector pixels. `services.physics` only accepts the mapped board.

Coordinates have their origin at the top left, with x increasing right and y
increasing down. The projector resolution used during calibration must be the
same during play. Marker order is top-left, top-right, bottom-right,
bottom-left in the camera view.

# Palette contract

`palette.py` defines which camera colors count as each role: `ROLES`
(`start`, `goal`, `bumper`, `marker`), their Spanish `ROLE_LABELS`, a
`ColorRule` (swatch hex, hue band on OpenCV's 0–179 scale, saturation and
value floors) and a `Palette` with one rule per role. A `Palette` refuses
rules that share a hue. The vision service owns the defaults and how a picked
color becomes a rule; the web backend stores the operator's choices and sends
`Palette.to_json()` to the browser.
