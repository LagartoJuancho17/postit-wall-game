/**
 * Port of services/vision/detector.py and quality.py on opencv.js.
 *
 * HSV segmentation of a stationary board while the projector is black. The
 * input is a BGR `cv.Mat` (CV_8UC3), like the decoded JPEG Python receives;
 * the returned coordinates are camera pixels. Every OpenCV call is the one
 * the Python code makes, with the same arguments, on the same 4.12 release.
 */

import { quadrilateralCenter } from "./calibration.mjs";
import { pyRound } from "./numbers.mjs";
import { ROLE_LABELS, ROLES } from "./palette.mjs";

/** How many notes of each role a playable board needs; bumpers are optional. */
export const EXPECTED_NOTES = Object.freeze({ start: 1, goal: 1, bumper: null, marker: 4 });
const MAX_NOTE_FRACTION = 0.05; // of the frame area
const CLIPPED_LEVEL = 250; // any channel this bright has lost its color
const DARK_LEVEL = 25; // every channel this dark is mostly sensor noise

/** The frame does not contain an unambiguous playable board. */
export class DetectionError extends Error {
  constructor(role, expected, found, { shape = false } = {}) {
    const label = ROLE_LABELS[role];
    let message;
    if (shape) {
      message = `Los post-its de ${label} no forman un cuadrilátero. ` +
        "Separalos hacia las cuatro esquinas del campo y volvé a escanear.";
    } else {
      const verb = expected === 1 ? "se esperaba" : "se esperaban";
      message = `Post-its de ${label}: ${verb} ${expected}; se detectaron ${found}. ` +
        `Apagá la proyección, revisá la luz y el color elegido para ${label}, y volvé a escanear.`;
    }
    super(message);
    this.name = "DetectionError";
    Object.assign(this, { role, expected, found });
  }
}

function checkFrame(cv, frame) {
  if (!frame || typeof frame.type !== "function" || frame.type() !== cv.CV_8UC3) {
    throw new Error("frame_bgr debe tener dtype uint8 y tres canales BGR");
  }
  if (Math.min(frame.rows, frame.cols) < 20) throw new Error("La imagen de la webcam es demasiado pequeña");
}

function mask(cv, hsv, rule) {
  const out = new cv.Mat(hsv.rows, hsv.cols, cv.CV_8UC1);
  const source = hsv.data;
  const target = out.data;
  const { hue_low: low, hue_high: high, min_saturation: floorS, min_value: floorV } = rule;
  const wraps = low > high;
  for (let i = 0, j = 0; i < target.length; i += 1, j += 3) {
    const hue = source[j];
    const inBand = wraps ? hue >= low || hue <= high : hue >= low && hue <= high;
    target[i] = inBand && source[j + 1] >= floorS && source[j + 2] >= floorV ? 255 : 0;
  }
  return out;
}

function polygonFromContour(cv, contour) {
  const perimeter = cv.arcLength(contour, true);
  const approx = new cv.Mat();
  try {
    cv.approxPolyDP(contour, approx, 0.025 * perimeter, true);
    if (approx.rows === 4 && cv.isContourConvex(approx)) {
      const data = approx.data32S;
      return [0, 1, 2, 3].map((i) => ({ x: data[2 * i], y: data[2 * i + 1] }));
    }
  } finally {
    approx.delete();
  }
  return cv.RotatedRect.points(cv.minAreaRect(contour)).map((p) => ({ x: p.x, y: p.y }));
}

