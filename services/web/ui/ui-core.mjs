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
