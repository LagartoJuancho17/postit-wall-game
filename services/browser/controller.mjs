/**
 * Port of services/web/backend/server.py `GameController` (and camera.py),
 * for the build that runs without a Python server.
 *
 * Same phases, same snapshot shape, same messages, same refusals: the panel
 * and the projector cannot tell which backend answers. What the browser
 * cannot do is said where it happens: there is no USB camera control, and
 * the palette, camera presets and event log persist in the page's storage.
 *
 * Frames arrive as BGR cv.Mat (the browser glue decodes the JPEG); `cv` is
 * needed for live detection and optional scans, and is passed in.
 */

import { ALIGNMENT_FRAMES, alignmentLayout, decodeAlignment, frameKey } from "./alignment.mjs";
import { offsetConvexPolygon, transformBoard, transformPoint } from "./calibration.mjs";
import { isPlainObject, pyJsonLength, pyLength, pyMod, pyRound, pyStrip, pyTruthy } from "./numbers.mjs";
import { buildPalette, DEFAULT_PALETTE, normalizeHex, paletteJson, ROLE_LABELS, ROLES } from "./palette.mjs";
import { GameStatus, launch, readyState, step } from "./physics.mjs";
import { boardFromNotes, findNotes, inspectFrame } from "./vision.mjs";

const LIVE_EXPECTED_NOTES = Object.freeze({ start: 1, goal: 1, bumper: null, marker: null });
const LIVE_DETECTION_MAX_AGE_S = 1.5;
// A run and its overlay check hold the last detection (previews are paused);
// a run that stops sending frames is cancelled (server.py, same values).
const ALIGN_HOLD_S = 20.0;
const ALIGN_STALL_S = 20.0;

// Camera <-> projector alignment and the overlay check
// (docs/alignment-and-color.md sections 1.4, 1.5 and 2.3).
const ALIGN_NEEDED = ALIGNMENT_FRAMES.length;
// What the projector draws around each note (live-view.mjs, contract 2.1),
// in projector px; the backend discounts that light (2.5).
export const RIM_OFFSET_PX = 6.0;
export const RIM_WIDTH_PX = 3.0;
export const RIM_MARGIN_PX = 4.0;
const CHECK_KINDS = ["neutral", "overlay"];
const OVERLAY_MIN_IOU = 0.85;
const PROBE_GRID = [1, 2, 3, 4, 5, 6, 7, 8, 9];   // candidate centers at tenths of the viewport
const PROBE_CAMERA_MARGIN = 8;                     // camera px a probe corner keeps from the frame edge
const STALE_DETAILS = Object.freeze({
  projector_resized: "Cambió el tamaño de la proyección: alineá de nuevo.",
  camera_changed: "Cambió la cámara: alineá de nuevo.",
  camera_label: "Cambió la cámara: alineá de nuevo.",
  camera_size: "Cambió la resolución de la cámara: alineá de nuevo.",
});
const OVERLAY_UNCHECKED = "El relleno de color todavía no se verificó.";
const OVERLAY_OK = "Relleno de color verificado con la cámara.";
// The physics engine validates in English; the operator reads Spanish.
const PHYSICS_MESSAGES = Object.freeze({
  "start centroid must fit inside playfield with ball radius":
    "El inicio está demasiado cerca del borde del área de juego.",
  "start overlaps goal": "El inicio toca la llegada. Separalos.",
  "start overlaps a bumper": "El inicio toca un rebote. Separalos.",
});
const PHYSICS_FALLBACK = "Un post-it detectado no tiene una forma válida.";
const CAMERA_MODES = ["whiteBalanceMode", "exposureMode", "focusMode"];
const CAMERA_VALUES = ["colorTemperature", "exposureTime", "focusDistance",
  "brightness", "contrast", "saturation", "sharpness", "gain"];
const MAX_LABEL = 200;
const MAX_REPORT_BYTES = 20000;

export const PALETTE_KEY = "postit-wall:palette";
export const PRESETS_KEY = "postit-wall:camera-presets";

export const HTTP = Object.freeze({
  BAD_REQUEST: 400, FORBIDDEN: 403, NOT_FOUND: 404, CONFLICT: 409, LENGTH_REQUIRED: 411,
  REQUEST_ENTITY_TOO_LARGE: 413, UNSUPPORTED_MEDIA_TYPE: 415, BAD_GATEWAY: 502,
});

/** A caller error with an explicit HTTP status, like Python's RequestError. */
export class RequestError extends Error {
  constructor(message, status = HTTP.BAD_REQUEST) {
    super(message);
    this.name = "RequestError";
    this.status = status;
  }
}

const pointJson = (p) => ({ x: pyRound(p.x, 3), y: pyRound(p.y, 3) });
export const polygonJson = (polygon) => ({ points: polygon.map(pointJson) });

function boardJson(board) {
  if (board === null) return null;
  return {
    playfield: polygonJson(board.playfield),
    start: polygonJson(board.start),
    goal: polygonJson(board.goal),
    bumpers: board.bumpers.map(polygonJson),
    markers: board.markers.map(polygonJson),
  };
}

function finiteNumber(value, label) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new RequestError(`${label} debe ser un número finito.`);
  }
  return value;
}

const rectangle = (width, height) => [{ x: 0, y: 0 }, { x: width, y: 0 }, { x: width, y: height }, { x: 0, y: height }];

/** The Spanish text an operator reads for an invalid live board. */
function operatorReason(error) {
  const text = error.message;
  if (text.startsWith("Post-its de ")) return text;
  return PHYSICS_MESSAGES[text] ?? PHYSICS_FALLBACK;
}

/** `X-Postit-Align: <bit>:<0|1>` -> [bit, inverse]. */
export function alignKey(header) {
  if (typeof header !== "string" || header.length !== 3 || header[1] !== ":" ||
      !"0123456".includes(header[0]) || !"01".includes(header[2])) {
    throw new RequestError("Cabecera X-Postit-Align inválida.");
  }
  return [Number(header[0]), header[2] === "1"];
}

const round3 = (value) => (typeof value === "number" && Number.isFinite(value) ? pyRound(value, 3) : value);

/** Left-to-right mean, as server.py `_mean`. */
function mean(values) {
  let total = 0.0;
  for (const value of values) total += value;
  return total / values.length;
}

