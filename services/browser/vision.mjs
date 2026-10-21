/**
 * Port of services/vision/detector.py and quality.py on opencv.js.
 *
 * HSV segmentation of a stationary board while the projector is black. The
 * input is a BGR `cv.Mat` (CV_8UC3), like the decoded JPEG Python receives;
 * the returned coordinates are camera pixels. Every OpenCV call is the one
 * the Python code makes, with the same arguments, on the same 4.12 release.
 *
 * Contract docs/alignment-and-color.md 2.4 (sub-pixel outlines, two passes)
 * and 2.5 (our own rims join a paper's pieces, never move an outline) are
 * mirrored operation by operation; the Python module's "parity" comments
 * name every choice that decides the last bit (plain left-to-right sums,
 * sqrt of dx*dx + dy*dy, the 7th smallest of 13 values as the median).
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

// ------------------------------------------- sub-pixel outline refinement

const REFINE_MIN_SIDE_PX = 8.0;      // step 2
const REFINE_MIN_CONTRAST = 20.0;    // step 4
const REFINE_MIN_POINTS = 3;         // step 6
const REFINE_MIN_CROSS = 0.2;        // step 7
const REFINE_MAX_MOVE_PX = 4.0;      // step 8
const REFINE_MAX_MOVE_SHARE = 0.15;  // step 8
const REFINE_AREA_RATIO = [0.85, 1.25];  // step 8
// Step 4: s = -6.0, -5.75, ..., 6.0 (exact binary fractions).
const REFINE_OFFSETS = Float64Array.from({ length: 49 }, (_, index) => -6.0 + 0.25 * index);
const FIRST_PAIR = 12;  // pairs (i, i + 1) with s_i >= -3.0 and s_(i+1) <= 3.0
const LAST_PAIR = 35;
export const REFINE_OUTCOMES = Object.freeze(["refined", "short_side", "few_points", "parallel", "moved", "concave", "area"]);

/**
 * F, the feature image: `{ width, height, at(x, y) }` with integer pixel
 * coordinates. `saturationFeature` reads channel 1 of an HSV Mat in place;
 * tests pass a plain grayscale image.
 */
export function saturationFeature(hsv) {
  const data = hsv.data;
  const width = hsv.cols;
  return { width, height: hsv.rows, at: (x, y) => data[(y * width + x) * 3 + 1] };
}

export function grayFeature({ width, height, data }) {
  return { width, height, at: (x, y) => data[y * width + x] };
}

/** Step 1: F at real coordinates; clamp, floor, lerp in x then y. */
function bilinear(feature, x, y) {
  const cx = Math.min(Math.max(x, 0.0), feature.width - 1.0);
  const cy = Math.min(Math.max(y, 0.0), feature.height - 1.0);
  const x0 = Math.floor(cx);
  const y0 = Math.floor(cy);
  const fx = cx - x0;
  const fy = cy - y0;
  const x1 = Math.min(x0 + 1, feature.width - 1);
  const y1 = Math.min(y0 + 1, feature.height - 1);
  const f00 = feature.at(x0, y0);
  const f01 = feature.at(x1, y0);
  const f10 = feature.at(x0, y1);
  const f11 = feature.at(x1, y1);
  const top = f00 + fx * (f01 - f00);
  const bottom = f10 + fx * (f11 - f10);
  return top + fy * (bottom - top);
}

/** numpy.median of 13 values: the 7th smallest. */
function median13(values) {
  return Float64Array.from(values).sort()[6];
}

function twiceSignedArea(points) {
  let total = 0.0;
  for (let index = 0; index < 4; index += 1) {
    const [x1, y1] = points[index];
    const [x2, y2] = points[(index + 1) % 4];
    total += x1 * y2 - x2 * y1;
  }
  return total;
}

