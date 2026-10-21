/**
 * Port of services/calibration/coded.py on opencv.js 4.12: the automatic
 * camera → projector alignment from a binary-coded dot grid (contract
 * docs/alignment-and-color.md sections 1.1-1.3; step numbers are the
 * contract's).
 *
 * Same operations in the same order as the Python module, whose docstring
 * spells out every reduction: sorted medians and percentiles with numpy's
 * formulas, raster-order sequential sums per component, one IEEE operation
 * at a time. Frames are grayscale images `{ width, height, data }` with
 * `data` a Uint8Array in raster order (the panel's JPEG through
 * cvtColor(BGR2GRAY), as in Python).
 *
 * Memory: Python keeps the 7 difference images; here each difference value
 * is recomputed where it is needed (`plain - gain * inverse`, the same two
 * roundings), so a 1280 × 720 run holds one float64 image instead of seven.
 */

import { estimateHomography, transformPoint } from "./calibration.mjs";
import { pyRound } from "./numbers.mjs";

export const ALIGNMENT_VERSION = 1;
export const ALIGNMENT_BITS = 7;
export const ALIGNMENT_MARGIN = 0.06;
/** The 14 frames in contract order: bit 0 plain, bit 0 inverse, bit 1 plain, ... */
export const ALIGNMENT_FRAMES = Object.freeze(Array.from({ length: ALIGNMENT_BITS },
  (_, bit) => [[bit, false], [bit, true]]).flat().map((pair) => Object.freeze(pair)));

const GAIN_STEP = 4;           // step 1
const GAIN_MIN_LEVEL = 8;      // step 1
const THRESHOLD_STEP = 2;      // step 4
const MIN_THRESHOLD = 12.0;    // step 4
const MIN_AREA = 4;            // step 6
const MIN_CONFIDENCE = 0.35;   // step 6
const AREA_FACTOR = 6;         // step 8
const MIN_DECODED = 16;        // step 10
const INLIER_PX = 3.0;         // step 10
const OUTLIER_PX = 5.0;        // step 11
const MIN_HOLDOUT = 4;         // step 11
const MAX_P95_PX = 1.5;        // step 11
const MAX_ERROR_PX = 3.0;      // step 11
const MIN_COVERAGE = 0.30;     // step 12

/** Operator texts, exactly as in the contract table. `code` is the stable key. */
export const ALIGNMENT_REASONS = Object.freeze({
  few_points: ({ decoded, total }) => `La cámara vio solo ${decoded} de ${total} puntos. Revisá que la cámara ` +
    "vea la proyección completa y que el proyector esté encendido.",
  inconsistent: () => "Los puntos detectados no forman una proyección coherente. Evitá " +
    "reflejos y volvé a alinear.",
  inaccurate: ({ p95 }) => `La alineación quedó imprecisa (${pyRound(p95, 1).toFixed(1)} px). Fijá la cámara y el ` +
    "proyector y volvé a alinear.",
  low_coverage: () => "La cámara ve muy poca parte de la proyección. Encuadrala para que vea " +
    "toda la pared.",
  frames: () => "Faltan cuadros del patrón de alineación.",
});

const REJECTION_KEYS = ["ambiguous", "parity", "unknown_id", "duplicate", "area"];

/** The key a frame map uses: `"<bit>:<0|1>"`, like the X-Postit-Align header. */
export const frameKey = (bit, inverse) => `${bit}:${inverse ? 1 : 0}`;

const isNumber = (value) => typeof value === "number";
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
const bitCount = (value) => {
  let count = 0;
  for (let rest = value; rest; rest >>= 1) count += rest & 1;
  return count;
};

/** Dot grid for a projector viewport of `width` × `height` projector px. */
export function alignmentLayout(width, height) {
  if (!isNumber(width) || !isNumber(height)) throw new Error("width and height must be numbers");
  if (!(Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0)) {
    throw new Error("width and height must be positive and finite");
  }
  const aspect = width / height;
  const cols = clamp(pyRound(Math.sqrt(60 * aspect)), 4, 12);
  let rows = clamp(pyRound(60 / cols), 3, 9);
  while (cols * rows > 63) rows -= 1;
  const margin = ALIGNMENT_MARGIN;
  const dots = [];
  for (let j = 0; j < rows; j += 1) {
    for (let i = 0; i < cols; i += 1) {
      dots.push({
        id: j * cols + i + 1,
        x: width * (margin + (1 - 2 * margin) * i / (cols - 1)),
        y: height * (margin + (1 - 2 * margin) * j / (rows - 1)),
      });
    }
  }
  return {
    version: ALIGNMENT_VERSION,
    width,
    height,
    cols,
    rows,
    radius: Math.max(4.0, 0.012 * Math.min(width, height)),
    bits: ALIGNMENT_BITS,
    dots,
    frames: ALIGNMENT_FRAMES.map(([bit, inverse]) => ({ bit, inverse })),
  };
}