/** inv(matrix) / inv[2][2], by cofactors. */
export function normalizedInverse(m) {
  const [[a, b, c], [d, e, f], [g, h, i]] = m;
  const A = e * i - f * h;
  const B = -(d * i - f * g);
  const C = d * h - e * g;
  const determinant = a * A + b * B + c * C;
  if (!Number.isFinite(determinant) || determinant === 0) throw new Error("singular matrix");
  const inverse = [
    [A, -(b * i - c * h), b * f - c * e],
    [B, a * i - c * g, -(a * f - c * d)],
    [C, -(a * h - b * g), a * e - b * d],
  ].map((row) => row.map((value) => value / determinant));
  const scale = inverse[2][2];
  return inverse.map((row) => row.map((value) => value / scale));
}

function mapPoint(point, matrix) {
  try {
    return transformPoint(point, matrix);
  } catch {
    return null;
  }
}

const boxesIntersect = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
const grown = (box, by) => [box[0] - by, box[1] - by, box[2] + by, box[3] + by];

/** Side of an overlay-check probe square in projector px (contract 2.3). */
export function probeSide(width, height) {
  return Math.max(32, pyRound(0.05 * Math.min(width, height)));
}

/** One bare-wall test square per role, deterministic (contract 2.3). */
export function placeProbes(liveDetection, matrix, [width, height], [cameraWidth, cameraHeight]) {
  const side = probeSide(width, height);
  const keepOut = [];
  for (const role of ROLES) {
    for (const note of liveDetection?.roles?.[role]?.notes ?? []) {
      const mapped = note.points.map((point) => mapPoint({ x: point.x, y: point.y }, matrix));
      if (!mapped.length || mapped.some((point) => point === null)) continue;
      const xs = mapped.map((point) => point.x);
      const ys = mapped.map((point) => point.y);
      keepOut.push(grown([Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)], side));
    }
  }
  const inverse = normalizedInverse(matrix);
  const low = PROBE_CAMERA_MARGIN;
  const chosen = [];
  for (const role of ROLES) {
    for (const j of PROBE_GRID) {
      let found = null;
      for (const i of PROBE_GRID) {
        const cx = width * i / 10;
        const cy = height * j / 10;
        const square = [cx - side / 2, cy - side / 2, cx + side / 2, cy + side / 2];
        if (keepOut.some((box) => boxesIntersect(square, box))) continue;
        if (chosen.some(([, box]) => boxesIntersect(square, grown(box, side)))) continue;
        const corners = [[square[0], square[1]], [square[2], square[1]], [square[2], square[3]], [square[0], square[3]]]
          .map(([x, y]) => mapPoint({ x, y }, inverse));
        if (corners.some((corner) => corner === null || !(low <= corner.x && corner.x <= cameraWidth - 1 - low &&
            low <= corner.y && corner.y <= cameraHeight - 1 - low))) continue;
        found = square;
        break;
      }
      if (found !== null) {
        chosen.push([role, found]);
        break;
      }
    }
  }
  return chosen.map(([role, box]) => ({ role, points: [[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]]
    .map(([x, y]) => ({ x, y })) }));
}

function polygonIou(cv, first, second) {
  const a = cv.matFromArray(first.length, 1, cv.CV_32FC2, first.flatMap((p) => [p.x, p.y]));
  const b = cv.matFromArray(second.length, 1, cv.CV_32FC2, second.flatMap((p) => [p.x, p.y]));
  const out = new cv.Mat();
  try {
    const areaA = Math.abs(cv.contourArea(a, false));
    const areaB = Math.abs(cv.contourArea(b, false));
    const intersection = cv.intersectConvexConvex(a, b, out, true);
    const union = areaA + areaB - intersection;
    return union > 0 ? intersection / union : 0.0;
  } finally {
    [a, b, out].forEach((mat) => mat.delete());
  }
}

/** Greedy IoU matching; the smallest IoU among matched pairs (null: no pair). */
function matchRole(cv, neutral, overlay) {
  const pairs = [];
  neutral.forEach((a, i) => overlay.forEach((b, j) => pairs.push([polygonIou(cv, a, b), i, j])));
  pairs.sort((p, q) => (q[0] - p[0]) || (p[1] - q[1]) || (p[2] - q[2]));
  const usedA = new Set();
  const usedB = new Set();
  const matched = [];
  for (const [iou, i, j] of pairs) {
    if (usedA.has(i) || usedB.has(j)) continue;
    usedA.add(i);
    usedB.add(j);
    matched.push(iou);
  }
  return matched.length ? Math.min(...matched) : null;
}

const overlayState = (status, probes, detail = null) => ({ status, fill: status === "ok",
  detail: detail || (status === "ok" ? OVERLAY_OK : OVERLAY_UNCHECKED), probes });

const copyFrame = (mat) => ({ width: mat.cols, height: mat.rows, data: new Uint8Array(mat.data) });

// ---- camera.py

export function cameraLabel(value) {
  if (typeof value !== "string" || !pyStrip(value) || pyLength(value) > MAX_LABEL) {
    throw new Error(`camera debe ser el nombre de la cámara (1 a ${MAX_LABEL} caracteres).`);
  }
  return pyStrip(value);
}

export function cameraSettings(value) {
  if (!isPlainObject(value) || !Object.keys(value).length) {
    throw new Error("settings debe ser un objeto con ajustes de cámara.");
  }
  const unknown = Object.keys(value).filter((k) => !CAMERA_MODES.includes(k) && !CAMERA_VALUES.includes(k)).sort();
  if (unknown.length) throw new Error(`Ajuste de cámara desconocido: ${unknown.join(", ")}.`);
  const clean = {};
  for (const key of CAMERA_MODES) {
    if (key in value) {
      if (value[key] !== "manual") throw new Error(`${key} tiene que ser manual: nada queda en automático.`);
      clean[key] = "manual";
    }
  }
  for (const key of CAMERA_VALUES) {
    if (key in value) {
      if (typeof value[key] !== "number" || !Number.isFinite(value[key])) {
        throw new Error(`${key} debe ser un número finito.`);
      }
      clean[key] = value[key];
    }
  }
  return clean;
}

export function cameraReport(value) {
  if (!isPlainObject(value)) throw new Error("report debe ser un objeto.");
  if (pyJsonLength(value) > MAX_REPORT_BYTES) throw new Error("El reporte de cámara es demasiado grande.");
  return value;
}

function presetEntry(settings, score, black, now) {
  const cleanScore = typeof score === "number" && Number.isFinite(score) ? score : null;
  return { settings, score: cleanScore, black: pyTruthy(black),
    saved_at: `${new Date(now).toISOString().slice(0, 19)}+00:00` };
}

