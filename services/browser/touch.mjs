/**
 * Port of services/calibration/touch.py: the touch-grid alignment (contract
 * 1.6), after NUI Group's Community Core Vision. Same operations in the same
 * order as the Python side, so the backends agree to the bit; see that file
 * for why one homography replaces CCV's triangle mesh and why a robust fit
 * names touches to take again instead of dropping them.
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
export const TOUCH_OUTLIER_SIGMA = 4.5;
export const TOUCH_OUTLIER_MIN_PX = 8.0;
export const TOUCH_MAX_MODEL_PX = 3.5;
export const TOUCH_MIN_POINTS = 9;
export const TOUCH_MAX_SUBSETS = 4096;
export const TOUCH_REFITS = 2;
export const TOUCH_RETAKE_LOOSE = 2;
export const TOUCH_MAX_SHIFT_PX = 2.0;
export const TOUCH_MAX_ROUNDS = 3;

export const TOUCH_REASONS = Object.freeze({
  touch_retake: ({ points }) => `Algunos toques no coinciden con los demás: repetí ${points}. Pegá el post-it ` +
    "centrado en la cruz y sacá la mano.",
  touch_inaccurate: ({ error }) => "Los toques están muy dispersos: el contorno quedaría a unos " +
    `${pyRound(error, 1).toFixed(1)} px de cada papel. Pegá cada post-it centrado en su cruz y no muevas la ` +
    "cámara ni el proyector.",
  touch_invalid: () => "Los toques no forman la grilla de la proyección. Poné el post-it sobre la cruz que " +
    "marca el círculo, en orden.",
});

/** "el punto 3" / "los puntos 3, 7 y 11" (1-based numbers). */
export function pointsText(numbers) {
  if (numbers.length === 1) return `el punto ${numbers[0]}`;
  return `los puntos ${numbers.slice(0, -1).join(", ")} y ${numbers[numbers.length - 1]}`;
}

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
  return { cols, rows, targets: touchTargets(width, height, cols, rows), captured: [], retake: [], rounds: 0,
    baseline: [...baseline], phase: "waiting", rebase: false, sum_x: 0.0, sum_y: 0.0, samples: 0, since: null,
    many: false };
}

/** The cross armed now: the next touch to retake, else the next new one. */
export function touchStep(run) {
  return run.retake.length ? run.retake[0] : run.captured.length;
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
    const point = { x: run.sum_x / run.samples, y: run.sum_y / run.samples };
    const captured = [...run.captured];
    let retake = run.retake;
    if (retake.length) {
      captured[retake[0]] = point;
      retake = retake.slice(1);
    } else {
      captured.push(point);
    }
    run = { ...idle(run, "lift"), captured, retake };
    return [run, !retake.length && captured.length === run.targets.length ? "done" : "captured"];
  }
  return [run, null];
}

/** CCV's [r]: the previous cross again; during retakes it only restarts the hold. */
export function touchBack(run) {
  if (run.retake.length) return idle(run, "waiting");
  return { ...idle(run, "waiting"), captured: run.captured.slice(0, -1) };
}

/** The run goes on with these touches (0-based) to take again; the phase stays. */
export function touchRetake(run, indices) {
  return { ...run, retake: [...indices], rounds: run.rounds + 1 };
}

export function touchProgress(run, now) {
  if (run.phase !== "holding" || run.since === null) return 0.0;
  return Math.min(1.0, Math.max(0.0, (now - run.since) / TOUCH_HOLD_S));
}

/** Least squares over the given touches. */
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