/** Steps 3-5 for the side from `a` to `b`: its sub-pixel edge points. */
function edgePoints(feature, a, b, centroid) {
  const [ax, ay] = a;
  const [bx, by] = b;
  const [cx, cy] = centroid;
  const dx = bx - ax;
  const dy = by - ay;
  const length = Math.sqrt(dx * dx + dy * dy);
  let nx = dy / length;
  let ny = -dx / length;
  if (nx * (ax - cx) + ny * (ay - cy) < 0) {
    nx = -nx;
    ny = -ny;
  }
  const count = Math.min(Math.max(pyRound(length / 4), 5), 25);
  const xs = [];
  const ys = [];
  for (let j = 0; j < count; j += 1) {
    const t = 0.15 + 0.7 * j / (count - 1);
    const px = ax + t * dx;
    const py = ay + t * dy;
    const profile = new Float64Array(49);
    for (let i = 0; i < 49; i += 1) {
      profile[i] = bilinear(feature, px + REFINE_OFFSETS[i] * nx, py + REFINE_OFFSETS[i] * ny);
    }
    const inside = median13(profile.subarray(0, 13));
    const outside = median13(profile.subarray(36, 49));
    if (!(inside - outside >= REFINE_MIN_CONTRAST)) continue;
    const level = (inside + outside) / 2;
    let index = -1;
    for (let i = FIRST_PAIR; i <= LAST_PAIR; i += 1) {
      if (profile[i] > level && level >= profile[i + 1]) {
        index = i;
        break;
      }
    }
    if (index < 0) continue;
    const fi = profile[index];
    const fnext = profile[index + 1];
    const offset = REFINE_OFFSETS[index] + 0.25 * (fi - level) / (fi - fnext);
    xs.push(px + offset * nx);
    ys.push(py + offset * ny);
  }
  return [xs, ys];
}

/** Step 6: total least squares line (mean x, mean y, cos theta, sin theta). */
function fitLine(xs, ys) {
  const count = xs.length;
  let sumX = 0.0;
  for (const x of xs) sumX += x;
  let sumY = 0.0;
  for (const y of ys) sumY += y;
  const meanX = sumX / count;
  const meanY = sumY / count;
  let sxx = 0.0;
  for (const x of xs) sxx += (x - meanX) * (x - meanX);
  let syy = 0.0;
  for (const y of ys) syy += (y - meanY) * (y - meanY);
  let sxy = 0.0;
  for (let i = 0; i < count; i += 1) sxy += (xs[i] - meanX) * (ys[i] - meanY);
  const theta = 0.5 * Math.atan2(2 * (sxy / count), sxx / count - syy / count);
  return [meanX, meanY, Math.cos(theta), Math.sin(theta)];
}

/** Steps 2-8 sampling along `start`, step 8 judged against `reference` (P). */
function refinePass(feature, start, reference) {
  const centroid = [(start[0][0] + start[1][0] + start[2][0] + start[3][0]) / 4,
    (start[0][1] + start[1][1] + start[2][1] + start[3][1]) / 4];
  for (let index = 0; index < 4; index += 1) {
    const [ax, ay] = start[index];
    const [bx, by] = start[(index + 1) % 4];
    if (Math.sqrt((bx - ax) * (bx - ax) + (by - ay) * (by - ay)) < REFINE_MIN_SIDE_PX) return [null, "short_side"];
  }
  const lines = [];
  for (let index = 0; index < 4; index += 1) {
    const [xs, ys] = edgePoints(feature, start[index], start[(index + 1) % 4], centroid);
    if (xs.length < REFINE_MIN_POINTS) return [null, "few_points"];
    lines.push(fitLine(xs, ys));
  }
  const refined = [];
  for (let index = 0; index < 4; index += 1) {
    const [m1x, m1y, d1x, d1y] = lines[(index + 3) % 4];
    const [m2x, m2y, d2x, d2y] = lines[index];
    const cross = d1x * d2y - d1y * d2x;
    if (Math.abs(cross) < REFINE_MIN_CROSS) return [null, "parallel"];
    const u = ((m2x - m1x) * d2y - (m2y - m1y) * d2x) / cross;
    refined.push([m1x + u * d1x, m1y + u * d1y]);
  }
  let shortest = Infinity;
  for (let index = 0; index < 4; index += 1) {
    const [ax, ay] = reference[index];
    const [bx, by] = reference[(index + 1) % 4];
    shortest = Math.min(shortest, Math.sqrt((bx - ax) * (bx - ax) + (by - ay) * (by - ay)));
  }
  const reach = Math.max(REFINE_MAX_MOVE_PX, REFINE_MAX_MOVE_SHARE * shortest);
  for (let index = 0; index < 4; index += 1) {
    const [qx, qy] = refined[index];
    const [px, py] = reference[index];
    if ((qx - px) * (qx - px) + (qy - py) * (qy - py) > reach * reach) return [null, "moved"];
  }
  const coarseArea = twiceSignedArea(reference);
  for (let index = 0; index < 4; index += 1) {
    const [x0, y0] = refined[(index + 3) % 4];
    const [x1, y1] = refined[index];
    const [x2, y2] = refined[(index + 1) % 4];
    const turn = (x1 - x0) * (y2 - y1) - (y1 - y0) * (x2 - x1);
    if (!((coarseArea > 0 && turn > 0) || (coarseArea < 0 && turn < 0))) return [null, "concave"];
  }
  const ratio = twiceSignedArea(refined) / coarseArea;
  if (!(ratio >= REFINE_AREA_RATIO[0] && ratio <= REFINE_AREA_RATIO[1])) return [null, "area"];
  return [refined, "refined"];
}

