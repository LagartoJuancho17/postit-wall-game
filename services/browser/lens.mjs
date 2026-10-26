/**
 * Port of services/calibration/lens.py: the one-parameter radial lens model
 * of the alignment (contract 1.4), operation for operation so both backends
 * pick the same `k` bit for bit.
 *
 *   u = c + (p - c) * (1 + k * |p - c|^2 / r^2)
 *
 * with `c` the camera frame center and `r` half its diagonal. A lens of
 * null is the identity everywhere.
 */

export const LENS_K_LIMIT = 0.25;
const LENS_GRID = 50;
const LENS_REFINE_STEPS = 40;
export const DISTORT_ITERATIONS = 12;
const INV_PHI = (Math.sqrt(5.0) - 1.0) / 2.0;

/** The lens of a `width` x `height` camera frame with stretch `k`. */
export function lensFor(width, height, k) {
  const cx = width / 2;
  const cy = height / 2;
  return { k, cx, cy, r: Math.sqrt(cx * cx + cy * cy) };
}

/** A lens object with finite numbers, positive `r` and |k| within the limit. */
export function validLens(lens) {
  if (!lens || typeof lens !== "object") return false;
  const values = ["k", "cx", "cy", "r"].map((key) => lens[key]);
  return values.every((value) => typeof value === "number" && Number.isFinite(value)) &&
    values[3] > 0 && Math.abs(values[0]) <= LENS_K_LIMIT;
}

/** Camera px -> straightened camera px. Identity for null. */
export function undistort(point, lens) {
  if (!lens) return point;
  const { cx, cy, r } = lens;
  const dx = point.x - cx;
  const dy = point.y - cy;
  const scale = 1.0 + lens.k * ((dx * dx + dy * dy) / (r * r));
  return { x: cx + dx * scale, y: cy + dy * scale };
}

/** Inverse of `undistort` (straightened px -> camera px), by Newton on the radius. */
export function distort(point, lens) {
  if (!lens) return point;
  const { cx, cy, r, k } = lens;
  const ux = point.x - cx;
  const uy = point.y - cy;
  const straight = Math.sqrt(ux * ux + uy * uy) / r;
  if (straight === 0.0) return { x: cx, y: cy };
  let rho = straight;
  for (let step = 0; step < DISTORT_ITERATIONS; step += 1) {
    const slope = 1.0 + 3.0 * k * rho * rho;
    if (slope <= 0.0) break;
    rho = rho - (rho * (1.0 + k * rho * rho) - straight) / slope;
  }
  const ratio = rho / straight;
  return { x: cx + ux * ratio, y: cy + uy * ratio };
}

/**
 * The `k` in [-limit, limit] with the lowest `cost(k)`: a grid 0.01 apart,
 * then golden-section search inside the cells beside the best grid point.
 * Ties keep the earlier `k`.
 */
export function fitLens(cost) {
  let bestK = 0.0;
  let bestCost = Infinity;
  for (let step = 0; step <= LENS_GRID; step += 1) {
    const k = (step - LENS_GRID / 2) / (LENS_GRID / 2) * LENS_K_LIMIT;
    const value = cost(k);
    if (value < bestCost) {
      bestK = k;
      bestCost = value;
    }
  }
  if (!Number.isFinite(bestCost)) return 0.0;
  const cell = LENS_K_LIMIT / (LENS_GRID / 2);
  let low = Math.max(-LENS_K_LIMIT, bestK - cell);
  let high = Math.min(LENS_K_LIMIT, bestK + cell);
  let a = high - INV_PHI * (high - low);
  let b = low + INV_PHI * (high - low);
  let costA = cost(a);
  let costB = cost(b);
  const keep = () => {
    for (const [k, value] of [[a, costA], [b, costB]]) {
      if (value < bestCost) {
        bestK = k;
        bestCost = value;
      }
    }
  };
  for (let step = 0; step < LENS_REFINE_STEPS; step += 1) {
    keep();
    if (costA <= costB) {
      high = b;
      b = a;
      costB = costA;
      a = high - INV_PHI * (high - low);
      costA = cost(a);
    } else {
      low = a;
      a = b;
      costA = costB;
      b = low + INV_PHI * (high - low);
      costB = cost(b);
    }
  }
  keep();
  return bestK;
}

export const undistortAll = (points, lens) => points.map((point) => undistort(point, lens));
