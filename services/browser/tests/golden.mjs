/**
 * Shared by the parity tests and eval: the Python answers, the frames they
 * were computed on, and opencv.js loaded in Node.
 */

import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { gunzipSync } from "node:zlib";

const GOLDEN = new URL("../parity/golden/", import.meta.url);
const REGENERATE = "./.venv/bin/python -m services.browser.parity.export";

export function loadGolden() {
  const file = new URL("golden.json", GOLDEN);
  if (!existsSync(file)) {
    throw new Error(`Faltan las respuestas de Python (${file.pathname}). Generalas con: ${REGENERATE}`);
  }
  return JSON.parse(readFileSync(file, "utf8"));
}

let cvPromise = null;

/** opencv.js in Node; the same file the browser build ships. */
export function loadCv() {
  cvPromise ??= (async () => {
    const require = createRequire(import.meta.url);
    const cv = require("@techstark/opencv-js");
    await new Promise((resolve) => {
      if (cv.Mat) resolve();
      else cv.onRuntimeInitialized = resolve;
    });
    // The Emscripten module is a thenable that resolves to itself: returning
    // it from an async function (or awaiting it) would spin forever.
    delete cv.then;
    return cv;
  })();
  return cvPromise;
}

/** A golden frame as a BGR cv.Mat; the caller deletes it. */
export function frameMat(cv, golden, name) {
  const meta = golden.frames[name];
  const bytes = gunzipSync(readFileSync(new URL(`frames/${name}.bgr.gz`, GOLDEN)));
  const mat = new cv.Mat(meta.height, meta.width, cv.CV_8UC3);
  mat.data.set(bytes);
  return mat;
}

/** A golden single-channel image as `{ width, height, data }` (raster order). */
export function grayFrame(golden, name) {
  const meta = golden.frames[name];
  const data = new Uint8Array(gunzipSync(readFileSync(new URL(`frames/${name}.gray.gz`, GOLDEN))));
  return { width: meta.width, height: meta.height, data };
}

/** A golden single-channel image as a CV_8UC1 cv.Mat; the caller deletes it. */
export function grayMat(cv, golden, name) {
  const { width, height, data } = grayFrame(golden, name);
  const mat = new cv.Mat(height, width, cv.CV_8UC1);
  mat.data.set(data);
  return mat;
}

export const points = (list) => list.map(([x, y]) => ({ x, y }));

export function boardFromGolden(data) {
  return {
    playfield: points(data.playfield),
    start: points(data.start),
    goal: points(data.goal),
    bumpers: data.bumpers.map(points),
    markers: data.markers.map(points),
  };
}

export function boardToGolden(board) {
  const poly = (polygon) => polygon.map((p) => [p.x, p.y]);
  return {
    playfield: poly(board.playfield), start: poly(board.start), goal: poly(board.goal),
    bumpers: board.bumpers.map(poly), markers: board.markers.map(poly),
  };
}

/**
 * Differences between two JSON values: numbers within `tolerance`, everything
 * else exact. Returns a list of "path: got vs want" strings (empty = equal).
 */
export function diff(got, want, tolerance = 1e-6, path = "$", out = []) {
  if (out.length > 20) return out;
  if (typeof want === "number" && typeof got === "number") {
    const scale = Math.max(1, Math.abs(want));
    if (!(Math.abs(got - want) <= tolerance * scale)) out.push(`${path}: ${got} vs ${want}`);
    return out;
  }
  if (Array.isArray(want)) {
    if (!Array.isArray(got) || got.length !== want.length) {
      out.push(`${path}: length ${Array.isArray(got) ? got.length : typeof got} vs ${want.length}`);
      return out;
    }
    want.forEach((item, i) => diff(got[i], item, tolerance, `${path}[${i}]`, out));
    return out;
  }
  if (want !== null && typeof want === "object") {
    if (got === null || typeof got !== "object" || Array.isArray(got)) {
      out.push(`${path}: ${JSON.stringify(got)?.slice(0, 80)} vs object`);
      return out;
    }
    const keys = new Set([...Object.keys(want), ...Object.keys(got)]);
    for (const key of keys) {
      if (!(key in want)) out.push(`${path}.${key}: unexpected`);
      else if (!(key in got)) out.push(`${path}.${key}: missing`);
      else diff(got[key], want[key], tolerance, `${path}.${key}`, out);
    }
    return out;
  }
  if (got !== want) out.push(`${path}: ${JSON.stringify(got)} vs ${JSON.stringify(want)}`);
  return out;
}