function findCandidates(cv, hsv, rule) {
  const width = hsv.cols;
  const height = hsv.rows;
  const binary = mask(cv, hsv, rule);
  const opened = new cv.Mat();
  const closed = new cv.Mat();
  const kernel = cv.Mat.ones(3, 3, cv.CV_8U);
  const contours = new cv.MatVector();
  const hierarchy = new cv.Mat();
  const anchor = new cv.Point(-1, -1);
  const border = cv.morphologyDefaultBorderValue();
  try {
    cv.morphologyEx(binary, opened, cv.MORPH_OPEN, kernel, anchor, 1, cv.BORDER_CONSTANT, border);
    cv.morphologyEx(opened, closed, cv.MORPH_CLOSE, kernel, anchor, 1, cv.BORDER_CONSTANT, border);
    // At 1080p the floor is roughly 250 px². This rejects isolated hot
    // pixels without depending on a particular webcam resolution.
    const minArea = Math.max(80.0, width * height * 0.00012);
    // A note is a small part of a wall that also shows the whole field. A
    // bigger blob is a tinted wall or a large object, never a post-it.
    const maxArea = width * height * MAX_NOTE_FRACTION;
    cv.findContours(closed, contours, hierarchy, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);
    const candidates = [];
    for (let i = 0; i < contours.size(); i += 1) {
      const contour = contours.get(i);
      try {
        const area = cv.contourArea(contour, false);
        if (area < minArea || area > maxArea) continue;
        const { size } = cv.minAreaRect(contour);
        const shortSide = Math.min(size.width, size.height);
        const longSide = Math.max(size.width, size.height);
        if (shortSide < 5 || longSide / shortSide > 4.0) continue;
        if (area / (size.width * size.height) < 0.55) continue;
        const polygon = polygonFromContour(cv, contour);
        candidates.push({ polygon, center: quadrilateralCenter(polygon), area });
      } finally {
        contour.delete();
      }
    }
    return candidates.sort((a, b) => b.area - a.area);
  } finally {
    [binary, opened, closed, kernel, contours, hierarchy].forEach((mat) => mat.delete());
  }
}

/** Order four marker centers clockwise, beginning at camera top-left. */
function orderedMarkers(cv, markers) {
  const flat = markers.flatMap((m) => [m.center.x, m.center.y]);
  const centers = cv.matFromArray(4, 1, cv.CV_32FC2, flat);
  const hull = new cv.Mat();
  try {
    cv.convexHull(centers, hull, false, true);
    if (hull.rows !== 4 || cv.contourArea(hull, false) < 100) {
      throw new DetectionError("marker", 4, hull.rows, { shape: true });
    }
  } finally {
    centers.delete();
    hull.delete();
  }
  const points = markers.map((m) => ({ x: Math.fround(m.center.x), y: Math.fround(m.center.y) }));
  const cx = points.reduce((sum, p) => sum + p.x, 0) / 4;
  const cy = points.reduce((sum, p) => sum + p.y, 0) / 4;
  let indices = [0, 1, 2, 3].sort((a, b) =>
    Math.atan2(points[a].y - cy, points[a].x - cx) - Math.atan2(points[b].y - cy, points[b].x - cx));
  let start = 0;
  for (let offset = 1; offset < 4; offset += 1) {
    const p = points[indices[offset]];
    const best = points[indices[start]];
    if (p.x + p.y < best.x + best.y) start = offset;
  }
  indices = [...indices.slice(start), ...indices.slice(0, start)];
  return indices.map((index) => markers[index]);
}

function toHsv(cv, frame) {
  const hsv = new cv.Mat();
  cv.cvtColor(frame, hsv, cv.COLOR_BGR2HSV);
  return hsv;
}

function candidatesByRole(cv, hsv, palette) {
  return Object.fromEntries(ROLES.map((role) => [role, findCandidates(cv, hsv, palette[role])]));
}

function boardFrom(cv, found) {
  for (const role of ["marker", "start", "goal"]) {
    if (found[role].length !== EXPECTED_NOTES[role]) {
      throw new DetectionError(role, EXPECTED_NOTES[role], found[role].length);
    }
  }
  const ordered = orderedMarkers(cv, found.marker);
  return {
    playfield: ordered.map((marker) => marker.center),
    start: found.start[0].polygon,
    goal: found.goal[0].polygon,
    bumpers: found.bumper.map((candidate) => candidate.polygon),
    markers: ordered.map((marker) => marker.polygon),
  };
}

/** Every note of each role that passes the shape filters, largest first. */
export function findNotes(cv, frame, palette) {
  checkFrame(cv, frame);
  const hsv = toHsv(cv, frame);
  try {
    const found = candidatesByRole(cv, hsv, palette);
    return Object.fromEntries(ROLES.map((role) => [role, found[role].map((c) => c.polygon)]));
  } finally {
    hsv.delete();
  }
}

/**
 * The board in a projector-dark frame: four markers, one start and one goal
 * are required; bumpers are optional. Throws DetectionError when a required
 * role is missing or ambiguous. `playfield` is the marker centers in camera
 * order: top-left, top-right, bottom-right, bottom-left.
 */
