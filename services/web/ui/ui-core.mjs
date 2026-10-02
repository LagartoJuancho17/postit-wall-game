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
  ready: { label: "Listo", hint: "Ajustá la dirección y lanzá desde el post-it de inicio." },
  running: { label: "En juego", hint: "La pelota está en movimiento." },
  paused: {
    label: "En pausa",
    hint: "Intento pausado. Con la proyección abierta podés continuarlo, releer los post-its o empezar otro.",
  },
  won: { label: "Llegada", hint: "Tocaste la llegada. Podés iniciar otro intento." },
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
  scan: "Siguiente: escaneá con S. Tienen que verse 4 esquinas, 1 inicio y 1 llegada (los rebotes son opcionales): mirá las luces debajo del video.",
  calibrate: "Siguiente: calibrá las 4 esquinas con C.",
  align: "En la ventana del proyector, llevá la cruz al centro de cada post-it de esquina y confirmá con Enter.",
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

/** Post-it roles in panel order, and how many a playable wall needs. */
export const PALETTE_ROLES = ["start", "goal", "bumper", "marker"];
export const ROLE_LABELS = { start: "inicio", goal: "llegada", bumper: "rebotes", marker: "esquinas" };
const NEEDED = { start: 1, goal: 1, bumper: null, marker: 4 };

/**
 * Detection light per role from a /api/preview response: "on" when the
 * camera sees what the scan needs (any bumper counts), "warn" when it sees
 * some but the wrong number, "off" when it sees none, "" before any preview.
 */
export function roleLights(preview) {
  return Object.fromEntries(PALETTE_ROLES.map((role) => {
    const count = preview?.roles?.[role]?.count;
    if (!Number.isInteger(count)) return [role, { state: "", count: null, text: "—" }];
    const needed = NEEDED[role];
    const state = count === 0 ? "off" : needed === null || count === needed ? "on" : "warn";
    return [role, { state, count, text: needed === null ? String(count) : `${count}/${needed}` }];
  }));
}

/** Where a frame lands inside a box with object-fit: contain. */
export function containTransform(boxWidth, boxHeight, frameWidth, frameHeight) {
  const scale = Math.min(boxWidth / frameWidth, boxHeight / frameHeight);
  return {
    scale,
    offsetX: (boxWidth - frameWidth * scale) / 2,
    offsetY: (boxHeight - frameHeight * scale) / 2,
  };
}

/** Frame pixel under a click on the camera stage; null on the letterbox bars. */
export function framePointFromClick(clientX, clientY, rect, frameWidth, frameHeight) {
  if (!(frameWidth > 0 && frameHeight > 0 && rect?.width > 0 && rect?.height > 0)) return null;
  const { scale, offsetX, offsetY } = containTransform(rect.width, rect.height, frameWidth, frameHeight);
  const x = (clientX - rect.left - offsetX) / scale;
  const y = (clientY - rect.top - offsetY) / scale;
  return x >= 0 && y >= 0 && x < frameWidth && y < frameHeight ? { x, y } : null;
}

/** Top-left corner of a size×size patch around a point, kept inside the frame. */
export function patchOrigin(point, frameWidth, frameHeight, size = 5) {
  const half = Math.floor(size / 2);
  const clamp = (value, top) => Math.max(0, Math.min(top - size, Math.round(value) - half));
  return { x: clamp(point.x, frameWidth), y: clamp(point.y, frameHeight) };
}

/** Mean color of RGBA pixels as #rrggbb: what the eyedropper reads. */
export function averageHex(rgba) {
  const pixels = rgba?.length / 4;
  if (!Number.isInteger(pixels) || pixels < 1) throw new TypeError("Se esperaban píxeles RGBA.");
  const sum = [0, 0, 0];
  for (let index = 0; index < rgba.length; index += 4) {
    sum[0] += rgba[index];
    sum[1] += rgba[index + 1];
    sum[2] += rgba[index + 2];
  }
  return `#${sum.map((total) => Math.round(total / pixels).toString(16).padStart(2, "0")).join("")}`;
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