function storageGet(storage, key) {
  try {
    return storage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function storageSet(storage, key, value) {
  try {
    storage?.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

/**
 * Serialized game transitions. `clock()` returns seconds (monotonic);
 * `storage` is a Storage-like object or null; `log(entry)` receives every
 * event the Python server would write to runs/web-events.jsonl.
 */
export class GameController {
  constructor({ clock = () => performance.now() / 1000, storage = null, log = null,
    wallClock = () => Date.now() } = {}) {
    this.clock = clock;
    this.wallClock = wallClock;
    this.storage = storage;
    this.logSink = log;
    this.cv = null;
    this.paletteChoices = Object.fromEntries(ROLES.map((role) => [role, null]));
    this.palette = DEFAULT_PALETTE;
    this.loadPalette();
    this.cameraPresets = {};
    this.loadCameraPresets();
    this.cameraBoard = null;
    this.board = null;
    this.game = null;
    this.projectorOpen = false;
    this.projectorWidth = 0;
    this.projectorHeight = 0;
    this.projectionMode = "live";
    this.aimDegrees = -45.0;
    this.attempts = 0;
    this.paused = false;
    this.lastTick = null;
    this.accumulator = 0.0;
    this.boardWidth = 0;
    this.boardHeight = 0;
    this.frameWidth = 0;
    this.frameHeight = 0;
    this.frameCamera = null;
    this.liveDetection = null;
    this.liveDetectionAt = null;
    this.boardSpace = "camera";   // space of this.board: "camera" or "projector"
    // The last live notes and the playable board they give, in the game's
    // space; kept so a change of alignment re-maps them without a new frame.
    this.lastNotes = null;
    this.candidate = null;
    // Alignment (contract 1.4) and the overlay check (contract 2.3).
    this.alignStatus = "none";
    this.alignLayout = null;
    this.alignFrames = new Map();
    this.alignMatrix = null;
    this.alignCamera = null;
    this.alignProjector = null;
    this.alignQuality = null;
    this.alignDetail = "Sin alinear.";
    this.alignCode = null;
    this.overlay = overlayState("unchecked", []);
    this.checkFrames = new Map();
    this.holdUntil = null;
    this.alignActivity = null;
    this.cameraEpoch = 0;
    this.message = "Abrí la cámara en el navegador y luego la ventana del proyector con J.";
    this.revision = 0;
  }

  log(event, fields = {}) {
    if (!this.logSink) return;
    this.logSink({ at: new Date(this.wallClock()).toISOString(), event, ...fields });
  }

  changed(message) {
    this.message = message;
    this.revision += 1;
  }

  loadPalette() {
    const text = storageGet(this.storage, PALETTE_KEY);
    if (text === null) return;
    try {
      const saved = JSON.parse(text).choices;
      if (!isPlainObject(saved)) throw new Error("choices must be an object");
      const choices = Object.fromEntries(ROLES.map((role) => [
        role, saved[role] !== null && saved[role] !== undefined ? normalizeHex(saved[role]) : null]));
      if (Object.keys(saved).some((role) => !ROLES.includes(role))) throw new Error("unknown role");
      this.palette = buildPalette(choices);
      this.paletteChoices = choices;
    } catch (error) {
      this.log("palette_ignored", { path: PALETTE_KEY, reason: error.message });
    }
  }

  savePalette() {
    if (!storageSet(this.storage, PALETTE_KEY, JSON.stringify({ version: 1, choices: this.paletteChoices }))) {
      this.log("palette_not_saved", { path: PALETTE_KEY, reason: "storage blocked" });
    }
  }

  loadCameraPresets() {
    const text = storageGet(this.storage, PRESETS_KEY);
    if (text === null) return;
    try {
      const { presets } = JSON.parse(text);
      if (!isPlainObject(presets)) throw new Error("presets debe ser un objeto");
      const loaded = {};
      for (const [label, entry] of Object.entries(presets)) {
        loaded[cameraLabel(label)] = {
          settings: cameraSettings(entry.settings),
          score: entry.score ?? null,
          black: pyTruthy(entry.black ?? false),
          saved_at: String(entry.saved_at ?? ""),
        };
      }
      this.cameraPresets = loaded;
    } catch (error) {
      this.log("camera_presets_ignored", { path: PRESETS_KEY, reason: error.message });
    }
  }

  saveCameraPresets() {
    if (!storageSet(this.storage, PRESETS_KEY, JSON.stringify({ version: 1, presets: this.cameraPresets }))) {
      this.log("camera_presets_not_saved", { path: PRESETS_KEY, reason: "storage blocked" });
    }
  }

  paletteJson() {
    const rules = paletteJson(this.palette);
    return {
      custom: Object.values(this.paletteChoices).some((choice) => choice !== null),
      roles: Object.fromEntries(ROLES.map((role) => [role, { ...rules[role], custom: this.paletteChoices[role] !== null }])),
    };
  }

  phase() {
    if (this.game !== null) {
      if (this.paused && this.game.status === GameStatus.RUNNING) return "paused";
      return this.game.status;
    }
    return "waiting_camera";
  }

  snapshot() {
    this.expireDetection();
    const { game } = this;
    return {
      revision: this.revision,
      phase: this.phase(),
      message: this.message,
      camera: { status: "browser", width: this.frameWidth, height: this.frameHeight, label: this.frameCamera },
      live_detection: this.liveDetection === null ? null : JSON.parse(JSON.stringify(this.liveDetection)),
      alignment: this.alignmentJson(),
      camera_board: boardJson(this.cameraBoard),
      board: boardJson(this.board),
      palette: this.paletteJson(),
      camera_presets: JSON.parse(JSON.stringify(this.cameraPresets)),
      game: {
        status: game ? game.status : "none",
        space: game ? this.boardSpace : this.nextSpace(),
        width: this.boardWidth,
        height: this.boardHeight,
        position: game ? pointJson(game.position) : null,
        bounces: game ? game.bounce_count : 0,
        elapsed_s: game ? pyRound(game.elapsed_s, 3) : 0.0,
        aim_degrees: pyRound(this.aimDegrees, 3),
        attempts: this.attempts,
        paused: this.paused,
      },
      projection: {
        open: this.projectorOpen,
        mode: this.projectionMode,
        width: this.projectorWidth,
        height: this.projectorHeight,
      },
    };
  }

  holdDetection() {
    this.holdUntil = this.clock() + ALIGN_HOLD_S;
  }

  /** Back to the live scene, except during a run: patterns until it ends. */
  liveMode() {
    this.projectionMode = this.alignStatus === "collecting" ? "align" : "live";
  }

  refuseWhileCollecting() {
    if (this.alignStatus === "collecting") throw new RequestError("Esperá a que termine la alineación.", HTTP.CONFLICT);
  }

  /** What a frame's answer belongs to, taken before an asynchronous decode. */
  ticket() {
    return { epoch: this.cameraEpoch, palette: this.palette };
  }

  expireDetection() {
    const now = this.clock();
    if (this.alignStatus === "collecting" && this.alignActivity !== null && now - this.alignActivity > ALIGN_STALL_S) {
      this.dropAlignment("none", "La alineación se interrumpió: el panel dejó de enviar patrones.");
      this.changed(this.alignDetail);
      this.log("alignment_cancelled", { reason: "stalled" });
    }
    if (this.holdUntil !== null) {
      if (now < this.holdUntil) return;
      this.holdUntil = null;
    }
    if (this.liveDetectionAt !== null && now - this.liveDetectionAt > LIVE_DETECTION_MAX_AGE_S) {
      this.clearDetection();
      this.changed("Esperando detección en vivo de la cámara.");
      this.log("live_detection_expired");
    }
  }

  clearDetection() {
    this.liveDetection = null;
    this.liveDetectionAt = null;
    this.lastNotes = null;
    this.candidate = null;
    this.cameraBoard = null;
    if (this.game === null || this.game.status === GameStatus.READY) this.applyCandidate();
  }

  // ---- alignment state

  nextSpace() {
    return this.alignStatus === "ready" ? "projector" : "camera";
  }

  alignmentJson() {
    return {
      status: this.alignStatus,
      layout: this.alignStatus === "collecting" ? JSON.parse(JSON.stringify(this.alignLayout)) : null,
      matrix: this.alignMatrix !== null ? this.alignMatrix.map((row) => [...row]) : null,
      camera: this.alignCamera ? { ...this.alignCamera } : null,
      projector: this.alignProjector ? { ...this.alignProjector } : null,
      received: this.alignFrames.size,
      needed: ALIGN_NEEDED,
      quality: this.alignQuality ? { ...this.alignQuality } : null,
      detail: this.alignDetail,
      code: this.alignCode,
      overlay: { ...this.overlay, probes: this.overlay.probes.map((probe) => ({ role: probe.role,
        points: probe.points.map((point) => ({ x: pyRound(point.x, 3), y: pyRound(point.y, 3) })) })) },
    };
  }

  dropAlignment(status, detail) {
    this.alignStatus = status;
    this.alignLayout = null;
    this.alignFrames = new Map();
    this.alignMatrix = null;
    this.alignCamera = null;
    this.alignProjector = null;
    this.alignQuality = null;
    this.alignDetail = detail;
    this.alignCode = null;
    this.overlay = overlayState("unchecked", []);
    this.checkFrames = new Map();
    this.holdUntil = null;
    this.alignActivity = null;
    if (this.projectionMode === "align") this.projectionMode = "live";
    this.recomputeCandidate();
  }

  /** A measured mapping no longer fits the camera or the projector window. */
  invalidateAlignment(reason) {
    if (this.alignStatus !== "ready" && this.alignStatus !== "collecting") return;
    const detail = STALE_DETAILS[reason];
    this.dropAlignment("stale", detail);
    this.changed(detail);
    this.log("alignment_invalidated", { reason });
  }

  // ---- game geometry

  /** The playable board in the game's space (contract 1.5); throws. */
  gameBoard(cameraBoard, width, height) {
    if (this.alignStatus === "ready" && this.alignMatrix !== null) {
      const mapped = transformBoard(cameraBoard, this.alignMatrix);
      const board = { playfield: rectangle(this.projectorWidth, this.projectorHeight), start: mapped.start,
        goal: mapped.goal, bumpers: mapped.bumpers, markers: mapped.markers };
      readyState(board);
      return [board, "projector", this.projectorWidth, this.projectorHeight];
    }
    readyState(cameraBoard);
    return [cameraBoard, "camera", width, height];
  }

  /** [camera board, game board tuple, operator reason] for one set of live notes. */
  candidateFor(notes, width, height) {
    try {
      const cameraBoard = boardFromNotes(notes, width, height);
      return [cameraBoard, this.gameBoard(cameraBoard, width, height), null];
    } catch (error) {
      if (!(error instanceof Error) || error instanceof TypeError) throw error;
      return [null, null, operatorReason(error)];
    }
  }

  /** An idle or ready game follows the latest playable board. */
  applyCandidate() {
    if (this.candidate === null) {
      this.board = null;
      this.game = null;
      this.boardWidth = 0;
      this.boardHeight = 0;
      this.boardSpace = this.nextSpace();
      return;
    }
    const [board, space, width, height] = this.candidate;
    this.board = board;
    this.game = readyState(board);
    this.boardSpace = space;
    this.boardWidth = width;
    this.boardHeight = height;
  }

  /** Re-map the last live notes after the alignment changed. */
  recomputeCandidate() {
    if (this.lastNotes === null) return;
    const [notes, width, height] = this.lastNotes;
    [this.cameraBoard, this.candidate] = this.candidateFor(notes, width, height);
    if (this.game === null || this.game.status === GameStatus.READY) this.applyCandidate();
  }

  action(payload) {
    this.expireDetection();
    const name = payload.action;
    if (typeof name !== "string") throw new RequestError("Falta action.");
    const running = () => this.game && this.game.status === GameStatus.RUNNING;
    if (name === "projection_open") {
      let width = finiteNumber(payload.width ?? null, "width");
      let height = finiteNumber(payload.height ?? null, "height");
      if (!Number.isInteger(width) || !Number.isInteger(height) ||
          !(width >= 320 && width <= 8192) || !(height >= 240 && height <= 8192)) {
        throw new RequestError("El proyector necesita tamaño válido entre 320×240 y 8192×8192.");
      }
      const changedSize = width !== this.projectorWidth || height !== this.projectorHeight;
      this.projectorWidth = width;
      this.projectorHeight = height;
      this.projectorOpen = true;
      this.liveMode();
      this.changed("Ventana del proyector abierta. Mostrando detección en vivo.");
      if (changedSize) {
        this.log("projection_resized", { width, height });
        this.invalidateAlignment("projector_resized");
      }
    } else if (name === "camera_changed") {
      this.cameraEpoch += 1;
      this.board = null;
      this.game = null;
      this.clearDetection();
      this.frameWidth = 0;
      this.frameHeight = 0;
      this.frameCamera = null;
      this.paused = false;
      this.lastTick = null;
      this.accumulator = 0.0;
      this.projectionMode = "live";
      this.changed("Cambió la cámara. Esperando detección en vivo.");
      this.log("camera_changed");
      this.invalidateAlignment("camera_changed");
    } else if (name === "projection_close") {
      this.projectorOpen = false;
      this.projectionMode = "live";
      if (running()) this.paused = true;
      this.lastTick = null;
      this.changed("Ventana del proyector cerrada. La partida quedó pausada.");
      if (this.alignStatus === "collecting") {
        this.dropAlignment("none", "Alineación cancelada: se cerró el proyector.");
        this.log("alignment_cancelled", { reason: "projection_close" });
      }
    } else if (name === "projection_black") {
      this.refuseWhileCollecting();
      if (running() && !this.paused) {
        throw new RequestError("Terminá o pausá el intento antes de escanear.", HTTP.CONFLICT);
      }
      this.projectionMode = "black";
      this.changed("Proyección en negro. Ahora podés capturar la pared.");
    } else if (name === "projection_neutral") {
      this.refuseWhileCollecting();
      if (running() && !this.paused) {
        throw new RequestError("Terminá o pausá el intento antes de escanear.", HTTP.CONFLICT);
      }
      this.projectionMode = "neutral";
      this.changed("Proyección neutra: solo el fondo, para leer colores.");
    } else if (name === "projection_restore") {
      this.liveMode();
      this.revision += 1;
    } else if (name === "align_start") {
      if (!this.projectorOpen) throw new RequestError("Abrí la ventana del proyector con J.", HTTP.CONFLICT);
      if (this.frameWidth <= 0) throw new RequestError("Primero tiene que llegar imagen de la cámara.", HTTP.CONFLICT);
      if (running() && !this.paused) {
        throw new RequestError("Terminá o pausá el intento antes de alinear.", HTTP.CONFLICT);
      }
      this.dropAlignment("collecting", "Alineando cámara y proyector…");
      this.alignLayout = alignmentLayout(this.projectorWidth, this.projectorHeight);
      this.alignCamera = { label: this.frameCamera, width: this.frameWidth, height: this.frameHeight };
      this.alignActivity = this.clock();
      this.holdDetection();
      this.projectionMode = "align";
      this.changed("Alineando cámara y proyector…");
      this.log("alignment_started", { width: this.projectorWidth, height: this.projectorHeight,
        dots: this.alignLayout.dots.length });
    } else if (name === "align_solve") {
      if (this.alignStatus !== "collecting") throw new RequestError("No hay una alineación en curso.", HTTP.CONFLICT);
      this.solveAlignment();
    } else if (name === "align_cancel") {
      if (this.alignStatus === "collecting") {
        this.dropAlignment("none", "Alineación cancelada.");
        this.changed("Alineación cancelada.");
        this.log("alignment_cancelled", { reason: "panel" });
      } else {
        this.revision += 1;
      }
    } else if (name === "align_clear") {
      this.dropAlignment("none", "Sin alinear.");
      this.changed("Alineación borrada.");
      this.log("alignment_cleared");
    } else if (name === "check_solve") {
      this.solveOverlayCheck();
      this.holdUntil = null;
    } else if (name === "aim" || name === "aim_left" || name === "aim_right") {
      if (this.game === null || this.game.status !== GameStatus.READY) {
        throw new RequestError("La dirección solo se cambia antes del lanzamiento.", HTTP.CONFLICT);
      }
      const aim = name === "aim" ? finiteNumber(payload.degrees ?? null, "degrees")
        : this.aimDegrees + (name === "aim_left" ? -5 : 5);
      this.aimDegrees = pyMod(aim + 180, 360) - 180;
      this.revision += 1;
    } else if (name === "launch") {
      this.refuseWhileCollecting();
      if (!this.projectorOpen || this.cameraBoard === null || this.board === null || this.game === null) {
        throw new RequestError("Abrí el proyector y mostrale a la cámara un inicio y una llegada.", HTTP.CONFLICT);
      }
      if (this.game.status !== GameStatus.READY) {
        throw new RequestError("Reiniciá el intento antes de volver a lanzar.", HTTP.CONFLICT);
      }
      const speed = Math.min(this.boardWidth, this.boardHeight) * 0.4;
      this.game = launch(this.board, this.aimDegrees, speed);
      this.attempts += 1;
      this.paused = false;
      this.lastTick = this.clock();
      this.accumulator = 0.0;
      this.projectionMode = "live";
      this.changed("Pelota lanzada.");
      this.log("attempt_started", { attempt: this.attempts, angle_degrees: pyRound(this.aimDegrees, 1),
        speed_px_s: pyRound(speed, 1) });
    } else if (name === "reset") {
      if (this.cameraBoard === null || this.candidate === null) {
        throw new RequestError("La cámara debe detectar un inicio y una llegada para reiniciar.", HTTP.CONFLICT);
      }
      this.applyCandidate();
      this.paused = false;
      this.lastTick = null;
      this.accumulator = 0.0;
      this.liveMode();
      this.changed("Intento reiniciado con los post-its de la vista en vivo.");
      this.log("attempt_reset");
    } else if (name === "pause") {
      if (this.game === null || this.game.status !== GameStatus.RUNNING) {
        throw new RequestError("No hay un intento en marcha.", HTTP.CONFLICT);
      }
      this.tickUnlocked();
      this.paused = true;
      this.lastTick = null;
      this.changed("Intento pausado.");
    } else if (name === "palette_set") {
      const { role } = payload;
      if (!ROLES.includes(role)) throw new RequestError("Rol de post-it desconocido.");
      let choices;
      let palette;
      try {
        choices = { ...this.paletteChoices, [role]: normalizeHex(payload.hex ?? null) };
        palette = buildPalette(choices, role);
      } catch (error) {
        throw new RequestError(error.message);
      }
      this.paletteChoices = choices;
      this.palette = palette;
      this.savePalette();
      const rule = palette[role];
      this.clearDetection();
      this.changed(`Color de ${ROLE_LABELS[role]} actualizado.`);
      this.log("palette_changed", { role, hex: rule.hex, hue_low: rule.hue_low, hue_high: rule.hue_high });
    } else if (name === "palette_reset") {
      this.paletteChoices = Object.fromEntries(ROLES.map((r) => [r, null]));
      this.palette = DEFAULT_PALETTE;
      this.savePalette();
      this.clearDetection();
      this.changed("Colores originales restaurados.");
      this.log("palette_reset");
    } else if (name === "camera_preset_save") {
      let label;
      let settings;
      let capabilities;
      try {
        label = cameraLabel(payload.camera ?? null);
        settings = cameraSettings(payload.settings ?? null);
        capabilities = cameraReport(pyTruthy(payload.capabilities) ? payload.capabilities : {});
      } catch (error) {
        throw new RequestError(error.message);
      }
      const entry = presetEntry(settings, payload.score, payload.black ?? null, this.wallClock());
      this.cameraPresets[label] = entry;
      this.saveCameraPresets();
      this.changed(`Ajuste fijo de cámara guardado para ${label}.`);
      const { trials } = payload;
      this.log("camera_preset_saved", { camera: label, settings, score: entry.score, black: entry.black,
        trials: Number.isInteger(trials) ? trials : null, capabilities });
    } else if (name === "camera_preset_clear") {
      let label;
      try {
        label = cameraLabel(payload.camera ?? null);
      } catch (error) {
        throw new RequestError(error.message);
      }
      if (label in this.cameraPresets) {
        delete this.cameraPresets[label];
        this.saveCameraPresets();
        this.changed(`Ajuste de cámara borrado para ${label}.`);
        this.log("camera_preset_cleared", { camera: label });
      }
    } else if (name === "camera_report") {
      let label;
      let details;
      try {
        label = cameraLabel(payload.camera ?? null);
        details = cameraReport(payload.report ?? null);
      } catch (error) {
        throw new RequestError(error.message);
      }
      this.log("camera_report", { camera: label, report: details });
    } else if (name === "resume") {
      this.refuseWhileCollecting();
      if (!this.projectorOpen || this.game === null || this.game.status !== GameStatus.RUNNING || !this.paused) {
        throw new RequestError("No hay un intento pausado o falta el proyector.", HTTP.CONFLICT);
      }
      this.paused = false;
      this.lastTick = this.clock();
      this.projectionMode = "live";
      this.changed("Intento reanudado.");
    } else {
      throw new RequestError(`Acción desconocida: ${name}`);
    }
    return this.snapshot();
  }

  requireCv() {
    if (!this.cv) throw new Error("OpenCV todavía se está cargando. Esperá unos segundos y reintentá.");
    return this.cv;
  }

  /** No USB camera control in the browser build: Chrome is the only route. */
  usbCamera() {
    return { available: false,
      reason: "en la versión web no hay control USB; solo se fija lo que Chrome permite" };
  }

  usbApply(payload) {
    try {
      cameraSettings(payload.settings ?? null);
    } catch (error) {
      throw new RequestError(error.message);
    }
    throw new RequestError(
      "La cámara no aceptó el ajuste por USB: la versión web no tiene control USB.", HTTP.BAD_GATEWAY);
  }

  /**
   * Python's `_measure` (find_notes with the bridge, frame_quality with
   * ready=False), with the footprint taken first, as `_measure_or_refuse`.
   */
  measureOrRefuse(frame) {
    const cv = this.requireCv();
    const { palette } = this;
    const bridge = this.rimFootprint(frame.cols, frame.rows);
    try {
      const { notes, quality } = inspectFrame(cv, frame, palette, { bridge, ready: false });
      return { notes, quality, palette };
    } catch (error) {
      if (!(error instanceof Error) || error instanceof TypeError || error instanceof ReferenceError) throw error;
      throw new RequestError(error.message);
    } finally {
      bridge?.delete();
    }
  }

  /**
   * Where the projector's own rims fall in this camera frame (contract 2.5):
   * a CV_8UC1 Mat the caller deletes, or null.
   */
  rimFootprint(width, height) {
    const detection = this.liveDetection;
    if (detection === null || !this.projectorOpen || this.projectionMode !== "live" ||
        detection.width !== width || detection.height !== height ||
        this.projectorWidth <= 0 || this.projectorHeight <= 0) return null;
    let toProjector;
    if (this.alignStatus === "ready" && this.alignMatrix !== null) {
      toProjector = this.alignMatrix;
    } else {
      const scale = Math.min(this.projectorWidth / width, this.projectorHeight / height);
      toProjector = [[scale, 0.0, (this.projectorWidth - width * scale) / 2],
        [0.0, scale, (this.projectorHeight - height * scale) / 2], [0.0, 0.0, 1.0]];
    }
    const toCamera = normalizedInverse(toProjector);
    const cv = this.requireCv();
    const footprint = cv.Mat.zeros(height, width, cv.CV_8UC1);
    let drawn = false;
    for (const role of ROLES) {
      for (const note of detection.roles[role].notes) {
        const camera = note.points.map((point) => ({ x: point.x, y: point.y }));
        const projected = camera.map((point) => mapPoint(point, toProjector));
        if (projected.some((point) => point === null)) continue;
        const rim = offsetConvexPolygon(projected, RIM_OFFSET_PX);
        if (rim === null) continue;
        const back = rim.map((point) => mapPoint(point, toCamera));
        if (back.some((point) => point === null)) continue;
        // Camera px per projector px around this note.
        const cx = mean(camera.map((point) => point.x));
        const cy = mean(camera.map((point) => point.y));
        const there = mapPoint({ x: cx + 1.0, y: cy }, toProjector);
        const here = mapPoint({ x: cx, y: cy }, toProjector);
        const stretch = there && here
          ? Math.sqrt((there.x - here.x) * (there.x - here.x) + (there.y - here.y) * (there.y - here.y)) : 1.0;
        const thickness = Math.max(3, Math.ceil((RIM_WIDTH_PX + 2 * RIM_MARGIN_PX) / Math.max(stretch, 1e-6)));
        const stroke = cv.matFromArray(back.length, 1, cv.CV_32SC2,
          back.flatMap((point) => [Math.floor(point.x + 0.5), Math.floor(point.y + 0.5)]));
        const strokes = new cv.MatVector();
        strokes.push_back(stroke);
        cv.polylines(footprint, strokes, true, new cv.Scalar(255), thickness, cv.LINE_8, 0);
        strokes.delete();
        stroke.delete();
        drawn = true;
      }
    }
    if (drawn) return footprint;
    footprint.delete();
    return null;
  }

  /** server.py `_store_frame_unlocked`: publish a frame's notes, follow its board. */
  storeFrame(notes, quality, width, height, label) {
    const preview = {
      width,
      height,
      roles: Object.fromEntries(ROLES.map((role) => [role, {
        count: notes[role].length, expected: LIVE_EXPECTED_NOTES[role], notes: notes[role].map(polygonJson),
      }])),
      quality,
    };
    if ((this.alignStatus === "ready" || this.alignStatus === "collecting") && this.alignCamera !== null) {
      if (label !== null && label !== this.alignCamera.label) this.invalidateAlignment("camera_label");
      else if (width !== this.alignCamera.width || height !== this.alignCamera.height) {
        this.invalidateAlignment("camera_size");
      }
    }
    if (label !== null && this.frameCamera !== null && label !== this.frameCamera) {
      // A camera source change cannot keep an attempt in an unrelated image plane.
      if (this.game !== null) {
        this.changed("Cambió la cámara: el intento se descartó.");
        this.log("attempt_discarded", { reason: "camera_label" });
      }
      this.board = null;
      this.game = null;
      this.boardWidth = 0;
      this.boardHeight = 0;
      this.paused = false;
      this.lastTick = null;
      this.accumulator = 0.0;
    }
    const [cameraBoard, candidate, error] = this.candidateFor(notes, width, height);
    preview.quality.ready = cameraBoard !== null;
    const previous = this.cameraBoard;
    this.liveDetection = preview;
    this.liveDetectionAt = this.clock();
    this.lastNotes = [notes, width, height];
    this.frameWidth = width;
    this.frameHeight = height;
    if (label !== null) this.frameCamera = label;
    this.cameraBoard = cameraBoard;
    this.candidate = candidate;
    // An attempt keeps its collision geometry, including while paused or
    // displaying its result. Only the overlay follows the moving notes.
    if (this.game === null || this.game.status === GameStatus.READY) this.applyCandidate();
    if (previous === null && cameraBoard !== null) {
      this.changed("Post-its detectados. Listo para lanzar.");
      this.log("live_board_ready", { bumpers: cameraBoard.bumpers.length, camera_width: width,
        camera_height: height, space: candidate[1] });
    } else if (previous !== null && cameraBoard === null) {
      this.changed(error || "No se detecta un tablero válido.");
      this.log("live_board_invalid", { reason: error });
    } else {
      this.revision += 1;
    }
    return [preview, error];
  }

  /** Publish every visible note and update the next playable board. */
  preview(frame, camera = null, ticket = null) {
    const label = this.scanLabel(camera);
    const { notes, quality } = this.measureOrRefuse(frame);
    if (ticket && (ticket.epoch !== this.cameraEpoch || ticket.palette !== this.palette)) {
      // The camera or the colors changed while this frame was decoded: its
      // notes belong to the old ones and must not replace the cleared view.
      return JSON.parse(JSON.stringify({ width: frame.cols, height: frame.rows,
        roles: Object.fromEntries(ROLES.map((role) => [role, { count: notes[role].length,
          expected: LIVE_EXPECTED_NOTES[role], notes: notes[role].map(polygonJson) }])),
        quality: { ...quality, ready: false } }));
    }
    this.expireDetection();
    const [preview] = this.storeFrame(notes, quality, frame.cols, frame.rows, label);
    return JSON.parse(JSON.stringify(preview));
  }

  /** Check the camera label before decoding, as the Python server does. */
  scanLabel(camera) {
    if (camera === null || camera === undefined) return null;
    try {
      return cameraLabel(camera);
    } catch (error) {
      throw new RequestError(error.message);
    }
  }

  /** Optional explicit refresh/reset, without blackout or calibration. */
  scan(frame, camera = null, ticket = null) {
    const label = this.scanLabel(camera);
    const running = () => this.game !== null && this.game.status === GameStatus.RUNNING && !this.paused;
    if (running()) throw new RequestError("Terminá o pausá el intento antes de escanear.", HTTP.CONFLICT);
    const { notes, quality } = this.measureOrRefuse(frame);
    if (ticket && ticket.palette !== this.palette) {
      throw new RequestError("Los colores cambiaron durante la lectura. Volvé a escanear.", HTTP.CONFLICT);
    }
    if (ticket && ticket.epoch !== this.cameraEpoch) {
      throw new RequestError("La cámara cambió durante la lectura. Volvé a escanear.", HTTP.CONFLICT);
    }
    this.expireDetection();
    const [, error] = this.storeFrame(notes, quality, frame.cols, frame.rows, label);
    if (this.cameraBoard === null || this.candidate === null) {
      throw new RequestError(error || "No se detecta un tablero válido.");
    }
    this.applyCandidate();
    this.paused = false;
    this.accumulator = 0.0;
    this.lastTick = null;
    this.liveMode();
    this.changed("Tablero actualizado con la detección en vivo.");
    this.log("board_scanned", { bumpers: this.board.bumpers.length,
      camera_width: this.frameWidth, camera_height: this.frameHeight });
    return this.snapshot();
  }

  // ---- alignment

  /** Decode the 14 pattern frames (contract 1.3) and adopt or reject H. */
  solveAlignment() {
    const result = decodeAlignment(this.requireCv(), this.alignFrames, this.alignLayout);
    this.alignFrames = new Map();
    this.projectionMode = "live";
    if (!result.ok) {
      const { reason } = result;
      this.dropAlignment("failed", reason);
      this.alignCode = result.code;
      this.changed(reason);
      this.log("alignment_failed", { code: result.code, reason, decoded: result.decoded, total: result.total,
        inliers: result.inliers, holdout_p95_px: round3(result.holdout_p95_px), coverage: round3(result.coverage),
        rejected: result.rejected });
      return;
    }
    const camera = this.alignCamera;
    this.alignStatus = "ready";
    this.alignLayout = null;
    this.alignMatrix = result.matrix.map((row) => row.map(Number));
    this.alignCamera = camera;
    this.alignProjector = { width: this.projectorWidth, height: this.projectorHeight };
    this.alignQuality = Object.fromEntries(["decoded", "total", "fit", "inliers", "holdout", "holdout_p95_px",
      "holdout_max_px", "outliers", "coverage"].map((key) => [key, round3(result[key])]));
    const probes = placeProbes(this.liveDetection, this.alignMatrix, [this.projectorWidth, this.projectorHeight],
      [camera.width, camera.height]);
    this.overlay = overlayState("unchecked", probes);
    this.checkFrames = new Map();
    this.holdDetection();   // the overlay check comes next
    const detail = `Proyector alineado: error ${pyRound(result.holdout_p95_px, 1).toFixed(1)} px ` +
      `con ${result.decoded} puntos.`;
    this.alignDetail = detail;
    this.recomputeCandidate();
    this.changed(detail);
    this.log("alignment_ready", { ...this.alignQuality, probes: probes.length,
      width: this.projectorWidth, height: this.projectorHeight });
  }

  /** Store one pattern frame of a run in progress (gray, at most 14). */
  alignFrame(frame, align, camera = null) {
    this.scanLabel(camera);
    const [bit, inverse] = alignKey(align);
    const cv = this.requireCv();
    const gray = new cv.Mat();
    let image;
    try {
      cv.cvtColor(frame, gray, cv.COLOR_BGR2GRAY);
      image = copyFrame(gray);
    } finally {
      gray.delete();
    }
    if (this.alignStatus !== "collecting") throw new RequestError("No hay una alineación en curso.", HTTP.CONFLICT);
    if (image.width !== this.frameWidth || image.height !== this.frameHeight) {
      throw new RequestError("El cuadro de alineación no coincide con el tamaño de la cámara.");
    }
    this.alignFrames.set(frameKey(bit, inverse), image);
    this.alignActivity = this.clock();
    this.holdDetection();
    this.revision += 1;
    return { received: this.alignFrames.size, needed: ALIGN_NEEDED };
  }

  /** The `X-Postit-Check` kind, checked before decoding as Python does. */
  checkKind(kind) {
    if (!CHECK_KINDS.includes(kind)) throw new RequestError("Cabecera X-Postit-Check inválida.");
    return kind;
  }

  /** Store the neutral or the overlay frame of the overlay check. */
  checkFrame(frame, kind) {
    this.checkKind(kind);
    if (this.alignStatus !== "ready") throw new RequestError("Primero alineá el proyector.", HTTP.CONFLICT);
    if (frame.cols !== this.alignCamera.width || frame.rows !== this.alignCamera.height) {
      throw new RequestError("El cuadro de verificación no coincide con el tamaño de la cámara.");
    }
    this.checkFrames.set(kind, copyFrame(frame));
    this.holdDetection();
    return { received: [...this.checkFrames.keys()].sort() };
  }

  /** Contract 2.3: does the colored fill change detection, or look like a note? */
  solveOverlayCheck() {
    if (this.alignStatus !== "ready") throw new RequestError("Primero alineá el proyector.", HTTP.CONFLICT);
    if (!CHECK_KINDS.every((kind) => this.checkFrames.has(kind))) {
      throw new RequestError("Faltan los cuadros de verificación.", HTTP.CONFLICT);
    }
    const cv = this.requireCv();
    const stored = { neutral: this.checkFrames.get("neutral"), overlay: this.checkFrames.get("overlay") };
    this.checkFrames = new Map();
    const detect = (image) => {
      const mat = new cv.Mat(image.height, image.width, cv.CV_8UC3);
      try {
        mat.data.set(image.data);
        return findNotes(cv, mat, this.palette);
      } finally {
        mat.delete();
      }
    };
    let neutral;
    let overlay;
    try {
      neutral = detect(stored.neutral);
      overlay = detect(stored.overlay);
    } catch (error) {
      if (!(error instanceof Error) || error instanceof TypeError) throw error;
      throw new RequestError(error.message);
    }
    const { probes } = this.overlay;
    const half = probeSide(this.projectorWidth, this.projectorHeight) / 2;
    const boxes = probes.map((probe) => [probe.role, [
      Math.min(...probe.points.map((p) => p.x)) - half, Math.min(...probe.points.map((p) => p.y)) - half,
      Math.max(...probe.points.map((p) => p.x)) + half, Math.max(...probe.points.map((p) => p.y)) + half]]);
    const hitRoles = new Set();
    const kept = {};
    for (const role of ROLES) {
      kept[role] = [];
      for (const polygon of overlay[role]) {
        const cx = mean(polygon.map((point) => point.x));
        const cy = mean(polygon.map((point) => point.y));
        const mapped = mapPoint({ x: cx, y: cy }, this.alignMatrix);
        const hits = boxes.filter(([, box]) => mapped !== null && box[0] <= mapped.x && mapped.x <= box[2] &&
          box[1] <= mapped.y && mapped.y <= box[3]).map(([probeRole]) => probeRole);
        if (hits.length) hits.forEach((hit) => hitRoles.add(hit));
        else kept[role].push(polygon);
      }
    }
    const rolesLog = {};
    let failing = null;
    for (const role of ROLES) {
      const before = neutral[role].length;
      const after = kept[role].length;
      const minIou = matchRole(cv, neutral[role], kept[role]);
      rolesLog[role] = { neutral: before, overlay: after, min_iou: minIou !== null ? pyRound(minIou, 3) : null };
      if (failing === null && (before !== after || (minIou !== null && minIou < OVERLAY_MIN_IOU))) {
        failing = [role, before, after];
      }
    }
    const probesHit = ROLES.filter((role) => hitRoles.has(role));
    if (probesHit.length) {
      const detail = "La cámara toma el color proyectado sobre la pared por un post-it de " +
        `${ROLE_LABELS[probesHit[0]]}; se proyectan solo bordes.`;
      this.overlay = overlayState("fill_off", probes, detail);
    } else if (failing !== null) {
      const [role, before, after] = failing;
      const what = before !== after ? `${ROLE_LABELS[role]}: ${before} → ${after}` : `${ROLE_LABELS[role]}: forma`;
      this.overlay = overlayState("fill_off", probes,
        `El color proyectado cambiaba la detección (${what}); se proyectan solo bordes.`);
    } else {
      this.overlay = overlayState("ok", probes);
    }
    this.changed(this.overlay.detail);
    this.log("overlay_check", { result: this.overlay.status, fill: this.overlay.fill, probes_hit: probesHit,
      roles: rolesLog });
  }

  tickUnlocked() {
    if (this.game === null || this.board === null || this.game.status !== GameStatus.RUNNING ||
        this.paused || !this.projectorOpen) return;
    const now = this.clock();
    if (this.lastTick === null) {
      this.lastTick = now;
      return;
    }
    const elapsed = now - this.lastTick;
    this.lastTick = now;
    if (elapsed < 0 || elapsed > 0.25) {
      this.accumulator = 0.0;
      this.log("simulation_paused", { gap_s: pyRound(elapsed, 3) });
      return;
    }
    this.accumulator += elapsed;
    const oldStatus = this.game.status;
    while (this.accumulator >= 1 / 120 && this.game.status === GameStatus.RUNNING) {
      this.game = step(this.board, this.game, 1 / 120);
      this.accumulator -= 1 / 120;
    }
    if (this.game.status !== oldStatus) {
      this.changed(this.game.status === GameStatus.WON ? "¡Llegaste!" : "La pelota salió del campo.");
      this.log("attempt_finished", { result: this.game.status, elapsed_s: pyRound(this.game.elapsed_s, 3),
        bounces: this.game.bounce_count });
    }
  }

  tick() {
    this.tickUnlocked();
    return this.snapshot();
  }
}
