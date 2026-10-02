# Coordinate contract

`services.vision` returns a `Board` in webcam pixels. `services.calibration`
maps every polygon vertex through a 3 × 3 homography and returns a `Board`
in projector pixels. `services.physics` only accepts the mapped board.

Coordinates have their origin at the top left, with x increasing right and y
increasing down. The projector resolution used during calibration must be the
same during play. Marker order is top-left, top-right, bottom-right,
bottom-left in the camera view.
