import {
  attemptPhase,
  attemptView,
  averageHex,
  bestTrial,
  boardCounts,
  cameraDiagnosis,
  cameraName,
  cameraSupport,
  containTransform,
  controlsFor,
  describeCamera,
  fixedPart,
  lockConstraints,
  presetScore,
  refinePlan,
  searchPlan,
  settleMs,
  sharpestTrial,
  framePointFromClick,
  isProjectorShortcut,
  isThemeShortcut,
  isTypingTarget,
  normalizeAngle,
  PALETTE_ROLES,
  patchOrigin,
  preferredCamera,
  projectorTheme,
  ROLE_LABELS,
  roleLights,
  SETUP_STAGES,
  setupGuide,
  statusText,
  THEME_KEY,
} from "./ui-core.mjs";

const $ = (id) => document.getElementById(id);
const video = $("camera-video");
const cameraSelect = $("camera-select");
const overlay = $("camera-overlay");
const channel = new BroadcastChannel("postit-wall");
const PROJECTOR_URL = "/projector";
const SETTLE_MS = 180;
const PREVIEW_EVERY_MS = 250;
const PREVIEW_MAX_AGE_MS = 1500;
const SAMPLE_SIZE = 7;
const PALETTE_ERROR_MS = 8000;
const PALETTE_NOTE = "Tocá el gotero y después el post-it en la imagen, o elegí el color en el cuadrado. La luz se enciende cuando la cámara lo detecta.";
const FALLBACK_COLORS = { start: "#ffdf53", goal: "#ffa659", bumper: "#77ec8f", marker: "#ff77d3" };

let stream = null;
let popup = null;
let popupReady = false;
let state = null;
let scanBusy = false;
let cameraBusy = false;
let polling = false;
let localNotice = null;
let requestId = 0;
const pendingBlackouts = new Map();
let preview = null;
let previewBusy = false;
let previewTimer = null;
let previewSupported = true;
let pickRole = null;
let paletteError = null;
let sampleCanvas = null;
let cameraCaps = null;
let cameraLabel = "";
let cameraLock = { tone: "idle", text: "Ajuste de cámara: esperando la imagen." };
let tuning = null;
const CAMERA_KEYS = ["whiteBalanceMode", "colorTemperature", "exposureMode", "exposureTime",
  "focusMode", "focusDistance", "brightness", "contrast", "saturation", "sharpness",
  "width", "height", "frameRate"];
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let projectorBackdrop = storedProjectorTheme();

function storedProjectorTheme() {
  try {
    return projectorTheme(localStorage.getItem(THEME_KEY));
  } catch {
    return "dark";
  }
}

// The projector window reads the same key and listens on the channel.
function setProjectorTheme(next) {
  projectorBackdrop = projectorTheme(next);
  try {
    localStorage.setItem(THEME_KEY, projectorBackdrop);
  } catch {
    // Not remembered across reloads; the open window still switches.
  }
  channel.postMessage({ type: "theme", theme: projectorBackdrop });
  render();
}

function notice(message, tone = "info") {
  localNotice = { message, tone };
  render();
}

function clearNotice() {
  localNotice = null;
}

async function responseJSON(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`El servidor respondió con HTTP ${response.status} sin datos válidos.`);
  }
  if (!response.ok) {
    throw new Error(payload.error || payload.detail || `Error HTTP ${response.status}.`);
  }
  return payload;
}

async function action(name, extra = {}) {
  const response = await fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Postit-Client": "1" },
    body: JSON.stringify({ action: name, ...extra }),
  });
  return responseJSON(response);
}

function applyState(next) {
  state = next;
  render();
}

function cameraError(error) {
  const messages = {
    NotAllowedError: "El navegador no tiene permiso de cámara. Permitilo en la barra de direcciones de Chrome y volvé a intentar.",
    PermissionDeniedError: "El navegador no tiene permiso de cámara. Permitilo en la barra de direcciones de Chrome y volvé a intentar.",
    NotFoundError: "No se encontró ninguna cámara. Revisá la conexión USB de la Logitech C920.",
    DevicesNotFoundError: "No se encontró ninguna cámara. Revisá la conexión USB de la Logitech C920.",
    NotReadableError: "La cámara está ocupada o macOS no puede abrirla. Cerrá otras apps que la usen y reintentá.",
    TrackStartError: "La cámara está ocupada o macOS no puede abrirla. Cerrá otras apps que la usen y reintentá.",
    SecurityError: "La cámara solo funciona en localhost o en una página segura.",
    OverconstrainedError: "La cámara no admite la configuración solicitada. Elegí otra cámara.",
  };
  return messages[error?.name] || error?.message || "No se pudo abrir la cámara.";
}