/** 7-bit code of a dot: 6 id bits plus even parity in bit 6. */
export function alignmentCode(dotId) {
  if (!Number.isInteger(dotId)) throw new Error("dot id must be an integer");
  if (!(dotId >= 0 && dotId <= 63)) throw new Error("dot id must fit in 6 bits");
  return dotId | ((bitCount(dotId) % 2) << 6);
}

function checkBit(bit) {
  if (!Number.isInteger(bit)) throw new Error("bit must be an integer");
  if (!(bit >= 0 && bit < ALIGNMENT_BITS)) throw new Error("bit must be between 0 and 6");
  return bit;
}

/** Ids of the dots drawn white in pattern frame `{bit, inverse}` (contract 1.2). */
export function patternDots(layout, bit, inverse) {
  const checked = checkBit(bit);
  const lit = inverse ? 0 : 1;
  return layout.dots.filter((dot) => ((alignmentCode(dot.id) >> checked) & 1) === lit).map((dot) => dot.id);
}

/** numpy.median of a sorted, non-empty float64 array. */
export function sortedMedian(ordered) {
  const count = ordered.length;
  const half = Math.floor(count / 2);
  if (count % 2 === 1) return ordered[half];
  return (ordered[half - 1] + ordered[half]) / 2.0;
}

/** numpy.percentile(..., q) ('linear') of a sorted, non-empty array. */
export function sortedPercentile(ordered, q) {
  const count = ordered.length;
  const virtual = (count - 1) * (q / 100.0);
  if (virtual >= count - 1) return ordered[count - 1];
  const lower = Math.floor(virtual);
  const gamma = virtual - lower;
  const below = ordered[lower];
  const above = ordered[lower + 1];
  const difference = above - below;
  if (gamma >= 0.5) return above - difference * (1 - gamma);
  return below + difference * gamma;
}

/** The 14 frames, or null when one is missing or sized differently. */
function collectFrames(frames) {
  const images = new Map();
  let shape = null;
  for (const [bit, inverse] of ALIGNMENT_FRAMES) {
    const image = frames.get(frameKey(bit, inverse));
    if (image === undefined || image === null) return null;
    if (!(image.data instanceof Uint8Array) || !Number.isInteger(image.width) || !Number.isInteger(image.height) ||
        image.width <= 0 || image.height <= 0 || image.data.length !== image.width * image.height) {
      throw new Error("alignment frames must be non-empty 2-D uint8 grayscale images");
    }
    if (shape === null) shape = [image.width, image.height];
    else if (image.width !== shape[0] || image.height !== shape[1]) return null;
    images.set(frameKey(bit, inverse), image);
  }
  return images;
}

/** Steps 1-3: per-bit gains and strength S (the differences are recomputed later). */
function signal(images) {
  const first = images.get(frameKey(0, false));
  const { width, height } = first;
  const size = width * height;
  const gains = [];
  const strength = new Float64Array(size);
  for (let bit = 0; bit < ALIGNMENT_BITS; bit += 1) {
    const plain = images.get(frameKey(bit, false)).data;
    const inverse = images.get(frameKey(bit, true)).data;
    // Step 1: exposure ratio between the two captures, from the wall itself.
    const ratios = [];
    for (let y = 0; y < height; y += GAIN_STEP) {
      for (let x = 0; x < width; x += GAIN_STEP) {
        const p = plain[y * width + x];
        const q = inverse[y * width + x];
        if (p >= GAIN_MIN_LEVEL && q >= GAIN_MIN_LEVEL) ratios.push(p / q);
      }
    }
    const gain = ratios.length ? sortedMedian(Float64Array.from(ratios).sort()) : 1.0;
    gains.push(gain);
    // Steps 2-3: D = P - g * Q, S summed left to right over the bits.
    for (let i = 0; i < size; i += 1) {
      const magnitude = Math.abs(plain[i] - gain * inverse[i]);
      strength[i] = bit === 0 ? magnitude : strength[i] + magnitude;
    }
  }
  for (let i = 0; i < size; i += 1) strength[i] /= ALIGNMENT_BITS;
  return { gains, strength, width, height };
}

/** Step 4: noise floor or 40% of the brightest dots, whichever is higher. */
function threshold(strength, width, height) {
  const sample = [];
  for (let y = 0; y < height; y += THRESHOLD_STEP) {
    for (let x = 0; x < width; x += THRESHOLD_STEP) sample.push(strength[y * width + x]);
  }
  const ordered = Float64Array.from(sample).sort();
  return Math.max(MIN_THRESHOLD, 4.0 * sortedMedian(ordered) + 6.0, 0.4 * sortedPercentile(ordered, 99.9));
}