/** The homography through 4 touches exactly (Gaussian elimination, as in Python). */
function homography4(camera, projector) {
  const rows = [];
  camera.forEach((c, i) => {
    const q = projector[i];
    rows.push([c.x, c.y, 1.0, 0.0, 0.0, 0.0, -q.x * c.x, -q.x * c.y, q.x]);
    rows.push([0.0, 0.0, 0.0, c.x, c.y, 1.0, -q.y * c.x, -q.y * c.y, q.y]);
  });
  for (let col = 0; col < 8; col += 1) {
    let pivot = col;
    for (let r = col + 1; r < 8; r += 1) {
      if (Math.abs(rows[r][col]) > Math.abs(rows[pivot][col])) pivot = r;
    }
    if (Math.abs(rows[pivot][col]) <= 1e-9) return null;
    [rows[col], rows[pivot]] = [rows[pivot], rows[col]];
    for (let r = col + 1; r < 8; r += 1) {
      const factor = rows[r][col] / rows[col][col];
      if (factor !== 0.0) {
        for (let k = col; k < 9; k += 1) rows[r][k] -= factor * rows[col][k];
      }
    }
  }
  const h = new Array(8).fill(0.0);
  for (let col = 7; col >= 0; col -= 1) {
    let total = rows[col][8];
    for (let k = col + 1; k < 8; k += 1) total -= rows[col][k] * h[k];
    h[col] = total / rows[col][col];
  }
  return h.every(Number.isFinite) ? h : null;
}

function residual(h, camera, projector) {
  const w = h[6] * camera.x + h[7] * camera.y + 1.0;
  if (Math.abs(w) <= 1e-12) return Infinity;
  const dx = (h[0] * camera.x + h[1] * camera.y + h[2]) / w - projector.x;
  const dy = (h[3] * camera.x + h[4] * camera.y + h[5]) / w - projector.y;
  return Math.sqrt(dx * dx + dy * dy);
}

function subsets(n, targets) {
  const out = [];
  if (n * (n - 1) * (n - 2) * (n - 3) / 24 <= TOUCH_MAX_SUBSETS) {
    for (let a = 0; a < n; a += 1) {
      for (let b = a + 1; b < n; b += 1) {
        for (let c = b + 1; c < n; c += 1) {
          for (let d = c + 1; d < n; d += 1) out.push([a, b, c, d]);
        }
      }
    }
    return out;
  }
  const xs = targets.map((t) => t.x);
  const ys = targets.map((t) => t.y);
  const cx = (Math.min(...xs) + Math.max(...xs)) / 2;
  const cy = (Math.min(...ys) + Math.max(...ys)) / 2;
  const quarters = [[false, false], [true, false], [false, true], [true, true]].map(([right, below]) =>
    targets.map((t, i) => [t, i]).filter(([t]) => t.x !== cx && t.y !== cy && (t.x > cx) === right && (t.y > cy) === below)
      .map(([, i]) => i));
  for (const a of quarters[0]) {
    for (const b of quarters[1]) {
      for (const c of quarters[2]) {
        for (const d of quarters[3]) out.push([a, b, c, d]);
      }
    }
  }
  const stride = Math.ceil(out.length / TOUCH_MAX_SUBSETS);
  return out.filter((_, i) => i % stride === 0);
}

/** Least median of squares over 4-touch subsets: [h, residuals, criterion] or null. */
function robustStart(camera, targets) {
  const n = camera.length;
  const rank = Math.floor((n + 5) / 2) - 1;
  let best = null;
  for (const subset of subsets(n, targets)) {
    const h = homography4(subset.map((i) => camera[i]), subset.map((i) => targets[i]));
    if (h === null) continue;
    const residuals = camera.map((c, i) => residual(h, c, targets[i]));
    if (!residuals.every(Number.isFinite)) continue;
    const criterion = residuals.map((r) => r * r).sort((p, q) => p - q)[rank];
    if (best === null || criterion < best[2]) best = [h, residuals, criterion];
  }
  return best;
}

function sumSquares(values) {
  let total = 0.0;
  for (const v of values) total += v * v;
  return total;
}