function stopCamera() {
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  video.srcObject = null;
}

async function updateCameraList(selectedId = "") {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const cameras = (await navigator.mediaDevices.enumerateDevices())
    .filter((device) => device.kind === "videoinput");
  cameraSelect.replaceChildren();
  if (cameras.length === 0) {
    cameraSelect.add(new Option("No se encontraron cámaras", ""));
    cameraSelect.disabled = true;
    return;
  }
  cameraSelect.disabled = false;
  cameras.forEach((device, index) => {
    cameraSelect.add(new Option(cameraName(device, index), device.deviceId));
  });
  cameraSelect.value = selectedId || preferredCamera(cameras)?.deviceId || cameras[0].deviceId;
}

async function startCamera(deviceId = "", preferExternal = false) {
  if (cameraBusy) return;
  cameraBusy = true;
  const previousId = stream?.getVideoTracks()[0]?.getSettings().deviceId || "";
  $("camera-placeholder").hidden = false;
  $("retry-camera").hidden = true;
  $("camera-placeholder").querySelector("strong").textContent = "Conectando la cámara";
  $("camera-placeholder").querySelector("span").textContent = "Si macOS pide permiso, elegí Permitir en el navegador.";
  notice("Solicitando imagen de cámara…", "busy");

  try {
    if (!navigator.mediaDevices?.getUserMedia) {
      throw Object.assign(new Error("Abrí la página en Chrome desde localhost para usar la cámara."), { name: "SecurityError" });
    }
    stopCamera();
    const constraints = {
      audio: false,
      video: {
        width: { ideal: 1280 },
        height: { ideal: 720 },
        ...(deviceId ? { deviceId: { exact: deviceId } } : {}),
      },
    };
    stream = await navigator.mediaDevices.getUserMedia(constraints);
    video.srcObject = stream;
    await video.play();
    const activeId = stream.getVideoTracks()[0]?.getSettings().deviceId || deviceId;
    if (previousId && activeId !== previousId) {
      applyState(await action("camera_changed"));
    }
    await updateCameraList(activeId);
    const cameras = (await navigator.mediaDevices.enumerateDevices())
      .filter((device) => device.kind === "videoinput");
    const preferred = preferredCamera(cameras);
    if (preferExternal && preferred?.deviceId && preferred.deviceId !== activeId) {
      cameraBusy = false;
      await startCamera(preferred.deviceId, false);
      return;
    }
    $("camera-placeholder").hidden = true;
    $("camera-live-tag").hidden = false;
    $("video-resolution").textContent = `${video.videoWidth} × ${video.videoHeight}`;
    $("connection-pill").textContent = cameraName(cameras.find((device) => device.deviceId === activeId));
    $("connection-pill").dataset.tone = "ready";
    clearNotice();
    render();
    setupCameraLock();
  } catch (error) {
    stopCamera();
    cameraCaps = null;
    cameraLock = { tone: "idle", text: "Ajuste de cámara: esperando la imagen." };
    const message = cameraError(error);
    $("camera-placeholder").hidden = false;
    $("camera-placeholder").querySelector("strong").textContent = "No se ve la cámara";
    $("camera-placeholder").querySelector("span").textContent = message;
    $("retry-camera").textContent = "Reintentar cámara";
    $("retry-camera").hidden = false;
    $("camera-live-tag").hidden = true;
    $("video-resolution").textContent = "Sin imagen";
    $("connection-pill").textContent = "Cámara sin acceso";
    $("connection-pill").dataset.tone = "error";
    notice(message, "error");
  } finally {
    cameraBusy = false;
    render();
  }
}

function openProjector() {
  if (popup && !popup.closed) {
    popup.focus();
    return;
  }
  // It must stay directly in the click/keydown call stack for popup permission.
  const opened = window.open(PROJECTOR_URL, "postit-wall-projector", "popup=yes,width=1280,height=720");
  if (!opened) {
    notice("Chrome bloqueó la ventana. Permití las ventanas emergentes de localhost y usá el botón «Abrir proyección».", "error");
    return;
  }
  popup = opened;
  popupReady = false;
  popup.focus();
  notice("Mové la nueva ventana al proyector y pulsá «Pantalla completa» allí.", "info");
}