/**
 * Contract 2.4 steps 2-9: the coarse quad `corners` ([x, y] × 4, pixel
 * centers on integers) moved onto the sub-pixel edges of `feature`.
 * Returns `[quad, outcome]`: the refined corners and "refined", or the
 * coarse corners and why they were kept.
 */
export function refineQuad(feature, corners) {
  if (!Array.isArray(corners) || corners.length !== 4) throw new Error("corners must be 4 points");
  const points = corners.map(([x, y]) => [Number(x), Number(y)]);
  if (points.some(([x, y]) => !Number.isFinite(x) || !Number.isFinite(y))) throw new Error("corners must be finite");
  const [first, outcome] = refinePass(feature, points, points);
  if (first === null) return [points, outcome];
  // Step 9: once more along the first result, still judged against P.
  const [second] = refinePass(feature, first, points);
  return [second ?? first, "refined"];
}

// ------------------------------------------------------------ candidates

const BRIDGE_KERNEL_SIZE = 5;

function morphology(cv, source, operation) {
  const kernel = cv.Mat.ones(3, 3, cv.CV_8U);
  const out = new cv.Mat();
  try {
    cv.morphologyEx(source, out, operation, kernel, new cv.Point(-1, -1), 1, cv.BORDER_CONSTANT,
      cv.morphologyDefaultBorderValue());
    return out;
  } finally {
    kernel.delete();
  }
}

/** close(open(mask, 3x3), 3x3): the usual cleanup. */
function clean(cv, mask) {
  const opened = morphology(cv, mask, cv.MORPH_OPEN);
  try {
    return morphology(cv, opened, cv.MORPH_CLOSE);
  } finally {
    opened.delete();
  }
}

/** The size and shape filters every candidate passes (`_is_note_shape`). */
function isNoteShape(cv, contour, minArea, maxArea) {
  const area = cv.contourArea(contour, false);
  if (area < minArea || area > maxArea) return false;
  const { size } = cv.minAreaRect(contour);
  const shortSide = Math.min(size.width, size.height);
  const longSide = Math.max(size.width, size.height);
  if (shortSide < 5 || longSide / shortSide > 4.0) return false;
  return area / (size.width * size.height) >= 0.55;
}

/**
 * Contract 2.5: the pieces of a paper our own rim cut become one, their
 * convex hull; a component with two or more pieces that are notes by
 * themselves stays apart (two papers with our rim between them), and a
 * contour alone in its group is kept as is. Takes the contour Mats
 * (ownership) and returns Mats the caller deletes.
 */
function joined(cv, contours, raw, bridge, minArea, maxArea) {
  const kernel = cv.Mat.ones(BRIDGE_KERNEL_SIZE, BRIDGE_KERNEL_SIZE, cv.CV_8U);
  const grown = new cv.Mat();
  const labels = new cv.Mat();
  let union = null;
  let cleaned = null;
  try {
    cv.dilate(raw, grown, kernel, new cv.Point(-1, -1), 1, cv.BORDER_CONSTANT, cv.morphologyDefaultBorderValue());
    cv.bitwise_and(grown, bridge, grown);
    union = new cv.Mat();
    cv.bitwise_or(raw, grown, union);
    cleaned = clean(cv, union);
    cv.connectedComponents(cleaned, labels, 8, cv.CV_32S);
    const label = labels.data32S;
    const width = raw.cols;
    const groups = new Map();
    for (const contour of contours) {
      const key = label[contour.data32S[1] * width + contour.data32S[0]];
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(contour);
    }
    const out = [];
    for (const members of groups.values()) {
      const notes = members.filter((member) => isNoteShape(cv, member, minArea, maxArea)).length;
      if (members.length === 1 || notes >= 2) {
        out.push(...members);
        continue;
      }
      const flat = members.flatMap((member) => Array.from(member.data32S));
      const stacked = cv.matFromArray(flat.length / 2, 1, cv.CV_32SC2, flat);
      const hull = new cv.Mat();
      cv.convexHull(stacked, hull, false, true);
      stacked.delete();
      members.forEach((member) => member.delete());
      out.push(hull);
    }
    return out;
  } finally {
    [kernel, grown, labels, union, cleaned].forEach((mat) => mat?.delete());
  }
}

