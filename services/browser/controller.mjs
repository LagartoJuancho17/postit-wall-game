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
 * needed for scans, previews and calibration only, and is passed in.
 */

import { estimateHomography, quadrilateralCenter, transformBoard } from "./calibration.mjs";
import { isPlainObject, pyJsonLength, pyMod, pyRound, pyTruthy } from "./numbers.mjs";
import { buildPalette, DEFAULT_PALETTE, normalizeHex, paletteJson, ROLE_LABELS, ROLES } from "./palette.mjs";
import { GameStatus, launch, readyState, step } from "./physics.mjs";
import { detectBoard, EXPECTED_NOTES, inspectFrame } from "./vision.mjs";

const MARKER_LABELS = ["superior izquierdo", "superior derecho", "inferior derecho", "inferior izquierdo"];
const START_TARGET_FRACTIONS = [[0.2, 0.2], [0.8, 0.2], [0.8, 0.8], [0.2, 0.8]];
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

// ---- camera.py

export function cameraLabel(value) {
  if (typeof value !== "string" || !value.trim() || value.length > MAX_LABEL) {
    throw new Error(`camera debe ser el nombre de la cámara (1 a ${MAX_LABEL} caracteres).`);
  }
  return value.trim();
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

/** The mapped board, validated at the web runtime's service boundary. */
function prepareProjectorBoard(cameraBoard, matrix, width, height) {
  const board = transformBoard(cameraBoard, matrix);
  const polygons = [board.playfield, board.start, board.goal, ...board.bumpers];
  if (!polygons.every((polygon) => polygon.every((p) => p.x >= 0 && p.x < width && p.y >= 0 && p.y < height))) {
    throw new Error("Hay post-its fuera del área proyectada. Recalibrá el montaje.");
  }
  readyState(board);
  return board;
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
    this.matrix = null;
    this.game = null;
    this.projectorOpen = false;
    this.projectorWidth = 0;
    this.projectorHeight = 0;
    this.projectionMode = "black";
    this.calibrationTargets = [];
    this.calibrationTarget = null;
    this.aimDegrees = -45.0;
    this.attempts = 0;
    this.paused = false;
    this.lastTick = null;
    this.accumulator = 0.0;
    this.frameWidth = 0;
    this.frameHeight = 0;
    this.frameCamera = null;
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
    if (this.calibrationTarget !== null) return "calibrating";
    if (this.game !== null) {
      if (this.paused && this.game.status === GameStatus.RUNNING) return "paused";
      return this.game.status;
    }
    if (this.cameraBoard !== null) return "scanned";
    return "waiting_camera";
  }

  snapshot() {
    const { game } = this;
    let target = null;
    if (this.calibrationTarget !== null) {
      target = { ...pointJson(this.calibrationTarget), index: this.calibrationTargets.length + 1,
        label: MARKER_LABELS[this.calibrationTargets.length] };
    }
    return {
      revision: this.revision,
      phase: this.phase(),
      message: this.message,
      camera: { status: "browser", width: this.frameWidth, height: this.frameHeight, label: this.frameCamera },
      camera_board: boardJson(this.cameraBoard),
      board: boardJson(this.board),
      palette: this.paletteJson(),
      camera_presets: JSON.parse(JSON.stringify(this.cameraPresets)),
      calibration: {
        status: this.calibrationTarget !== null ? "collecting" : this.matrix !== null ? "ready" : "none",
        index: this.calibrationTargets.length,
        total: 4,
        detail: this.calibrationTarget !== null ? "Alineá la cruz con cada post-it de esquina."
          : this.matrix !== null ? "Manual: comprobá la alineación de los contornos." : "Sin calibrar",
        error_px: null,
      },
      game: {
        status: game ? game.status : "none",
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
        target,
      },
    };
  }

  newTarget() {
    const [fx, fy] = START_TARGET_FRACTIONS[this.calibrationTargets.length];
    this.calibrationTarget = { x: this.projectorWidth * fx, y: this.projectorHeight * fy };
  }

  projectorPoint(payload) {
    if (this.calibrationTarget === null) throw new RequestError("Primero iniciá la calibración.", HTTP.CONFLICT);
    const x = finiteNumber(payload.x ?? null, "x");
    const y = finiteNumber(payload.y ?? null, "y");
    if (!(x >= 0 && x < this.projectorWidth && y >= 0 && y < this.projectorHeight)) {
      throw new RequestError("La cruz debe quedar dentro de la ventana del proyector.");
    }
    return { x, y };
  }

  resetBoard() {
    this.cameraBoard = null;
    this.board = null;
    this.matrix = null;
    this.game = null;
    this.frameWidth = 0;
    this.frameHeight = 0;
    this.frameCamera = null;
    this.calibrationTargets = [];
    this.calibrationTarget = null;
    this.paused = false;
    this.projectionMode = "black";
  }

  restoredMode() {
    return this.calibrationTarget ? "calibration" : this.board ? "game" : "black";
  }

  action(payload) {
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
      if (changedSize) {
        this.resetBoard();
        this.changed("Cambió el tamaño del proyector. Escaneá y calibrá otra vez.");
        this.log("projection_resized", { width, height });
      } else {
        this.projectionMode = this.restoredMode();
        this.changed("Ventana del proyector abierta.");
      }
    } else if (name === "camera_changed") {
      this.resetBoard();
      this.lastTick = null;
      this.accumulator = 0.0;
      this.changed("Cambió la cámara. Escaneá y calibrá otra vez.");
      this.log("camera_changed");
    } else if (name === "projection_close") {
      this.projectorOpen = false;
      this.projectionMode = "black";
      if (running()) this.paused = true;
      this.lastTick = null;
      this.changed("Ventana del proyector cerrada. La partida quedó pausada.");
    } else if (name === "projection_black") {
      if (running() && !this.paused) {
        throw new RequestError("Terminá o pausá el intento antes de escanear.", HTTP.CONFLICT);
      }
      this.projectionMode = "black";
      this.changed("Proyección en negro. Ahora podés capturar la pared.");
    } else if (name === "projection_restore") {
      // After a blackout that produced no new board (a failed scan or a
      // camera search), show again what the projector was showing.
      if (this.projectorOpen) {
        this.projectionMode = this.restoredMode();
        this.revision += 1;
      }
    } else if (name === "calibrate") {
      if (!this.projectorOpen) throw new RequestError("Abrí la ventana del proyector con J.", HTTP.CONFLICT);
      if (this.cameraBoard === null) {
        throw new RequestError("Primero escaneá los post-its con la proyección negra.", HTTP.CONFLICT);
      }
      if (running() && !this.paused) {
        throw new RequestError("Terminá o pausá el intento antes de recalibrar.", HTTP.CONFLICT);
      }
      this.matrix = null;
      this.board = null;
      this.game = null;
      this.paused = false;
      this.calibrationTargets = [];
      this.newTarget();
      this.projectionMode = "calibration";
      this.changed("Alineá la cruz 1/4 con el post-it de esquina superior izquierdo.");
      this.log("calibration_started", { mode: "manual" });
    } else if (name === "calibration_move") {
      this.calibrationTarget = this.projectorPoint(payload);
      this.revision += 1;
    } else if (name === "calibration_confirm") {
      const point = "x" in payload || "y" in payload ? this.projectorPoint(payload) : this.calibrationTarget;
      if (point === null || this.cameraBoard === null) {
        throw new RequestError("Primero iniciá la calibración.", HTTP.CONFLICT);
      }
      const targets = [...this.calibrationTargets, point];
      if (targets.length === 4) {
        let matrix;
        let board;
        try {
          const cameraPoints = this.cameraBoard.markers.map(quadrilateralCenter);
          matrix = estimateHomography(this.requireCv(), cameraPoints, targets);
          board = prepareProjectorBoard(this.cameraBoard, matrix, this.projectorWidth, this.projectorHeight);
        } catch (error) {
          if (error instanceof RequestError) throw error;
          throw new RequestError(`Calibración inválida: ${error.message}`);
        }
        this.matrix = matrix;
        this.board = board;
        this.game = readyState(board);
        this.calibrationTargets = [];
        this.calibrationTarget = null;
        this.projectionMode = "game";
        this.accumulator = 0.0;
        this.lastTick = null;
        this.changed("Calibración lista. Revisá los contornos sobre la pared.");
        this.log("calibration_completed", { mode: "manual", bumpers: board.bumpers.length,
          width: this.projectorWidth, height: this.projectorHeight });
      } else {
        this.calibrationTargets = targets;
        this.newTarget();
        this.changed(`Alineá la cruz ${targets.length + 1}/4 con el post-it de esquina ${MARKER_LABELS[targets.length]}.`);
      }
    } else if (name === "aim" || name === "aim_left" || name === "aim_right") {
      if (this.game === null || this.game.status !== GameStatus.READY) {
        throw new RequestError("La dirección solo se cambia antes del lanzamiento.", HTTP.CONFLICT);
      }
      const aim = name === "aim" ? finiteNumber(payload.degrees ?? null, "degrees")
        : this.aimDegrees + (name === "aim_left" ? -5 : 5);
      this.aimDegrees = pyMod(aim + 180, 360) - 180;
      this.revision += 1;
    } else if (name === "launch") {
      if (!this.projectorOpen || this.board === null || this.game === null) {
        throw new RequestError("Abrí el proyector, escaneá y calibrá antes de lanzar.", HTTP.CONFLICT);
      }
      if (this.game.status !== GameStatus.READY) {
        throw new RequestError("Reiniciá el intento antes de volver a lanzar.", HTTP.CONFLICT);
      }
      const speed = Math.min(this.projectorWidth, this.projectorHeight) * 0.4;
      this.game = launch(this.board, this.aimDegrees, speed);
      this.attempts += 1;
      this.paused = false;
      this.lastTick = this.clock();
      this.accumulator = 0.0;
      this.projectionMode = "game";
      this.changed("Pelota lanzada.");
      this.log("attempt_started", { attempt: this.attempts, angle_degrees: pyRound(this.aimDegrees, 1),
        speed_px_s: pyRound(speed, 1) });
    } else if (name === "reset") {
      if (this.board === null) throw new RequestError("Primero escaneá y calibrá el tablero.", HTTP.CONFLICT);
      this.game = readyState(this.board);
      this.paused = false;
      this.lastTick = null;
      this.accumulator = 0.0;
      this.projectionMode = this.projectorOpen ? "game" : "black";
      this.changed("Intento reiniciado. Podés mover post-its y volver a escanear.");
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
      this.changed(`Color de ${ROLE_LABELS[role]} actualizado.` +
        (this.cameraBoard !== null ? " Escaneá otra vez para aplicarlo al tablero." : ""));
      this.log("palette_changed", { role, hex: rule.hex, hue_low: rule.hue_low, hue_high: rule.hue_high });
    } else if (name === "palette_reset") {
      this.paletteChoices = Object.fromEntries(ROLES.map((r) => [r, null]));
      this.palette = DEFAULT_PALETTE;
      this.savePalette();
      this.changed("Colores originales restaurados." +
        (this.cameraBoard !== null ? " Escaneá otra vez para aplicarlos al tablero." : ""));
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
      if (!this.projectorOpen || this.game === null || this.game.status !== GameStatus.RUNNING || !this.paused) {
        throw new RequestError("No hay un intento pausado o falta el proyector.", HTTP.CONFLICT);
      }
      this.paused = false;
      this.lastTick = this.clock();
      this.projectionMode = "game";
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

  /** Live detection for the camera panel; never changes the game. */
  preview(frame) {
    let result;
    try {
      result = inspectFrame(this.requireCv(), frame, this.palette);
    } catch (error) {
      throw new RequestError(error.message);
    }
    return {
      width: frame.cols,
      height: frame.rows,
      roles: Object.fromEntries(ROLES.map((role) => [role, {
        count: result.notes[role].length, expected: EXPECTED_NOTES[role],
        notes: result.notes[role].map(polygonJson),
      }])),
      quality: result.quality,
    };
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

  /** Read the board; `camera` names the camera, so another camera discards it. */
  scan(frame, camera = null) {
    const label = this.scanLabel(camera);
    if (!this.projectorOpen) {
      throw new RequestError("Abrí la ventana del proyector con J antes de escanear.", HTTP.CONFLICT);
    }
    if (this.projectionMode !== "black") {
      throw new RequestError("Poné la proyección en negro antes de escanear.", HTTP.CONFLICT);
    }
    if (this.game !== null && this.game.status === GameStatus.RUNNING && !this.paused) {
      throw new RequestError("Terminá o pausá el intento antes de escanear.", HTTP.CONFLICT);
    }
    let cameraBoard;
    let board;
    let game;
    try {
      cameraBoard = detectBoard(this.requireCv(), frame, this.palette);
      board = this.matrix !== null
        ? prepareProjectorBoard(cameraBoard, this.matrix, this.projectorWidth, this.projectorHeight) : null;
      game = board !== null ? readyState(board) : null;
    } catch (error) {
      throw new RequestError(error.message);
    }
    this.cameraBoard = cameraBoard;
    this.board = board;
    this.game = game;
    this.frameHeight = frame.rows;
    this.frameWidth = frame.cols;
    this.frameCamera = label;
    this.paused = false;
    this.accumulator = 0.0;
    this.lastTick = null;
    this.calibrationTargets = [];
    this.calibrationTarget = null;
    this.projectionMode = board !== null ? "game" : "black";
    this.changed(board === null ? "Tablero leído. Iniciá la calibración." : "Tablero actualizado. Listo para otro intento.");
    this.log("board_scanned", { bumpers: cameraBoard.bumpers.length, calibrated: board !== null,
      camera_width: this.frameWidth, camera_height: this.frameHeight });
    return this.snapshot();
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