function waitForBlackout() {
  return new Promise((resolve, reject) => {
    const id = ++requestId;
    const timeout = setTimeout(() => {
      pendingBlackouts.delete(id);
      reject(new Error("La ventana de proyección no confirmó el fondo negro. Abrila otra vez con J."));
    }, 2500);
    pendingBlackouts.set(id, () => {
      clearTimeout(timeout);
      pendingBlackouts.delete(id);
      resolve();
    });
    channel.postMessage({ type: "blackout", id });
  });
}

function jpegFromVideo(quality = .94) {
  return new Promise((resolve, reject) => {
    if (!stream || video.readyState < HTMLMediaElement.HAVE_CURRENT_DATA || !video.videoWidth) {
      reject(new Error("La cámara aún no entrega imágenes. Esperá un momento y reintentá."));
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d", { alpha: false }).drawImage(video, 0, 0);
    canvas.toBlob((blob) => {
      if (blob) resolve(blob);
      else reject(new Error("No se pudo capturar una imagen de la cámara."));
    }, "image/jpeg", quality);
  });
}

function schedulePreview(delay = PREVIEW_EVERY_MS) {
  if (previewTimer || !previewSupported) return;
  previewTimer = setTimeout(previewTick, delay);
}

function freshPreview() {
  return preview && performance.now() - preview.at < PREVIEW_MAX_AGE_MS ? preview : null;
}

// Live detection: the scan's own detector and palette, run on the backend.
async function previewTick() {
  previewTimer = null;
  if (previewBusy || scanBusy || tuning || document.hidden || !stream || !video.videoWidth) {
    schedulePreview();
    return;
  }
  previewBusy = true;
  try {
    const response = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "image/jpeg", "X-Postit-Client": "1" },
      body: await jpegFromVideo(.8),
    });
    if (response.status === 404) {
      previewSupported = false; // server started before live detection existed
      preview = null;
    } else {
      preview = { ...(await responseJSON(response)), at: performance.now() };
    }
  } catch {
    preview = null; // best effort; the scan itself still reports real errors
  } finally {
    previewBusy = false;
    render();
    schedulePreview();
  }
}

function sampleHex(point) {
  sampleCanvas ||= document.createElement("canvas");
  sampleCanvas.width = video.videoWidth;
  sampleCanvas.height = video.videoHeight;
  const ctx = sampleCanvas.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(video, 0, 0);
  const origin = patchOrigin(point, video.videoWidth, video.videoHeight, SAMPLE_SIZE);
  return averageHex(ctx.getImageData(origin.x, origin.y, SAMPLE_SIZE, SAMPLE_SIZE).data);
}

async function setColor(role, hex) {
  paletteError = null;
  try {
    applyState(await action("palette_set", { role, hex }));
  } catch (error) {
    paletteError = { message: error.message, at: performance.now() };
    const current = state?.palette?.roles?.[role]?.hex;
    if (current) $(`color-${role}`).value = current;
    render();
  }
}

function currentTrack() {
  return stream?.getVideoTracks()[0] ?? null;
}

function cameraKeys(source) {
  return Object.fromEntries(CAMERA_KEYS.filter((key) => source?.[key] !== undefined)
    .map((key) => [key, source[key]]));
}

async function readCapabilities(track) {
  // Chrome fills the image-capture part of getCapabilities() once it has read
  // the camera's photo state; asking ImageCapture first makes that explicit,
  // and a few retries cover a slow first read.
  let photoError = null;
  if ("ImageCapture" in window) {
    try {
      await Promise.race([new ImageCapture(track).getPhotoCapabilities(), sleep(3000)]);
    } catch (error) {
      photoError = `${error.name}: ${error.message}`;
    }
  } else {
    photoError = "ImageCapture no existe en este navegador";
  }
  let caps = track.getCapabilities?.() ?? {};
  for (let attempt = 0; attempt < 6 && !cameraSupport(caps).any; attempt += 1) {
    await sleep(500);
    caps = track.getCapabilities?.() ?? {};
  }
  return { caps, photoError };
}