/** Steps 5-6: bright blobs away from the border, with code and confidence. */
function components(cv, images, gains, strength, width, height, level) {
  const mask = new cv.Mat(height, width, cv.CV_8UC1);
  const labels = new cv.Mat();
  const stats = new cv.Mat();
  const centroids = new cv.Mat();
  try {
    const bits = mask.data;
    for (let i = 0; i < bits.length; i += 1) bits[i] = strength[i] >= level ? 1 : 0;
    const count = cv.connectedComponentsWithStats(mask, labels, stats, centroids, 8, cv.CV_32S);
    const label = labels.data32S;
    const weightSum = new Float64Array(count);
    const xSum = new Float64Array(count);
    const ySum = new Float64Array(count);
    const signed = Array.from({ length: ALIGNMENT_BITS }, () => new Float64Array(count));
    const absolute = Array.from({ length: ALIGNMENT_BITS }, () => new Float64Array(count));
    const planes = Array.from({ length: ALIGNMENT_BITS }, (_, bit) =>
      [images.get(frameKey(bit, false)).data, images.get(frameKey(bit, true)).data]);
    // Raster order: the same sequential sums np.bincount makes.
    for (let index = 0; index < label.length; index += 1) {
      const component = label[index];
      if (component === 0) continue;
      const weight = strength[index];
      weightSum[component] += weight;
      xSum[component] += weight * (index % width);
      ySum[component] += weight * Math.floor(index / width);
      for (let bit = 0; bit < ALIGNMENT_BITS; bit += 1) {
        const [plain, inverse] = planes[bit];
        const value = plain[index] - gains[bit] * inverse[index];
        signed[bit][component] += weight * value;
        absolute[bit][component] += weight * Math.abs(value);
      }
    }
    const found = [];
    for (let component = 1; component < count; component += 1) {
      const left = stats.intAt(component, 0);
      const top = stats.intAt(component, 1);
      const boxWidth = stats.intAt(component, 2);
      const boxHeight = stats.intAt(component, 3);
      const area = stats.intAt(component, 4);
      // Step 6: a dot cut by the frame edge has a biased centroid.
      if (area < MIN_AREA || left <= 0 || top <= 0) continue;
      if (left + boxWidth >= width || top + boxHeight >= height) continue;
      const total = weightSum[component];
      let code = 0;
      const confidences = [];
      for (let bit = 0; bit < ALIGNMENT_BITS; bit += 1) {
        const value = signed[bit][component] / total;
        const spread = absolute[bit][component] / total;
        if (value > 0) code |= 1 << bit;
        confidences.push(spread > 0 ? Math.abs(value) / spread : 0.0);
      }
      found.push({
        x: xSum[component] / total, y: ySum[component] / total, area, left, top,
        width: boxWidth, height: boxHeight, code, confidence: Math.min(...confidences),
      });
    }
    return found;
  } finally {
    [mask, labels, stats, centroids].forEach((mat) => mat.delete());
  }
}

/** Steps 6 (ambiguous), 7 and 8: name components by dot id, drop doubtful ones. */
function identify(found, total) {
  const rejected = Object.fromEntries(REJECTION_KEYS.map((key) => [key, 0]));
  const named = [];
  for (const component of found) {
    if (component.confidence < MIN_CONFIDENCE) {
      rejected.ambiguous += 1;
      continue;
    }
    const { code } = component;
    const dotId = code & 63;
    if (((code >> 6) & 1) !== bitCount(dotId) % 2) {
      rejected.parity += 1;
      continue;
    }
    if (dotId < 1 || dotId > total) {
      rejected.unknown_id += 1;
      continue;
    }
    named.push({ ...component, id: dotId });
  }
  // Step 7: two blobs claiming one id are both suspect.
  const claims = new Map();
  for (const component of named) claims.set(component.id, (claims.get(component.id) ?? 0) + 1);
  const unique = named.filter((component) => claims.get(component.id) === 1);
  rejected.duplicate = named.length - unique.length;
  // Step 8: drop blobs far larger or smaller than a typical dot.
  let kept = unique;
  if (unique.length) {
    const medianArea = sortedMedian(Float64Array.from(unique.map((c) => c.area)).sort());
    kept = unique.filter((c) => !(c.area > AREA_FACTOR * medianArea || c.area * AREA_FACTOR < medianArea));
    rejected.area = unique.length - kept.length;
  }
  kept.sort((a, b) => a.id - b.id);
  return { kept, rejected };
}