function findCandidates(cv, hsv, rule, feature, bridge = null) {
  const width = hsv.cols;
  const height = hsv.rows;
  const raw = mask(cv, hsv, rule);
  const closed = clean(cv, raw);
  const vector = new cv.MatVector();
  const hierarchy = new cv.Mat();
  let contours = [];
  try {
    // At 1080p the floor is roughly 250 px². This rejects isolated hot
    // pixels without depending on a particular webcam resolution.
    const minArea = Math.max(80.0, width * height * 0.00012);
    // A note is a small part of a wall that also shows the whole field. A
    // bigger blob is a tinted wall or a large object, never a post-it.
    const maxArea = width * height * MAX_NOTE_FRACTION;
    cv.findContours(closed, vector, hierarchy, cv.RETR_EXTERNAL, cv.CHAIN_APPROX_SIMPLE);
    for (let i = 0; i < vector.size(); i += 1) contours.push(vector.get(i));
    if (bridge !== null) contours = joined(cv, contours, raw, bridge, minArea, maxArea);
    const candidates = [];
    for (const contour of contours) {
      if (!isNoteShape(cv, contour, minArea, maxArea)) continue;
      const area = cv.contourArea(contour, false);
      const coarse = polygonFromContour(cv, contour);
      const [quad, outcome] = refineQuad(feature, coarse.map((p) => [p.x, p.y]));
      const polygon = outcome === "refined" ? quad.map(([x, y]) => ({ x, y })) : coarse;
      candidates.push({ polygon, center: quadrilateralCenter(polygon), area, coarse, outcome });
    }
    return candidates.sort((a, b) => b.area - a.area);
  } finally {
    contours.forEach((contour) => contour.delete());
    [raw, closed, vector, hierarchy].forEach((mat) => mat.delete());
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

function candidatesByRole(cv, hsv, palette, bridge = null) {
  if (bridge !== null && (bridge.rows !== hsv.rows || bridge.cols !== hsv.cols || bridge.type() !== cv.CV_8UC1)) {
    throw new Error("bridge debe ser una máscara uint8 del tamaño del cuadro");
  }
  const feature = saturationFeature(hsv);
  return Object.fromEntries(ROLES.map((role) => [role, findCandidates(cv, hsv, palette[role], feature, bridge)]));
}

/** Live web boards use camera pixels; corner colors are optional. */
export function boardFromNotes(notes, width, height) {
  for (const role of ["start", "goal"]) {
    if (notes[role].length !== 1) {
      throw new Error(`Post-its de ${ROLE_LABELS[role]}: se esperaba 1; se detectaron ${notes[role].length}.`);
    }
  }
  return {
    playfield: [{ x: 0, y: 0 }, { x: width, y: 0 },
      { x: width, y: height }, { x: 0, y: height }],
    start: notes.start[0], goal: notes.goal[0],
    bumpers: notes.bumper, markers: notes.marker,
  };
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

/**
 * Every note of each role that passes the shape filters, largest first.
 * `bridge` (CV_8UC1, 255 where the projector draws its own rims) joins the
 * pieces of a paper one of those rims cuts (contract 2.5).
 */
export function findNotes(cv, frame, palette, bridge = null) {
  checkFrame(cv, frame);
  const hsv = toHsv(cv, frame);
  try {
    const found = candidatesByRole(cv, hsv, palette, bridge);
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
 * `bridge` as in findNotes; `ready` as in Python's frame_quality: a caller
 * that already knows (the live controller decides it from its own board)
 * passes it and skips the 4-marker board check.
 */
export function inspectFrame(cv, frame, palette, { bridge = null, ready = null } = {}) {
  checkFrame(cv, frame);
  const hsv = toHsv(cv, frame);
  try {
    const found = candidatesByRole(cv, hsv, palette, bridge);
    const notes = Object.fromEntries(ROLES.map((role) => [role, found[role].map((c) => c.polygon)]));
    let verdict = ready;
    if (verdict === null) {
      verdict = true;
      try {
        boardFrom(cv, found);
      } catch (error) {
        if (!(error instanceof Error)) throw error;
        verdict = false;
      }
    }
    return { notes, quality: quality(cv, frame, hsv, notes, verdict) };
  } finally {
    hsv.delete();
  }
}

/** frame_quality: detection readiness and exposure, color and focus measures. */
export function frameQuality(cv, frame, palette) {
  return inspectFrame(cv, frame, palette).quality;
}