async function applyCamera(track, settings) {
  const sets = lockConstraints(settings, cameraCaps);
  if (sets.length) {
    const { width, height } = track.getSettings();
    await track.applyConstraints({ width: { ideal: width }, height: { ideal: height }, advanced: sets });
  }
  return track.getSettings();
}

async function reportCamera(kind, details) {
  try {
    await action("camera_report", { camera: cameraLabel, report: { kind, ...details } });
  } catch {
    // The log is evidence, not a requirement: a failed report changes nothing.
  }
}

function lockStatus(settings, { unfixed = [], unavailable = [] }, black) {
  const values = describeCamera(settings);
  const text = values ? `Cámara fija: ${values}` : "Cámara fija en los valores actuales";
  const problems = [
    unfixed.length ? `no tomó el manual: ${unfixed.join(", ")}` : "",
    unavailable.length ? `Chrome no da control de ${unavailable.join(", ")}` : "",
  ].filter(Boolean);
  if (problems.length) return { tone: "warn", text: `${text}. Ojo: ${problems.join("; ")}.` };
  if (!black) return { tone: "warn", text: `${text}. Repetí la búsqueda con la proyección abierta.` };
  return { tone: "ok", text };
}

// Every camera start: apply the saved preset, or search one and save it.
async function setupCameraLock() {
  const track = currentTrack();
  if (!track || tuning) return;
  cameraLabel = track.label || cameraSelect.selectedOptions[0]?.textContent || "Cámara";
  cameraLock = { tone: "busy", text: "Leyendo los controles de la cámara…" };
  render();
  const { caps, photoError } = await readCapabilities(track);
  if (track !== currentTrack()) return;
  cameraCaps = caps;
  const support = cameraSupport(cameraCaps);
  const diagnosis = cameraDiagnosis(cameraCaps);
  console.info("Controles de cámara que informa Chrome:", cameraKeys(cameraCaps), photoError ?? "");
  await reportCamera("capabilities", { capabilities: cameraKeys(cameraCaps), support, diagnosis,
    photo_error: photoError, all_keys: Object.keys(cameraCaps) });
  if (!support.any) {
    cameraLock = { tone: "warn", detail: diagnosis,
      text: "Chrome no ofrece modo manual de balance, exposición ni foco para esta cámara (pasá el mouse para ver qué informó)." };
    render();
    return;
  }
  try {
    applyState(await responseJSON(await fetch("/api/state", { cache: "no-store" })));
  } catch {
    // The regular poll keeps trying.
  }
  if (!state || !("camera_presets" in state)) {
    cameraLock = { tone: "warn", text: "Reiniciá web.py para fijar la cámara." };
    render();
    return;
  }
  const preset = state.camera_presets[cameraLabel];
  if (!preset) {
    await tuneCamera();
    return;
  }
  try {
    const actual = await applyCamera(track, preset.settings);
    const fixed = fixedPart(actual, cameraCaps);
    await reportCamera("applied", { requested: preset.settings, actual: cameraKeys(actual),
      unfixed: fixed.unfixed, unavailable: fixed.unavailable });
    cameraLock = { ...lockStatus(fixed.settings, fixed, preset.black), detail: diagnosis };
  } catch (error) {
    cameraLock = { tone: "error", text: `No se pudo fijar la cámara: ${error.message}` };
  }
  render();
}

async function tryCamera(track, settings) {
  if (track !== currentTrack()) throw new Error("la cámara cambió durante la búsqueda");
  await applyCamera(track, settings);
  await sleep(settleMs(settings));
  const response = await fetch("/api/preview", {
    method: "POST",
    headers: { "Content-Type": "image/jpeg", "X-Postit-Client": "1" },
    body: await jpegFromVideo(.8),
  });
  const result = await responseJSON(response);
  preview = { ...result, at: performance.now() }; // the lights follow the search
  tuning.done += 1;
  render();
  return { settings, preview: result, score: presetScore(result) };
}

