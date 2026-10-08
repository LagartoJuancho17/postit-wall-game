/** Pure view logic shared by the operator page and its tests. */

/** The operator's last chosen camera (by label), else the Logitech C920, else the first. */
export function preferredCamera(devices, remembered = "") {
  const cameras = devices.filter((device) => device.kind === "videoinput");
  return cameras.find((device) => remembered && device.label === remembered)
    ?? cameras.find((device) => /c920|logitech/i.test(device.label))
    ?? cameras[0] ?? null;
}

export const CAMERA_KEY = "postit-wall:camera";

/** An iPhone used as a webcam through Continuity Camera. */
export function isPhoneCamera(label) {
  return /iphone|continuidad|continuity/i.test(label ?? "");
}

/**
 * Wait after the projector goes black before capturing a scan. A fixed
 * camera only needs the projector to settle; an automatic one also has to
 * let its exposure and white balance adapt to the dark wall.
 */
export function scanSettleMs(cameraFixed) {
  return cameraFixed ? 180 : 1000;
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

/**
 * Camera controls the panel fixes by hand. Each pairs a mode with the value
 * it fixes; extras have no automatic mode but are pinned so another app
 * cannot leave them changed. Units follow the Image Capture spec:
 * exposureTime in 100 µs, colorTemperature in kelvin.
 */
export const CAMERA_GROUPS = [
  { key: "whiteBalance", mode: "whiteBalanceMode", value: "colorTemperature", label: "balance de blancos" },
  { key: "exposure", mode: "exposureMode", value: "exposureTime", label: "exposición" },
  { key: "focus", mode: "focusMode", value: "focusDistance", label: "foco" },
];
const CAMERA_EXTRAS = ["brightness", "contrast", "saturation", "sharpness", "gain"];
const CAMERA_MODES = ["whiteBalanceMode", "exposureMode", "focusMode"];

/** USB vendor and product ids Chrome appends to a camera label: "C920 (046d:082d)". */
export function usbIdsFromLabel(label) {
  const match = /\(([0-9a-f]{4}):([0-9a-f]{4})\)/i.exec(label ?? "");
  return match ? { vendor: match[1].toLowerCase(), product: match[2].toLowerCase() } : null;
}

/** Modes a saved preset fixes by hand that the camera has back on automatic. */
export function driftedModes(preset, current) {
  return CAMERA_MODES.filter((mode) => preset?.[mode] === "manual" && current?.[mode] !== "manual");
}

/** The advanced sets as one settings object, for the USB route. */
export function mergeSets(sets) {
  return Object.assign({}, ...sets);
}
export const SEARCH_EXPOSURE = [50, 100, 200, 400, 800]; // 5 to 80 ms; most are 10 ms multiples (50 Hz)
export const SEARCH_TEMPERATURE = [3000, 4000, 5000, 6000];
const TARGET_BRIGHTNESS = 0.62;

const isRange = (range) => Number.isFinite(range?.min) && Number.isFinite(range?.max) && range.max >= range.min;

/**
 * What Chrome lets the panel do with each control (from getCapabilities()).
 * lock: a "manual" mode exists, enough to switch the automatic off and
 * freeze the current value. search: there is also a value range, so values
 * can be tried and set.
 */
export function cameraSupport(caps) {
  const groups = Object.fromEntries(CAMERA_GROUPS.map((group) => {
    const lock = Array.isArray(caps?.[group.mode]) && caps[group.mode].includes("manual");
    return [group.key, { lock, search: lock && isRange(caps?.[group.value]) }];
  }));
  return {
    ...groups,
    any: CAMERA_GROUPS.some((group) => groups[group.key].lock),
    searchable: CAMERA_GROUPS.some((group) => groups[group.key].search),
  };
}

/** Per control, what Chrome reported: for the operator and the log. */
export function cameraDiagnosis(caps) {
  return CAMERA_GROUPS.map((group) => {
    const modes = Array.isArray(caps?.[group.mode]) ? caps[group.mode].join("/") : "no informado";
    const range = isRange(caps?.[group.value]) ? `${caps[group.value].min}–${caps[group.value].max}` : "sin rango";
    return `${group.label}: modos ${modes}, ${range}`;
  }).join(" · ");
}

/** A value clamped into a capability range and onto its step. */
export function snap(value, range) {
  const clamp = (number) => Math.min(range.max, Math.max(range.min, number));
  let result = clamp(value);
  if (range.step > 0) result = clamp(range.min + Math.round((result - range.min) / range.step) * range.step);
  return Number(result.toFixed(6));
}

/**
 * applyConstraints() advanced sets that fix these values. One set per control:
 * an advanced set the camera cannot satisfy is skipped whole, so a separate
 * set keeps one unsupported control from voiding the others.
 */
export function lockConstraints(settings, caps) {
  const support = cameraSupport(caps);
  const sets = [];
  for (const group of CAMERA_GROUPS) {
    const can = support[group.key];
    if (!can.lock) continue;
    // Without a range only the mode is sent: manual alone freezes the value.
    sets.push(can.search && Number.isFinite(settings?.[group.value])
      ? { [group.mode]: "manual", [group.value]: snap(settings[group.value], caps[group.value]) }
      : { [group.mode]: "manual" });
  }
  for (const key of CAMERA_EXTRAS) {
    if (isRange(caps?.[key]) && Number.isFinite(settings?.[key])) sets.push({ [key]: snap(settings[key], caps[key]) });
  }
  return sets;
}

/**
 * The fixed part of getSettings(), ready to save. unfixed: Chrome offers a
 * manual mode but the control did not switch. unavailable: Chrome offers no
 * manual mode at all for that control.
 */
export function fixedPart(actual, caps) {
  const support = cameraSupport(caps);
  const settings = {};
  const unfixed = [];
  const unavailable = [];
  for (const group of CAMERA_GROUPS) {
    if (!support[group.key].lock) {
      unavailable.push(group.label);
    } else if (actual?.[group.mode] === "manual") {
      settings[group.mode] = "manual";
      if (Number.isFinite(actual?.[group.value])) settings[group.value] = actual[group.value];
    } else {
      unfixed.push(group.label);
    }
  }
  for (const key of CAMERA_EXTRAS) {
    if (isRange(caps?.[key]) && Number.isFinite(actual?.[key])) settings[key] = actual[key];
  }
  return { settings, unfixed, unavailable };
}

/** Exposure × white balance grid and focus positions for this camera. */
export function searchPlan(caps, focusSteps = 7) {
  const support = cameraSupport(caps);
  const unique = (values) => [...new Set(values)];
  const focus = caps?.focusDistance;
  return {
    exposureTime: support.exposure.search ? unique(SEARCH_EXPOSURE.map((value) => snap(value, caps.exposureTime))) : [undefined],
    colorTemperature: support.whiteBalance.search ? unique(SEARCH_TEMPERATURE.map((value) => snap(value, caps.colorTemperature))) : [undefined],
    focusDistance: support.focus.search
      ? unique(Array.from({ length: focusSteps }, (_, index) =>
        snap(focus.min + (focus.max - focus.min) * index / (focusSteps - 1), focus)))
      : [],
  };
}

/** Neighbors of the best grid point: a bit shorter and longer, cooler and warmer. */
export function refinePlan(best, caps) {
  const support = cameraSupport(caps);
  const plans = [];
  if (support.exposure.search && Number.isFinite(best?.exposureTime)) {
    for (const factor of [0.75, 1.33]) plans.push({ ...best, exposureTime: snap(best.exposureTime * factor, caps.exposureTime) });
  }
  if (support.whiteBalance.search && Number.isFinite(best?.colorTemperature)) {
    for (const delta of [-500, 500]) plans.push({ ...best, colorTemperature: snap(best.colorTemperature + delta, caps.colorTemperature) });
  }
  const same = (a, b) => a.exposureTime === b.exposureTime && a.colorTemperature === b.colorTemperature;
  return plans.filter((plan, index) => !same(plan, best) && plans.findIndex((other) => same(other, plan)) === index);
}

/**
 * How well a /api/preview frame suits scanning. A frame the scan accepts
 * always beats one it refuses; then each satisfied role, then saturated
 * notes, a wall exposed near 62% and no clipped or black pixels.
 */
export function presetScore(preview) {
  const quality = preview?.quality;
  if (!quality) return -Infinity;
  const roles = preview.roles ?? {};
  const satisfied = ["start", "goal", "marker"].filter((role) => roles[role]?.count === NEEDED[role]).length;
  return (quality.ready ? 1000 : 0) + satisfied * 100 + ((roles.bumper?.count ?? 0) > 0 ? 10 : 0)
    + 0.2 * (quality.note_saturation ?? 0) - 400 * quality.clipped - 150 * quality.dark
    - 200 * Math.abs(quality.brightness - TARGET_BRIGHTNESS);
}

/** Highest score; the earliest trial wins a tie. */
export function bestTrial(trials) {
  return trials.reduce((best, trial) => (!best || trial.score > best.score ? trial : best), null);
}

/** Sharpest frame of a focus sweep; the earliest wins a tie. */
export function sharpestTrial(trials) {
  return trials.reduce((best, trial) =>
    (!best || trial.preview.quality.sharpness > best.preview.quality.sharpness ? trial : best), null);
}

/** "4000 K · 20 ms · foco 35" for the fixed parts of a preset. */
export function describeCamera(settings) {
  const parts = [];
  if (Number.isFinite(settings?.colorTemperature)) parts.push(`${Math.round(settings.colorTemperature)} K`);
  if (Number.isFinite(settings?.exposureTime)) parts.push(`${Number((settings.exposureTime / 10).toFixed(1))} ms`);
  if (Number.isFinite(settings?.focusDistance)) parts.push(`foco ${Number(settings.focusDistance.toFixed(2))}`);
  return parts.join(" · ");
}

/** Wait after a change before measuring: three frames of exposure plus slack. */
export function settleMs(settings) {
  return 250 + 3 * Math.max(0, (settings?.exposureTime ?? 0) / 10);
}

/**
 * Projector themes: the background and everything drawn on it. Dark keeps
 * the original colors. Light darkens every stroke so it stays visible on
 * a lit wall. Whatever the theme, a scan or camera search sees pure black.
 */
export const PROJECTOR_THEMES = {
  dark: {
    background: "#000000", playfield: "#56dbe0", grid: "#36696a", start: "#ffdf53",
    goal: "#ffa659", bumper: "#77ec8f", ball: "#f6fbfc", won: "#77ec8f", lost: "#ff8680",
    cross: "#ffffff", crossRing: "#f877d5",
  },
  light: {
    background: "#f3f2ec", playfield: "#0d7168", grid: "#6f968d", start: "#946f00",
    goal: "#c2540c", bumper: "#1f7a43", ball: "#17211f", won: "#1f7a43", lost: "#b0392e",
    cross: "#17211f", crossRing: "#a8308a",
  },
};
export const THEME_KEY = "postit-wall:projector-theme";
export const SCAN_BLACK = "#000000";

export function projectorTheme(value) {
  return value === "light" ? "light" : "dark";
}

export function projectorColors(theme) {
  return PROJECTOR_THEMES[projectorTheme(theme)];
}

/** The projector's fill: the theme's background, or black while scanning. */
export function projectorBackground(theme, forcedBlack) {
  return forcedBlack ? SCAN_BLACK : projectorColors(theme).background;
}

/** F switches the projector background, like J opens the projector. */
export function isThemeShortcut(event) {
  return event.key?.toLowerCase() === "f"
    && !event.repeat && !event.altKey && !event.ctrlKey && !event.metaKey
    && !isTypingTarget(event.target);
}

/** WCAG contrast ratio between two #rrggbb colors (1 to 21). */
export function contrastRatio(first, second) {
  const luminance = (hex) => {
    const [red, green, blue] = [1, 3, 5].map((index) => {
      const channel = parseInt(hex.slice(index, index + 2), 16) / 255;
      return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
    });
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
  };
  const [light, dark] = [luminance(first), luminance(second)].sort((a, b) => b - a);
  return (light + 0.05) / (dark + 0.05);
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
