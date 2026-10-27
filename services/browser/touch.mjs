/**
 * Port of services/calibration/touch.py: the touch-grid alignment (contract
 * 1.6), after NUI Group's Community Core Vision. Same operations in the same
 * order as the Python side, so the backends agree to the bit; see that file
 * for why one homography replaces CCV's triangle mesh.
 */

import { sortedPercentile } from "./alignment.mjs";
import { transformPoint } from "./calibration.mjs";
import { pyRound } from "./numbers.mjs";

export const TOUCH_GRID = Object.freeze([4, 4]);
export const TOUCH_GRID_MIN = Object.freeze([3, 3]);
export const TOUCH_GRID_MAX = Object.freeze([8, 6]);
export const TOUCH_MARGIN = 0.12;
export const TOUCH_HOLD_S = 1.0;
export const TOUCH_MIN_SAMPLES = 3;
export const TOUCH_STILL_SHARE = 0.012;
export const TOUCH_NEW_SHARE = 0.03;
export const TOUCH_MAX_P95_PX = 6.0;
export const TOUCH_MAX_ERROR_PX = 9.0;
export const TOUCH_OUTLIER_PX = 9.0;
export const TOUCH_MIN_POINTS = 9;

export const TOUCH_REASONS = Object.freeze({
  touch_inaccurate: ({ worst, error }) => `Los toques no coinciden entre sí: el punto ${worst} quedó corrido unos ` +
    `${pyRound(error, 1).toFixed(1)} px. Centrá el post-it en cada cruz y no muevas la cámara ni el proyector.`,
  touch_invalid: () => "Los toques no forman la grilla de la proyección. Poné el post-it sobre la cruz que " +
    "marca el círculo, en orden.",
});

export function touchTargets(width, height, cols, rows) {
  const span = 1.0 - 2.0 * TOUCH_MARGIN;
  const targets = [];
  for (let j = 0; j < rows; j += 1) {
    for (let i = 0; i < cols; i += 1) {
      targets.push({ x: (TOUCH_MARGIN + span * i / (cols - 1)) * width, y: (TOUCH_MARGIN + span * j / (rows - 1)) * height });
    }
  }
  return targets;
}

export function validGrid(cols, rows) {
  return Number.isInteger(cols) && Number.isInteger(rows) && cols >= TOUCH_GRID_MIN[0] && cols <= TOUCH_GRID_MAX[0]
    && rows >= TOUCH_GRID_MIN[1] && rows <= TOUCH_GRID_MAX[1];
}

/** Every note's vertex mean, role by role in `roles` order. */
export function noteCenters(notes, roles) {
  const centers = [];
  for (const role of roles) {
    for (const points of notes[role] ?? []) {
      let sx = 0.0;
      let sy = 0.0;
      for (const point of points) {
        sx += point.x;
        sy += point.y;
      }
      centers.push({ x: sx / points.length, y: sy / points.length });
    }
  }
  return centers;
}

function distance(a, b) {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return Math.sqrt(dx * dx + dy * dy);
}

export function newNotes(centers, baseline, [width, height]) {
  const near = TOUCH_NEW_SHARE * Math.min(width, height);
  return centers.filter((c) => baseline.every((b) => distance(c, b) > near));
}

export function touchBegin(width, height, cols, rows, baseline) {
  return { cols, rows, targets: touchTargets(width, height, cols, rows), captured: [], baseline: [...baseline],
    phase: "waiting", rebase: false, sum_x: 0.0, sum_y: 0.0, samples: 0, since: null, many: false };
}

function idle(run, phase) {
  return { ...run, phase, sum_x: 0.0, sum_y: 0.0, samples: 0, since: null };
}

/** One live frame: [run, event], event "background", "captured", "done" or null. */
export function touchUpdate(run, centers, cameraSize, now) {
  if (run.rebase) return [{ ...idle(run, "waiting"), baseline: [...centers], rebase: false, many: false }, "background"];
  const fresh = newNotes(centers, run.baseline, cameraSize);
  run = { ...run, many: fresh.length > 1 };
  if (run.phase === "lift") return [fresh.length ? run : idle(run, "waiting"), null];
  if (fresh.length !== 1) return [idle(run, "waiting"), null];
  const point = fresh[0];
  if (run.samples) {
    const mean = { x: run.sum_x / run.samples, y: run.sum_y / run.samples };
    if (distance(point, mean) > TOUCH_STILL_SHARE * Math.min(...cameraSize)) run = idle(run, "waiting");
  }
  if (!run.samples) run = { ...run, since: now };
  run = { ...run, phase: "holding", sum_x: run.sum_x + point.x, sum_y: run.sum_y + point.y, samples: run.samples + 1 };
  if (now - run.since >= TOUCH_HOLD_S && run.samples >= TOUCH_MIN_SAMPLES) {
    const captured = [...run.captured, { x: run.sum_x / run.samples, y: run.sum_y / run.samples }];
    run = { ...idle(run, "lift"), captured };
    return [run, captured.length === run.targets.length ? "done" : "captured"];
  }
  return [run, null];
}

export function touchBack(run) {
  return { ...idle(run, "waiting"), captured: run.captured.slice(0, -1) };
}