// Only fixed values are tried: an exposure × white balance grid, a focus
// sweep, then the neighbors of the best. Scored by the scan's own detector.
async function tuneCamera() {
  const track = currentTrack();
  if (!track || tuning || scanBusy || !cameraCaps || !cameraSupport(cameraCaps).any) return;
  const plan = searchPlan(cameraCaps);
  const projectorReady = Boolean(popupReady && state?.projection?.open && popup && !popup.closed);
  tuning = { done: 0, total: plan.exposureTime.length * plan.colorTemperature.length
    + plan.focusDistance.length + 5 };
  cameraSelect.disabled = true;
  let black = false;
  render();
  try {
    if (projectorReady && attemptPhase(state?.game) !== "running") {
      applyState(await action("projection_black"));
      await waitForBlackout();
      black = true;
    }
    const start = track.getSettings();
    const focus = Number.isFinite(start.focusDistance) ? { focusDistance: start.focusDistance } : {};
    const grid = [];
    for (const exposureTime of plan.exposureTime) {
      for (const colorTemperature of plan.colorTemperature) {
        grid.push(await tryCamera(track, { ...focus, exposureTime, colorTemperature }));
      }
    }
    let best = bestTrial(grid).settings;
    if (plan.focusDistance.length) {
      const sweep = [];
      for (const focusDistance of plan.focusDistance) sweep.push(await tryCamera(track, { ...best, focusDistance }));
      best = { ...best, focusDistance: sharpestTrial(sweep).settings.focusDistance };
    }
    const finalists = [await tryCamera(track, best)];
    for (const settings of refinePlan(best, cameraCaps)) finalists.push(await tryCamera(track, settings));
    const winner = bestTrial(finalists);
    const actual = await applyCamera(track, winner.settings);
    const fixed = fixedPart(actual, cameraCaps);
    const { settings } = fixed;
    await reportCamera("search", { chosen: winner.settings, score: winner.score, trials: tuning.done,
      actual: cameraKeys(actual), unfixed: fixed.unfixed, unavailable: fixed.unavailable, black });
    if (Object.keys(settings).length) {
      applyState(await action("camera_preset_save", { camera: cameraLabel, settings, score: winner.score,
        black, trials: tuning.done, capabilities: cameraKeys(cameraCaps) }));
    }
    cameraLock = { ...lockStatus(settings, fixed, black), detail: cameraDiagnosis(cameraCaps) };
  } catch (error) {
    cameraLock = { tone: "error", text: `No se pudo fijar la cámara: ${error.message}` };
  } finally {
    if (black) {
      await restoreProjection();
      channel.postMessage({ type: "resume" });
    }
    tuning = null;
    cameraSelect.disabled = cameraSelect.options.length === 0 || !cameraSelect.options[0].value;
    render();
  }
}

// A blackout that produced no new board must not leave the projector dark.
async function restoreProjection() {
  try {
    applyState(await action("projection_restore"));
  } catch {
    // A server without this action restores the view on the next game action.
  }
}

function togglePick(role) {
  paletteError = null;
  pickRole = pickRole === role ? null : role;
  render();
}

async function scanBoard() {
  if (scanBusy || tuning) return;
  if (!controlsFor(state, Boolean(stream), popupReady && state?.projection?.open).scan) return;
  scanBusy = true;
  render();
  try {
    notice("Apagando el proyector para leer los colores…", "busy");
    applyState(await action("projection_black"));
    await waitForBlackout();
    await new Promise((resolve) => setTimeout(resolve, SETTLE_MS));
    notice("Leyendo los post-its…", "busy");
    const jpeg = await jpegFromVideo();
    const response = await fetch("/api/frame", {
      method: "POST",
      headers: { "Content-Type": "image/jpeg", "X-Postit-Client": "1" },
      body: jpeg,
    });
    applyState(await responseJSON(response));
    clearNotice();
    render();
  } catch (error) {
    notice(error.message, "error");
    await restoreProjection();
  } finally {
    channel.postMessage({ type: "resume" });
    scanBusy = false;
    render();
  }
}

async function sendAction(name, extra = {}) {
  try {
    clearNotice();
    applyState(await action(name, extra));
  } catch (error) {
    notice(error.message, "error");
  }
}

function setAngle(value) {
  const angle = normalizeAngle(value);
  $("angle-range").value = String(angle);
  $("angle-value").textContent = `${angle < 0 ? "−" : ""}${Math.abs(angle)}°`;
  return angle;
}

async function changeAngle(value) {
  const angle = setAngle(value);
  await sendAction("aim", { degrees: angle });
}