/** Per-pair reprojection error; a point H sends to infinity counts as Infinity. */
function errors(camera, projector, matrix) {
  return camera.map((source, index) => {
    try {
      const mapped = transformPoint(source, matrix);
      const dx = mapped.x - projector[index].x;
      const dy = mapped.y - projector[index].y;
      return Math.sqrt(dx * dx + dy * dy);   // as services/calibration reprojection_errors
    } catch {
      return Infinity;
    }
  });
}

function emptyResult(total) {
  return { ok: false, reason: null, code: null, matrix: null, decoded: null, total, fit: null,
    inliers: null, holdout: null, holdout_p95_px: null, holdout_max_px: null, outliers: null,
    coverage: null, threshold: null, gains: null, rejected: null };
}

function fail(result, code, values = {}) {
  result.ok = false;
  result.matrix = null;
  result.code = code;
  result.reason = ALIGNMENT_REASONS[code](values);
  return result;
}

/** Steps 9-12 on identified dots (sorted by id); fills `result`. */
function solve(cv, found, layout, result) {
  const targets = new Map(layout.dots.map((dot) => [dot.id, { x: dot.x, y: dot.y }]));
  // Step 9.
  const fit = found.filter((dot) => dot.id % 5 !== 0);
  const holdout = found.filter((dot) => dot.id % 5 === 0);
  result.decoded = found.length;
  result.fit = fit.length;
  result.holdout = holdout.length;
  const camera = (dots) => dots.map((dot) => ({ x: dot.x, y: dot.y }));
  const projector = (dots) => dots.map((dot) => targets.get(dot.id));
  // Step 10.
  if (found.length < MIN_DECODED) return fail(result, "few_points", { decoded: found.length, total: result.total });
  let matrix;
  try {
    matrix = estimateHomography(cv, camera(fit), projector(fit));
  } catch {
    result.inliers = 0;
    return fail(result, "inconsistent");
  }
  const fitErrors = errors(camera(fit), projector(fit), matrix);
  const inliers = fitErrors.filter((error) => error <= INLIER_PX).length;
  result.inliers = inliers;
  if (inliers < Math.max(10, Math.ceil(0.8 * fit.length))) return fail(result, "inconsistent");
  // Step 11.
  const holdoutErrors = errors(camera(holdout), projector(holdout), matrix);
  const good = Float64Array.from(holdoutErrors.filter((error) => error <= OUTLIER_PX)).sort();
  const outliers = holdoutErrors.length - good.length;
  const p95 = good.length ? sortedPercentile(good, 95) : null;
  const worst = good.length ? good[good.length - 1] : null;
  result.outliers = outliers;
  result.holdout_p95_px = p95;
  result.holdout_max_px = worst;
  if (holdout.length < MIN_HOLDOUT || outliers > Math.max(1, Math.floor(0.1 * holdout.length)) ||
      p95 === null || p95 > MAX_P95_PX || worst > MAX_ERROR_PX) {
    let shown = p95;
    if (shown === null) {
      const finite = Float64Array.from(holdoutErrors.filter(Number.isFinite)).sort();
      shown = finite.length ? sortedPercentile(finite, 95) : 0.0;
    }
    return fail(result, "inaccurate", { p95: shown });
  }
  // Step 12: share of the projector frame the decoded dots span.
  const points = cv.matFromArray(found.length, 1, cv.CV_32FC2, found.flatMap((dot) => {
    const target = targets.get(dot.id);
    return [target.x, target.y];
  }));
  const hull = new cv.Mat();
  let coverage;
  try {
    cv.convexHull(points, hull, false, true);
    coverage = cv.contourArea(hull, false) / (layout.width * layout.height);
  } finally {
    points.delete();
    hull.delete();
  }
  result.coverage = coverage;
  if (coverage < MIN_COVERAGE) return fail(result, "low_coverage");
  result.ok = true;
  result.matrix = matrix.map((row) => [...row]);
  return result;
}

/**
 * Camera → projector homography from the 14 captured pattern frames.
 * `frames` maps `frameKey(bit, inverse)` to a grayscale frame. Returns the
 * contract 1.3 result plus `code` (null on success). Floats are not rounded.
 */
export function decodeAlignment(cv, frames, layout) {
  const total = layout.dots.length;
  const result = emptyResult(total);
  const images = collectFrames(frames);
  if (images === null) return fail(result, "frames");
  const { gains, strength, width, height } = signal(images);
  result.gains = gains;
  const level = threshold(strength, width, height);
  result.threshold = level;
  const found = components(cv, images, gains, strength, width, height, level);
  const { kept, rejected } = identify(found, total);
  result.rejected = rejected;
  return solve(cv, kept, layout, result);
}