export function touchProgress(run, now) {
  if (run.phase !== "holding" || run.since === null) return 0.0;
  return Math.min(1.0, Math.max(0.0, (now - run.since) / TOUCH_HOLD_S));
}

/** Least squares over every touch (no RANSAC: every touch was confirmed). */
export function fitHomography(cv, camera, projector) {
  const src = cv.matFromArray(camera.length, 1, cv.CV_64FC2, camera.flatMap((p) => [p.x, p.y]));
  const dst = cv.matFromArray(projector.length, 1, cv.CV_64FC2, projector.flatMap((p) => [p.x, p.y]));
  let found = null;
  try {
    try {
      found = cv.findHomography(src, dst, 0);
    } catch {
      return null;
    }
    if (found === null || found.empty() || found.rows !== 3 || found.cols !== 3) return null;
    const m = [0, 1, 2].map((r) => [0, 1, 2].map((c) => found.doubleAt(r, c)));
    if (!m.flat().every(Number.isFinite) || Math.abs(m[2][2]) <= 1e-12) return null;
    return m.map((row) => row.map((value) => value / m[2][2]));
  } finally {
    src.delete();
    dst.delete();
    found?.delete?.();
  }
}

function error(matrix, camera, projector) {
  let mapped;
  try {
    mapped = transformPoint(camera, matrix);
  } catch {
    return Infinity;
  }
  return distance(mapped, projector);
}

function jacobianSign(matrix, camera) {
  let sx = 0.0;
  let sy = 0.0;
  for (const point of camera) {
    sx += point.x;
    sy += point.y;
  }
  const x = sx / camera.length;
  const y = sy / camera.length;
  const [[a, b, c], [d, e, f], [g, h, i]] = matrix;
  const w = g * x + h * y + i;
  const det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g);
  return det / (w * w * w);
}

export function leaveOneOut(cv, camera, projector) {
  return camera.map((_, k) => {
    const matrix = fitHomography(cv, camera.filter((__, i) => i !== k), projector.filter((__, i) => i !== k));
    return matrix === null ? Infinity : error(matrix, camera[k], projector[k]);
  });
}

function evaluate(cv, camera, projector) {
  const errors = leaveOneOut(cv, camera, projector);
  const finite = errors.filter(Number.isFinite).sort((p, q) => p - q);
  const p95 = finite.length === errors.length ? sortedPercentile(finite, 95) : Infinity;
  let worst = 0;
  errors.forEach((e, k) => {
    if (e > errors[worst]) worst = k;
  });
  return { errors, p95, max: errors[worst], worst };
}

/** The homography from the touches, or why not (contract 1.6). */
export function solveTouch(cv, camera, targets, width, height) {
  const total = targets.length;
  const result = { ok: false, code: null, reason: null, matrix: null, decoded: camera.length, total,
    fit: null, inliers: null, holdout: null, holdout_p95_px: null, holdout_max_px: null,
    outliers: null, coverage: null, dropped: null, fit_rms_px: null };
  const fail = (code, values = {}) => Object.assign(result, { ok: false, code, reason: TOUCH_REASONS[code](values), matrix: null });

  let kept = camera.map((_, i) => i);
  let measured = camera.length >= TOUCH_MIN_POINTS ? evaluate(cv, camera, targets) : null;
  if (measured === null || !Number.isFinite(measured.p95)) return fail("touch_invalid");
  if (measured.max > TOUCH_OUTLIER_PX && camera.length - 1 >= TOUCH_MIN_POINTS) {
    const k = measured.worst;
    const rest = kept.filter((i) => i !== k);
    const again = evaluate(cv, rest.map((i) => camera[i]), rest.map((i) => targets[i]));
    if (again.p95 <= TOUCH_MAX_P95_PX && again.max <= TOUCH_MAX_ERROR_PX) {
      kept = rest;
      measured = again;
      result.dropped = k + 1;
    }
  }
  const cam = kept.map((i) => camera[i]);
  const proj = kept.map((i) => targets[i]);
  const matrix = fitHomography(cv, cam, proj);
  Object.assign(result, { fit: cam.length, holdout: cam.length, holdout_p95_px: measured.p95, holdout_max_px: measured.max });
  if (matrix === null) return fail("touch_invalid");
  if (jacobianSign(matrix, cam) <= 0) return fail("touch_invalid");
  const fitErrors = cam.map((c, i) => error(matrix, c, proj[i]));
  let totalSq = 0.0;
  for (const e of fitErrors) totalSq += e * e;
  result.fit_rms_px = Math.sqrt(totalSq / fitErrors.length);
  result.inliers = fitErrors.filter((e) => e <= TOUCH_OUTLIER_PX).length;
  result.outliers = camera.length - cam.length;
  const xs = proj.map((p) => p.x);
  const ys = proj.map((p) => p.y);
  result.coverage = (Math.max(...xs) - Math.min(...xs)) * (Math.max(...ys) - Math.min(...ys)) / (width * height);
  if (measured.p95 > TOUCH_MAX_P95_PX || measured.max > TOUCH_MAX_ERROR_PX) {
    return fail("touch_inaccurate", { worst: kept[measured.worst] + 1, error: measured.max });
  }
  return Object.assign(result, { ok: true, matrix });
}