function drawCameraOverlay() {
  const box = $("camera-stage");
  const dpr = window.devicePixelRatio || 1;
  const width = box.clientWidth;
  const height = box.clientHeight;
  const pixelWidth = Math.round(width * dpr);
  const pixelHeight = Math.round(height * dpr);
  if (overlay.width !== pixelWidth || overlay.height !== pixelHeight) {
    overlay.width = pixelWidth;
    overlay.height = pixelHeight;
  }
  const ctx = overlay.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, width, height);
  const colors = Object.fromEntries(PALETTE_ROLES.map((role) =>
    [role, state?.palette?.roles?.[role]?.hex || FALLBACK_COLORS[role]]));
  const trace = (fit, shape) => {
    ctx.beginPath();
    shape.points.forEach((point, index) => {
      const x = fit.offsetX + point.x * fit.scale;
      const y = fit.offsetY + point.y * fit.scale;
      if (index === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.closePath();
  };
  const outline = (fit, shape, color) => {
    if (!shape?.points?.length) return;
    trace(fit, shape);
    ctx.lineWidth = 2.6;
    ctx.strokeStyle = color;
    ctx.shadowColor = "#00130e";
    ctx.shadowBlur = 5;
    ctx.stroke();
    ctx.shadowBlur = 0;
  };
  // A detected note lights up: a halo in its role color, a white rim on top.
  const glow = (fit, shape, color) => {
    if (!shape?.points?.length) return;
    ctx.save();
    trace(fit, shape);
    ctx.shadowColor = color;
    ctx.shadowBlur = 18;
    ctx.globalAlpha = .3;
    ctx.fillStyle = color;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.lineWidth = 2;
    ctx.strokeStyle = "#ffffff";
    ctx.stroke();
    ctx.restore();
  };

  const board = state?.camera_board;
  const frameWidth = state?.camera?.width || video.videoWidth;
  const frameHeight = state?.camera?.height || video.videoHeight;
  if (board && frameWidth && frameHeight) {
    const fit = containTransform(width, height, frameWidth, frameHeight);
    outline(fit, board.playfield, "#70dce0");
    outline(fit, board.start, colors.start);
    outline(fit, board.goal, colors.goal);
    board.bumpers?.forEach((shape) => outline(fit, shape, colors.bumper));
    board.markers?.forEach((shape) => outline(fit, shape, colors.marker));
  }
  const live = freshPreview();
  if (live?.width && live?.height) {
    const fit = containTransform(width, height, live.width, live.height);
    for (const role of PALETTE_ROLES) {
      live.roles?.[role]?.notes?.forEach((shape) => glow(fit, shape, colors[role]));
    }
  }
}

function renderPalette(cameraReady) {
  const palette = state?.palette;
  const editable = Boolean(palette) && previewSupported;
  if (pickRole && (!editable || !cameraReady)) pickRole = null;
  const lights = roleLights(freshPreview());
  for (const role of PALETTE_ROLES) {
    const swatch = $(`color-${role}`);
    const color = palette?.roles?.[role]?.hex;
    swatch.disabled = !editable;
    if (color && document.activeElement !== swatch) swatch.value = color;
    const box = swatch.closest(".palette-role");
    box.style.setProperty("--role-color", color || FALLBACK_COLORS[role]);
    box.dataset.picking = String(pickRole === role);
    $(`light-${role}`).dataset.state = lights[role].state;
    $(`seen-${role}`).textContent = lights[role].text;
    $(`seen-${role}`).dataset.state = lights[role].state;
    $(`pick-${role}`).disabled = !editable || !cameraReady;
    $(`pick-${role}`).setAttribute("aria-pressed", String(pickRole === role));
  }
  $("palette-reset").disabled = !editable || !palette.custom;
  document.querySelectorAll(".note-dot[data-role]").forEach((dot) => {
    dot.style.background = palette?.roles?.[dot.dataset.role]?.hex || "";
  });

  const note = $("palette-note");
  const outdated = Boolean(state) && (!palette || !previewSupported);
  if (paletteError && performance.now() - paletteError.at > PALETTE_ERROR_MS) paletteError = null;
  note.textContent = outdated
    ? "Reiniciá web.py para elegir colores y ver la detección en vivo."
    : paletteError?.message || (pickRole
      ? `Tocá un post-it de ${ROLE_LABELS[pickRole]} en la imagen. Esc cancela.`
      : PALETTE_NOTE);
  note.dataset.tone = outdated || paletteError ? "warn" : "";
  $("pick-banner").hidden = !pickRole;
  $("pick-banner").textContent = pickRole ? `Tocá un post-it de ${ROLE_LABELS[pickRole]} · Esc cancela` : "";
  if (pickRole) $("camera-stage").dataset.picking = pickRole;
  else delete $("camera-stage").dataset.picking;
}

function render() {
  const cameraReady = Boolean(stream && video.videoWidth);
  const projectionReady = Boolean(popupReady && state?.projection?.open && popup && !popup.closed);
  const controls = controlsFor(state, cameraReady, projectionReady);
  const guide = setupGuide(state, cameraReady, projectionReady);
  $("scan-button").disabled = scanBusy || Boolean(tuning) || !controls.scan;
  $("calibrate-button").disabled = scanBusy || Boolean(tuning) || !controls.calibrate;
  const lock = tuning ? { tone: "busy", text: `Buscando ajuste fijo de cámara ${tuning.done}/${tuning.total}…` } : cameraLock;
  $("camera-lock").textContent = lock.text;
  $("camera-lock").title = lock.detail ? `${lock.text}\n\nChrome informa: ${lock.detail}` : lock.text;
  $("camera-lock").dataset.tone = lock.tone;
  $("camera-tune").disabled = !cameraReady || Boolean(tuning) || scanBusy || !cameraSupport(cameraCaps).any
    || !state || !("camera_presets" in state) || attemptPhase(state?.game) === "running";
  $("camera-tune").textContent = state?.camera_presets?.[cameraLabel] ? "Buscar otra vez" : "Buscar ajuste fijo";
  $("launch-button").disabled = !controls.launch;
  $("launch-button").hidden = controls.resume;
  $("resume-button").hidden = !controls.resume;
  $("resume-button").disabled = !controls.resume;
  $("reset-button").disabled = !controls.reset;
  $("angle-range").disabled = !controls.aim;
  $("aim-left").disabled = !controls.aim;
  $("aim-right").disabled = !controls.aim;
  $("open-projector").innerHTML = projectionReady
    ? "Enfocar proyección <kbd>J</kbd>"
    : "Abrir proyección <kbd>J</kbd>";
  $("open-projector").dataset.attention = String(guide.step === "projector");
  $("theme-dark").setAttribute("aria-pressed", String(projectorBackdrop === "dark"));
  $("theme-light").setAttribute("aria-pressed", String(projectorBackdrop === "light"));
  $("setup-hint").textContent = guide.message;
  $("setup-hint").dataset.tone = guide.step === "play" ? "" : "next";
  const stageIndex = SETUP_STAGES.indexOf(guide.stage);
  document.querySelectorAll(".step-hint [data-step]").forEach((step) => {
    const index = SETUP_STAGES.indexOf(step.dataset.step);
    step.dataset.state = index < stageIndex ? "done" : index === stageIndex ? "current" : "todo";
  });

  const counts = boardCounts(state);
  const scanned = Boolean(state?.camera_board || state?.board);
  $("count-start").textContent = scanned ? String(counts.start) : "—";
  $("count-goal").textContent = scanned ? String(counts.goal) : "—";
  $("count-bumpers").textContent = scanned ? String(counts.bumpers) : "—";
  $("count-markers").textContent = scanned ? `${counts.markers}/4` : "—";

  const game = state?.game ?? {};
  const attempt = attemptView(game);
  $("game-pill").textContent = attempt.label;
  $("game-pill").dataset.tone = attempt.phase;
  $("attempt-hint").textContent = attempt.hint;
  if (document.activeElement !== $("angle-range")) setAngle(game.aim_degrees ?? -45);
  $("stat-bounces").textContent = String(game.bounces ?? 0);
  $("stat-time").textContent = `${Number(game.elapsed_s ?? 0).toLocaleString("es-AR", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} s`;
  $("stat-attempts").textContent = String(game.attempts ?? 0);
  $("status-message").textContent = statusText(localNotice, state, guide);
  $("status-panel").dataset.tone = localNotice?.tone || "info";
  renderPalette(cameraReady);
  drawCameraOverlay();
}

channel.onmessage = (event) => {
  const data = event.data || {};
  if (data.type === "ready") {
    popupReady = true;
    channel.postMessage({ type: "theme", theme: projectorBackdrop });
    render();
  } else if (data.type === "theme") {
    projectorBackdrop = projectorTheme(data.theme); // switched from the projector window
    render();
  } else if (data.type === "closed") {
    popupReady = false;
    render();
  } else if (data.type === "blackout_ack") {
    pendingBlackouts.get(data.id)?.();
  } else if (data.type === "projector_error") {
    notice(data.message, "error");
  }
};

$("open-projector").addEventListener("click", openProjector);
$("retry-camera").addEventListener("click", () => startCamera(cameraSelect.value || "", false));
cameraSelect.addEventListener("change", () => startCamera(cameraSelect.value, false));
$("scan-button").addEventListener("click", scanBoard);
$("calibrate-button").addEventListener("click", () => sendAction("calibrate"));
$("launch-button").addEventListener("click", () => sendAction("launch"));
$("resume-button").addEventListener("click", () => sendAction("resume"));
$("reset-button").addEventListener("click", () => sendAction("reset"));
$("aim-left").addEventListener("click", () => changeAngle(Number($("angle-range").value) - 5));
$("aim-right").addEventListener("click", () => changeAngle(Number($("angle-range").value) + 5));
$("angle-range").addEventListener("input", (event) => setAngle(event.target.value));
$("angle-range").addEventListener("change", (event) => changeAngle(event.target.value));
for (const role of PALETTE_ROLES) {
  $(`color-${role}`).addEventListener("change", (event) => setColor(role, event.target.value));
  $(`pick-${role}`).addEventListener("click", () => togglePick(role));
}
$("camera-tune").addEventListener("click", () => tuneCamera());
$("theme-dark").addEventListener("click", () => setProjectorTheme("dark"));
$("theme-light").addEventListener("click", () => setProjectorTheme("light"));
$("palette-reset").addEventListener("click", () => {
  paletteError = null;
  sendAction("palette_reset");
});
$("camera-stage").addEventListener("click", (event) => {
  if (!pickRole || !stream || !video.videoWidth) return;
  const point = framePointFromClick(event.clientX, event.clientY,
    $("camera-stage").getBoundingClientRect(), video.videoWidth, video.videoHeight);
  if (!point) return;
  const role = pickRole;
  pickRole = null;
  setColor(role, sampleHex(point));
});

document.addEventListener("keydown", (event) => {
  if (isProjectorShortcut(event)) {
    event.preventDefault();
    openProjector();
    return;
  }
  if (isThemeShortcut(event)) {
    event.preventDefault();
    setProjectorTheme(projectorBackdrop === "dark" ? "light" : "dark");
    return;
  }
  if (pickRole && event.key === "Escape") {
    event.preventDefault();
    pickRole = null;
    render();
    return;
  }
  if (isTypingTarget(event.target) || event.repeat || event.altKey || event.ctrlKey || event.metaKey) return;
  const key = event.key.toLowerCase();
  if (key === "s") {
    event.preventDefault();
    scanBoard();
  } else if (key === "c" && !$("calibrate-button").disabled) {
    event.preventDefault();
    sendAction("calibrate");
  } else if (key === "r" && !$("reset-button").disabled) {
    event.preventDefault();
    sendAction("reset");
  } else if (event.code === "Space" && !$("launch-button").disabled) {
    event.preventDefault();
    sendAction("launch");
  } else if (event.code === "Space" && !$("resume-button").disabled) {
    event.preventDefault();
    sendAction("resume");
  } else if (key === "arrowleft" && !$("aim-left").disabled) {
    event.preventDefault();
    changeAngle(Number($("angle-range").value) - 5);
  } else if (key === "arrowright" && !$("aim-right").disabled) {
    event.preventDefault();
    changeAngle(Number($("angle-range").value) + 5);
  }
});

window.addEventListener("resize", drawCameraOverlay);
window.addEventListener("beforeunload", stopCamera);
navigator.mediaDevices?.addEventListener?.("devicechange", () => {
  updateCameraList(stream?.getVideoTracks()[0]?.getSettings().deviceId).catch(() => {});
});

async function pollState() {
  if (polling) return;
  polling = true;
  try {
    const response = await fetch("/api/state", { cache: "no-store" });
    applyState(await responseJSON(response));
    if (popup && popup.closed) popupReady = false;
  } catch (error) {
    notice(`No se puede conectar con el servidor local: ${error.message}`, "error");
  } finally {
    polling = false;
  }
}

render();
pollState();
setInterval(pollState, 300);
startCamera("", true);
schedulePreview(0);