/** The homography from the touches, or why not (contract 1.6). */
export function solveTouch(cv, camera, targets, width, height) {
  const total = targets.length;
  const result = { ok: false, code: null, reason: null, matrix: null, decoded: camera.length, total,
    fit: null, inliers: null, holdout: null, holdout_p95_px: null, holdout_max_px: null,
    outliers: null, coverage: null, retake: null, fit_rms_px: null, model_rms_px: null, outlier_bar_px: null,
    shift_px: null };
  const fail = (code, values = {}) => Object.assign(result, { ok: false, code, reason: TOUCH_REASONS[code](values), matrix: null });

  const n = camera.length;
  if (n < TOUCH_MIN_POINTS) return fail("touch_invalid");
  const start = robustStart(camera, targets);
  if (start === null) return fail("touch_invalid");
  let [, errors, criterion] = start;
  let sigma = Math.sqrt(criterion) / 1.18;
  let without = null;
  for (let pass = 0; pass < TOUCH_REFITS; pass += 1) {
    const bar = Math.max(TOUCH_OUTLIER_SIGMA * sigma, TOUCH_OUTLIER_MIN_PX);
    let kept = errors.map((_, i) => i).filter((i) => errors[i] <= bar);
    if (kept.length < TOUCH_MIN_POINTS) {
      kept = errors.map((_, i) => i).sort((p, q) => (errors[p] - errors[q]) || (p - q)).slice(0, TOUCH_MIN_POINTS);
    }
    const robust = fitHomography(cv, kept.map((i) => camera[i]), kept.map((i) => targets[i]));
    if (robust === null) return fail("touch_invalid");
    without = robust;
    errors = camera.map((c, i) => error(robust, c, targets[i]));
    sigma = Math.min(sigma, Math.sqrt(sumSquares(kept.map((i) => errors[i])) / (2 * kept.length - 8)));
  }
  const bar = Math.max(TOUCH_OUTLIER_SIGMA * sigma, TOUCH_OUTLIER_MIN_PX);
  let off = errors.map((_, i) => i).filter((i) => errors[i] > bar);
  result.outlier_bar_px = bar;
  const matrix = fitHomography(cv, camera, targets);
  if (matrix === null || jacobianSign(matrix, camera) <= 0) return fail("touch_invalid");
  const fitErrors = camera.map((c, i) => error(matrix, c, targets[i]));
  const totalSq = sumSquares(fitErrors);
  const held = leaveOneOut(cv, camera, targets);
  const finite = held.filter(Number.isFinite).sort((p, q) => p - q);
  Object.assign(result, { fit: n, holdout: n, inliers: n - off.length, outliers: off.length,
    holdout_p95_px: finite.length === held.length ? sortedPercentile(finite, 95) : Infinity,
    holdout_max_px: Math.max(...held), fit_rms_px: Math.sqrt(totalSq / n),
    model_rms_px: Math.sqrt(8.0 * totalSq / ((2 * n - 8) * n)) });
  const xs = targets.map((q) => q.x);
  const ys = targets.map((q) => q.y);
  result.coverage = (Math.max(...xs) - Math.min(...xs)) * (Math.max(...ys) - Math.min(...ys)) / (width * height);
  // How far the touches that are off bend the outline (as in Python).
  let shift = 0.0;
  for (const c of camera) {
    let a;
    let b;
    try {
      a = transformPoint(c, matrix);
      b = transformPoint(c, without);
    } catch {
      shift = Infinity;
      break;
    }
    shift = Math.max(shift, distance(a, b));
  }
  result.shift_px = shift;
  if (result.model_rms_px <= TOUCH_MAX_MODEL_PX && (!off.length || shift <= TOUCH_MAX_SHIFT_PX)) {
    return Object.assign(result, { ok: true, matrix });
  }
  if (!off.length) {
    off = errors.map((_, i) => i).sort((p, q) => (errors[q] - errors[p]) || (p - q)).slice(0, TOUCH_RETAKE_LOOSE)
      .sort((p, q) => p - q);
  }
  if (off.length <= Math.floor(n / 2)) {
    result.retake = off;
    return fail("touch_retake", { points: pointsText(off.map((i) => i + 1)) });
  }
  return fail("touch_inaccurate", { error: result.model_rms_px });
}

/** A touch_retake after TOUCH_MAX_ROUNDS rounds: the run ends instead. */
export function touchGiveUp(result) {
  return { ...result, code: "touch_inaccurate", retake: null,
    reason: TOUCH_REASONS.touch_inaccurate({ error: result.model_rms_px }) };
}
