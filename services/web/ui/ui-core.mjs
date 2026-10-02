/** Pure view logic shared by the operator page and its tests. */

export function preferredCamera(devices) {
  const cameras = devices.filter((device) => device.kind === "videoinput");
  return cameras.find((device) => /c920|logitech/i.test(device.label)) ?? cameras[0] ?? null;
}

export function cameraName(device, index = 0) {
  return device?.label?.trim() || `Cámara ${index + 1}`;
}

export function boardCounts(state) {
  const board = state?.camera_board ?? state?.board;
  return {
    start: board?.start ? 1 : 0,
    goal: board?.goal ? 1 : 0,
    bumpers: board?.bumpers?.length ?? 0,
    markers: board?.markers?.length ?? 0,
  };
}

const ATTEMPT_VIEWS = {
  none: { label: "Sin tablero", hint: "Escaneá y calibrá antes de lanzar." },
  ready: { label: "Listo", hint: "Ajustá la dirección y lanzá desde el amarillo." },
  running: { label: "En juego", hint: "La pelota está en movimiento." },
  paused: {
    label: "En pausa",
    hint: "Intento pausado. Con la proyección abierta podés continuarlo, releer los post-its o empezar otro.",
  },
  won: { label: "Llegada", hint: "Llegaste al naranja. Podés iniciar otro intento." },
  lost: { label: "Fuera", hint: "La pelota salió del campo. Podés reintentar." },
};

/** The backend keeps status "running" while paused; the panel treats it apart. */
export function attemptPhase(game) {
  const status = game?.status ?? "none";
  return status === "running" && game?.paused ? "paused" : status;
}

export function attemptView(game) {
  const phase = attemptPhase(game);
  return { phase, ...(ATTEMPT_VIEWS[phase] ?? { label: phase, hint: "" }) };
}

export function controlsFor(state, cameraReady, popupReady) {
  const phase = attemptPhase(state?.game);
  const moving = phase === "running";
  const popup = Boolean(popupReady);
  const live = Boolean(cameraReady) && popup;
  const hasCameraBoard = Boolean(state?.camera_board);
  const calibrated = state?.calibration?.status === "ready";
  return {
    scan: live && !moving,
    calibrate: live && hasCameraBoard && !moving,
    launch: live && calibrated && phase === "ready",
    reset: popup && phase !== "none",
    aim: popup && phase === "ready",
    resume: popup && phase === "paused",
  };
}

/** Setup order of the page's step indicator (data-step in index.html). */
export const SETUP_STAGES = ["camera", "projector", "scan", "play"];

const SETUP_GUIDES = {
  camera: "Primero la cámara: permití el acceso en Chrome o elegí la C920 debajo del video.",
  projector: "Siguiente: abrí la proyección con J (botón de arriba a la derecha) y llevá esa ventana al proyector.",
  scan: "Siguiente: escaneá con S. Tienen que verse 4 fucsias, 1 amarillo y 1 naranja; los verdes son opcionales.",
  calibrate: "Siguiente: calibrá las 4 esquinas con C.",
  align: "En la ventana del proyector, llevá la cruz al centro de cada fucsia y confirmá con Enter.",
  play: "La proyección se apaga un instante al escanear.",
};
const STAGE_OF = { camera: "camera", projector: "projector", scan: "scan", calibrate: "scan", align: "scan", play: "play" };

/**
 * What unblocks the operator next, from the same inputs as controlsFor.
 * Camera and projector are browser-side: the backend cannot see them, so
 * its message must not hide them (see statusText).
 */
export function setupGuide(state, cameraReady, popupReady) {
  let step = "play";
  if (!cameraReady) step = "camera";
  else if (!popupReady) step = "projector";
  else if (state?.calibration?.status === "collecting") step = "align";
  else if (!state?.camera_board) step = "scan";
  else if (state?.calibration?.status !== "ready") step = "calibrate";
  return {
    step,
    stage: STAGE_OF[step],
    message: SETUP_GUIDES[step],
    blocking: step === "camera" || step === "projector",
  };
}

/** A local notice wins, then a browser-side blocker, then the backend. */
export function statusText(notice, state, guide) {
  return notice?.message
    || (guide?.blocking ? guide.message : state?.message)
    || "Activá la cámara y abrí la proyección para comenzar.";
}

export function isTypingTarget(target) {
  if (!target || typeof target.closest !== "function") return false;
  return Boolean(target.closest("input:not([type='range']), select, textarea, [contenteditable='true']"));
}

export function isProjectorShortcut(event) {
  return event.key?.toLowerCase() === "j"
    && !event.repeat
    && !event.altKey
    && !event.ctrlKey
    && !event.metaKey
    && !isTypingTarget(event.target);
}

export function pointInCanvas(clientX, clientY, rect, width, height) {
  const x = Math.max(0, Math.min(width - 1, (clientX - rect.left) * width / rect.width));
  const y = Math.max(0, Math.min(height - 1, (clientY - rect.top) * height / rect.height));
  return { x, y };
}

export function normalizeAngle(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) throw new TypeError("Ángulo no válido");
  return Math.max(-180, Math.min(180, number));
}

export function projectionSize(win) {
  return {
    width: Math.max(1, Math.floor(win.innerWidth)),
    height: Math.max(1, Math.floor(win.innerHeight)),
  };
}