export function detectBoard(cv, frame, palette) {
  checkFrame(cv, frame);
  const hsv = toHsv(cv, frame);
  try {
    return boardFrom(cv, candidatesByRole(cv, hsv, palette));
  } finally {
    hsv.delete();
  }
}

function quality(cv, frame, hsv, notes, ready) {
  const pixels = frame.rows * frame.cols;
  const data = frame.data;
  const histogram = new Uint32Array(256);
  let clipped = 0;
  let dark = 0;
  for (let i = 0, j = 0; i < pixels; i += 1, j += 3) {
    const brightest = Math.max(data[j], data[j + 1], data[j + 2]);
    histogram[brightest] += 1;
    if (brightest >= CLIPPED_LEVEL) clipped += 1;
    if (brightest < DARK_LEVEL) dark += 1;
  }
  const valueAt = (rank) => {
    let seen = 0;
    for (let value = 0; value < 256; value += 1) {
      seen += histogram[value];
      if (seen > rank) return value;
    }
    return 255;
  };
  const median = pixels % 2 ? valueAt((pixels - 1) / 2) : (valueAt(pixels / 2 - 1) + valueAt(pixels / 2)) / 2;

  const gray = new cv.Mat();
  const blurred = new cv.Mat();
  const laplacian = new cv.Mat();
  const shapes = new cv.MatVector();
  const fill = new cv.Mat();
  let sharpness;
  let noteSaturation = null;
  try {
    cv.cvtColor(frame, gray, cv.COLOR_BGR2GRAY);
    cv.GaussianBlur(gray, blurred, new cv.Size(3, 3), 0, 0, cv.BORDER_DEFAULT);
    cv.Laplacian(blurred, laplacian, cv.CV_64F, 1, 1, 0, cv.BORDER_DEFAULT);
    const values = laplacian.data64F;
    let mean = 0;
    for (let i = 0; i < values.length; i += 1) mean += values[i];
    mean /= values.length;
    let variance = 0;
    for (let i = 0; i < values.length; i += 1) variance += (values[i] - mean) ** 2;
    sharpness = variance / values.length;

    const polygons = ROLES.flatMap((role) => notes[role]);
    if (polygons.length) {
      for (const polygon of polygons) {
        const shape = cv.matFromArray(polygon.length, 1, cv.CV_32SC2,
          polygon.flatMap((p) => [Math.trunc(p.x), Math.trunc(p.y)]));
        shapes.push_back(shape);
        shape.delete();
      }
      fill.create(gray.rows, gray.cols, cv.CV_8UC1);
      fill.setTo(new cv.Scalar(0));
      cv.fillPoly(fill, shapes, new cv.Scalar(255), cv.LINE_8, 0, new cv.Point(0, 0));
      const inside = fill.data;
      const hsvData = hsv.data;
      let total = 0;
      let count = 0;
      for (let i = 0; i < inside.length; i += 1) {
        if (inside[i] > 0) {
          total += hsvData[3 * i + 1];
          count += 1;
        }
      }
      noteSaturation = pyRound(total / count, 1);
    }
  } finally {
    [gray, blurred, laplacian, shapes, fill].forEach((mat) => mat.delete());
  }
  return {
    ready,
    clipped: pyRound(clipped / pixels, 4),
    dark: pyRound(dark / pixels, 4),
    brightness: pyRound(median / 255, 3),
    sharpness: pyRound(sharpness, 2),
    note_saturation: noteSaturation,
  };
}

/**
 * One pass for the live preview: the notes (no count required), whether a
 * scan of this frame would succeed, and the exposure, color and focus
 * measures. Same results as find_notes + frame_quality, one HSV conversion.
 */
export function inspectFrame(cv, frame, palette) {
  checkFrame(cv, frame);
  const hsv = toHsv(cv, frame);
  try {
    const found = candidatesByRole(cv, hsv, palette);
    const notes = Object.fromEntries(ROLES.map((role) => [role, found[role].map((c) => c.polygon)]));
    let ready = true;
    try {
      boardFrom(cv, found);
    } catch (error) {
      if (!(error instanceof Error)) throw error;
      ready = false;
    }
    return { notes, quality: quality(cv, frame, hsv, notes, ready) };
  } finally {
    hsv.delete();
  }
}

/** frame_quality: detection readiness and exposure, color and focus measures. */
export function frameQuality(cv, frame, palette) {
  return inspectFrame(cv, frame, palette).quality;
}
