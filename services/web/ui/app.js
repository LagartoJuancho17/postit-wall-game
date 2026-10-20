import {
  attemptPhase,
  attemptView,
  bestTrial,
  CAMERA_GROUPS,
  CAMERA_KEY,
  boardCounts,
  cameraDiagnosis,
  cameraName,
  cameraToSwitchTo,
  cameraSupport,
  controlsFor,
  describeCamera,
  invalidatesBoard,
  driftedModes,
  fixedPart,
  lockConstraints,
  mergeSets,
  presetScore,
  refinePlan,
  searchPlan,
  settleMs,
  sharpestTrial,
  framePointFromClick,
  isPhoneCamera,
  isProjectorShortcut,
  isThemeShortcut,
  isTypingTarget,
  normalizeAngle,
  noteColorHex,
  PALETTE_ROLES,
  patchOrigin,
  pickVerdict,
  preferredCamera,
  projectorTheme,
  ROLE_LABELS,
  roleLights,
  SAMPLE_SIZE,
  scanSettleMs,
  SETUP_STAGES,
  setupGuide,
  statusText,
  THEME_KEY,
  usbIdsFromLabel,
  weakColor,
  withTimeout,
} from "./ui-core.mjs";
import { createApi } from "./api.mjs";
import { autoAlignKey, runAlignment, shouldAutoAlign } from "./align-run.mjs";
import { drawLiveDetection, FALLBACK_COLORS, LIVE_MAX_AGE_MS } from "./live-view.mjs";

const api = createApi("panel");

const $ = (id) => document.getElementById(id);
const video = $("camera-video");
const cameraSelect = $("camera-select");
const overlay = $("camera-overlay");
const channel = new BroadcastChannel("postit-wall");
const PROJECTOR_URL = "/projector";
const PREVIEW_EVERY_MS = 250;
const PREVIEW_MAX_AGE_MS = LIVE_MAX_AGE_MS;
const PALETTE_ERROR_MS = 8000;
const CAMERA_FRAMES_MS = 8000;
const PALETTE_NOTE = "Tocá el gotero y después el post-it en la imagen, o elegí el color en el cuadrado. La luz se enciende cuando la cámara lo detecta.";

let stream = null;
let popup = null;
let popupReady = false;
let state = null;
let scanBusy = false;
let cameraBusy = false;
let polling = false;
let connectionNotice = null;
let pickBusy = false;
// The color just set, judged by the first preview taken after it.
let pickCheck = null;
let localNotice = null;
let requestId = 0;
// Projector messages waiting for their ack, by id: blackout, neutral,
// align_pattern and overlay_test all answer once drawn.
const pendingAcks = new Map();
let aligning = false;
let projectorFullscreen = false;
let projectorHidden = false;
const autoAlignTried = new Set();
let preview = null;
let previewGeneration = 0;
let previewBusy = false;
let previewTimer = null;
let previewSupported = true;
let pickRole = null;
let paletteError = null;
let sampleCanvas = null;
let cameraCaps = null;
let cameraDriver = "chrome"; // "chrome" or "usb"
let cameraUsb = null;        // { vendor, product } when the server drives the camera
let cameraLabel = "";
let cameraLock = { tone: "idle", text: "Ajuste de cámara: esperando la imagen." };
let cameraFixed = false; // white balance, exposure or focus held by hand
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
  const response = await api("/api/action", {
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
    NotFoundError: "No se encontró ninguna cámara. Conectá la Logitech C920 por USB o acercá el iPhone a la Mac (Cámara de continuidad).",
    DevicesNotFoundError: "No se encontró ninguna cámara. Conectá la Logitech C920 por USB o acercá el iPhone a la Mac (Cámara de continuidad).",
    NotReadableError: "La cámara está ocupada o macOS no puede abrirla. Cerrá otras apps que la usen y reintentá.",
    TrackStartError: "La cámara está ocupada o macOS no puede abrirla. Cerrá otras apps que la usen y reintentá.",
    SecurityError: "La cámara solo funciona en localhost o en una página segura.",
    OverconstrainedError: "La cámara no admite la configuración solicitada. Elegí otra cámara.",
  };
  return messages[error?.name] || error?.message || "No se pudo abrir la cámara.";
}

function stopCamera() {
  previewGeneration += 1;
  stream?.getTracks().forEach((track) => track.stop());
  stream = null;
  video.srcObject = null;
  preview = null;
}

// The camera the operator picked stays picked across reloads.
function rememberedCamera() {
  try {
    return localStorage.getItem(CAMERA_KEY) || "";
  } catch {
    return "";
  }
}

function rememberCamera(label) {
  try {
    if (label) localStorage.setItem(CAMERA_KEY, label);
  } catch {
    // Not remembered; the C920 stays the default.
  }
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
  cameraSelect.value = selectedId || preferredCamera(cameras, rememberedCamera())?.deviceId || cameras[0].deviceId;
}

async function startCamera(deviceId = "", preferExternal = false) {
  if (cameraBusy) return;
  cameraBusy = true;
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
    await withTimeout(video.play(), CAMERA_FRAMES_MS,
      "La cámara no entrega imágenes. Revisá que esté conectada y que ninguna otra app la use, y reintentá.");
    const activeId = stream.getVideoTracks()[0]?.getSettings().deviceId || deviceId;
    await updateCameraList(activeId);
    const cameras = (await navigator.mediaDevices.enumerateDevices())
      .filter((device) => device.kind === "videoinput");
    const preferred = preferredCamera(cameras, rememberedCamera());
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
    await forgetBoardOfOtherCamera(currentTrack()?.label);
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

// Only once the camera has settled (startup may hop from the default camera
// to the preferred one) and only if the board came from a different camera:
// reloading with the same camera keeps the active attempt.
async function forgetBoardOfOtherCamera(label) {
  try {
    const latest = state ?? await responseJSON(await api("/api/state", { cache: "no-store" }));
    if (invalidatesBoard(latest?.camera?.label, label)) applyState(await action("camera_changed"));
  } catch (error) {
    notice(error.message, "error");
  }
}

function openProjector() {
  if (popup && !popup.closed) {
    popup.focus();
    return;
  }
  // It must stay directly in the click/keydown call stack for popup permission.
  // A named popup: a reloaded panel reuses it instead of opening a second
  // projector, and Chrome gives it its own window to move to the projector.
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

/** Ask the projector to draw a scene; resolves with its ack once on screen. */
function showOnProjector(message, failure = "La ventana de proyección no respondió. Abrila otra vez con J.") {
  return new Promise((resolve, reject) => {
    const id = ++requestId;
    const timeout = setTimeout(() => {
      pendingAcks.delete(id);
      reject(new Error(failure));
    }, 2500);
    pendingAcks.set(id, (ack) => {
      clearTimeout(timeout);
      pendingAcks.delete(id);
      resolve({ ...ack, at: performance.now() });
    });
    channel.postMessage({ ...message, id });
  });
}

function waitForBlackout() {
  return showOnProjector({ type: "blackout" },
    "La ventana de proyección no confirmó el fondo negro. Abrila otra vez con J.");
}

// Colors and camera settings are measured under the play light, minus our
// own drawing: the theme background only (contract 2.2).
function waitForNeutral() {
  return showOnProjector({ type: "neutral" },
    "La ventana de proyección no confirmó el fondo neutro. Abrila otra vez con J.");
}

/** At least `ms` after the projector's ack AND two new camera frames presented after it. */
function settleAfter(ack, ms) {
  return new Promise((resolve) => {
    const since = ack?.at ?? performance.now();
    let frames = 0;
    let finished = false;
    const finish = () => {
      if (!finished) {
        finished = true;
        resolve();
      }
    };
    setTimeout(finish, ms + 1500); // a stalled camera must not hang the run
    if (typeof video.requestVideoFrameCallback !== "function") {
      setTimeout(finish, ms);
      return;
    }
    const onFrame = (now) => {
      if (now >= since) frames += 1;
      if (frames >= 2 && performance.now() - since >= ms) finish();
      else if (!finished) video.requestVideoFrameCallback(onFrame);
    };
    video.requestVideoFrameCallback(onFrame);
  });
}

async function uploadFrame(path, blob, headers) {
  const response = await api(path, {
    method: "POST",
    headers: { "Content-Type": "image/jpeg", "X-Postit-Client": "1",
      "X-Postit-Camera": encodeURIComponent(currentTrack()?.label || ""), ...headers },
    body: blob,
  });
  return responseJSON(response);
}

function projectorIsReady() {
  return Boolean(popupReady && state?.projection?.open && popup && !popup.closed);
}

// Contract section 3: coded patterns, decode, then the overlay check.
async function alignProjector() {
  if (aligning || scanBusy || tuning || pickBusy || !stream || !video.videoWidth) return;
  if (!controlsFor(state, true, projectorIsReady()).align) return;
  aligning = true;
  previewGeneration += 1;
  autoAlignTried.add(autoAlignKey(state));
  notice("Alineando cámara y proyector: no los muevas.", "busy");
  render();
  try {
    const result = await runAlignment({
      action: async (name, extra) => {
        const next = await action(name, extra);
        applyState(next);
        return next;
      },
      show: (message) => showOnProjector(message),
      resume: () => channel.postMessage({ type: "resume" }),
      settle: settleAfter,
      sleep,
      capture: () => jpegFromVideo(.92),
      upload: uploadFrame,
      progress: (text) => { notice(text, "busy"); render(); },
    }, { cameraFixed });
    applyState(result);
    const { alignment } = result;
    if (alignment.status === "ready") {
      notice(`${alignment.detail} ${alignment.overlay.detail}`, alignment.overlay.fill ? "info" : "warn");
    } else {
      notice(alignment.detail, "error");
    }
  } catch (error) {
    notice(error.message, "error");
  } finally {
    channel.postMessage({ type: "resume" });
    aligning = false;
    previewGeneration += 1;
    render();
    schedulePreview(0);
  }
}

function maybeAutoAlign(cameraReady, projectionReady) {
  const busy = aligning || scanBusy || Boolean(tuning) || pickBusy;
  if (shouldAutoAlign({ state, cameraReady, projectorReady: projectionReady, fullscreen: projectorFullscreen,
    busy, tried: autoAlignTried })) {
    setTimeout(alignProjector, 0);
  }
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
  if (previewBusy || scanBusy || tuning || aligning || !stream || !video.videoWidth || currentTrack()?.readyState === "ended") {
    schedulePreview();
    return;
  }
  previewBusy = true;
  const generation = previewGeneration;
  const sentAt = performance.now();
  try {
    const response = await api("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "image/jpeg", "X-Postit-Client": "1",
        "X-Postit-Camera": encodeURIComponent(currentTrack()?.label || "") },
      body: await jpegFromVideo(.8),
    });
    if (generation !== previewGeneration) return;
    if (response.status === 404) {
      previewSupported = false; // server started before live detection existed
      preview = null;
    } else {
      const result = await responseJSON(response);
      if (generation !== previewGeneration) return;
      preview = { ...result, at: performance.now() };
      if (pickCheck && sentAt > pickCheck.at) {
        const message = pickVerdict(pickCheck.role, pickCheck.hex, pickCheck.before, preview);
        if (message) paletteError = { message, at: performance.now() };
        pickCheck = null;
      }
    }
  } catch {
    if (generation === previewGeneration) preview = null; // best effort; the scan itself still reports real errors
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
  return noteColorHex(ctx.getImageData(origin.x, origin.y, SAMPLE_SIZE, SAMPLE_SIZE).data);
}

async function setColor(role, hex) {
  paletteError = null;
  try {
    const before = freshPreview();
    applyState(await action("palette_set", { role, hex }));
    pickCheck = { role, hex, before, at: performance.now() };
    previewGeneration += 1;
    preview = null; // changed colors must not relabel old polygons in the panel
    render();
    if (weakColor(hex)) {
      paletteError = { at: performance.now(), message:
        `El color de ${ROLE_LABELS[role]} es muy pálido (${hex}): puede confundirse con la pared, la piel o la madera. Tocá el centro del post-it.` };
      render();
    }
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

// Where the camera is fixed: through Chrome when it offers manual modes,
// otherwise through the server, which talks to the camera over USB.
async function usbCamera(ids) {
  const query = ids ? `?vendor=${ids.vendor}&product=${ids.product}` : "";
  const response = await api(`/api/camera${query}`, { cache: "no-store" });
  if (response.status === 404) return { available: false, reason: "Reiniciá web.py para fijar la cámara por USB." };
  return responseJSON(response);
}

async function readCameraSettings(track) {
  if (cameraDriver === "usb") return (await usbCamera(cameraUsb)).settings ?? {};
  return track.getSettings();
}

async function applyCamera(track, settings) {
  const sets = lockConstraints(settings, cameraCaps);
  if (cameraDriver === "usb") {
    if (!sets.length) return readCameraSettings(track);
    const response = await api("/api/camera", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Postit-Client": "1" },
      body: JSON.stringify({ ...cameraUsb, settings: mergeSets(sets) }),
    });
    return (await responseJSON(response)).settings;
  }
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
  cameraFixed = CAMERA_GROUPS.some((group) => settings?.[group.mode] === "manual");
  const values = describeCamera(settings);
  const how = cameraDriver === "usb" ? " por USB" : "";
  const text = values ? `Cámara fija${how}: ${values}` : `Cámara fija${how} en los valores actuales`;
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
  cameraDriver = "chrome";
  cameraUsb = null;
  cameraCaps = caps;
  const chromeDiagnosis = cameraDiagnosis(caps);
  let usbReason = null;
  const usbIds = usbIdsFromLabel(track.label);
  if (!cameraSupport(caps).any && !usbIds) {
    // Never ask for "the only USB camera": with the phone or the FaceTime
    // camera active, that would fix the C920 while watching another image.
    usbReason = "no es una cámara USB";
  } else if (!cameraSupport(caps).any) {
    try {
      const usb = await usbCamera(usbIds);
      if (usb.available && cameraSupport(usb.capabilities).any) {
        cameraDriver = "usb";
        cameraUsb = { vendor: usb.device.vendor, product: usb.device.product };
        cameraCaps = usb.capabilities;
      } else {
        usbReason = usb.reason || "la cámara no ofrece controles manuales por USB";
      }
    } catch (error) {
      usbReason = error.message;
    }
  }
  if (track !== currentTrack()) return;
  const support = cameraSupport(cameraCaps);
  const diagnosis = cameraDriver === "usb"
    ? `por USB: ${cameraDiagnosis(cameraCaps)} · Chrome: ${chromeDiagnosis}`
    : chromeDiagnosis;
  console.info("Controles de cámara:", cameraDriver, cameraKeys(cameraCaps), photoError ?? "");
  await reportCamera("capabilities", { driver: cameraDriver, capabilities: cameraKeys(cameraCaps), support,
    diagnosis, chrome_keys: Object.keys(caps), photo_error: photoError, usb_reason: usbReason });
  if (!support.any) {
    cameraFixed = false;
    cameraLock = { tone: "warn", detail: `Chrome: ${chromeDiagnosis} · USB: ${usbReason}`,
      text: isPhoneCamera(track.label)
        ? "La cámara del celular queda en automático: no se puede fijar. Apagá Encuadre centrado y Retrato (Centro de control → Efectos de video) y mantené el celular fijo durante cada intento."
        : `Ni Chrome ni el control USB pueden fijar esta cámara (${usbReason}).` };
    render();
    return;
  }
  try {
    applyState(await responseJSON(await api("/api/state", { cache: "no-store" })));
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
  const response = await api("/api/preview", {
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
  if (!track || tuning || scanBusy || pickBusy || aligning || !cameraCaps || !cameraSupport(cameraCaps).any) return;
  const plan = searchPlan(cameraCaps);
  const projectorReady = Boolean(popupReady && state?.projection?.open && popup && !popup.closed);
  tuning = { done: 0, total: plan.exposureTime.length * plan.colorTemperature.length
    + plan.focusDistance.length + 5 };
  cameraSelect.disabled = true;
  let black = false;
  render();
  try {
    if (projectorReady && attemptPhase(state?.game) !== "running") {
      applyState(await action("projection_neutral"));
      black = true;
      await waitForNeutral();
    }
    const start = await readCameraSettings(track);
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

// Colors are read under the light the game plays in: the projector shows
// its theme background only (no outlines, no ball), so the sample matches
// what live detection will see, in the light theme too (contract 2.2).
async function pickColor(role, point) {
  const projectorReady = Boolean(popupReady && state?.projection?.open && popup && !popup.closed);
  const darken = projectorReady && attemptPhase(state?.game) !== "running";
  let black = false;
  pickBusy = true;
  render();
  try {
    if (darken) {
      notice("Ocultando los contornos para tomar el color…", "busy");
      applyState(await action("projection_neutral"));
      black = true;
      await waitForNeutral();
      await sleep(scanSettleMs(cameraFixed));
    }
    const hex = sampleHex(point);
    clearNotice();
    await setColor(role, hex);
  } catch (error) {
    clearNotice();
    paletteError = { message: error.message, at: performance.now() };
  } finally {
    if (black) {
      await restoreProjection();
      channel.postMessage({ type: "resume" });
    }
    pickBusy = false;
    render();
  }
}

function togglePick(role) {
  paletteError = null;
  pickRole = pickRole === role ? null : role;
  render();
}

async function scanBoard() {
  if (scanBusy || tuning || pickBusy || aligning) return;
  if (!controlsFor(state, Boolean(stream), popupReady && state?.projection?.open).scan) return;
  scanBusy = true;
  render();
  try {
    notice("Leyendo los post-its…", "busy");
    const jpeg = await jpegFromVideo();
    const response = await api("/api/frame", {
      method: "POST",
      headers: { "Content-Type": "image/jpeg", "X-Postit-Client": "1",
        "X-Postit-Camera": encodeURIComponent(currentTrack()?.label || "") },
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
  const resetsColors = name === "palette_reset";
  if (resetsColors) { previewGeneration += 1; preview = null; }
  try {
    clearNotice();
    const next = await action(name, extra);
    if (resetsColors) { previewGeneration += 1; preview = null; }
    applyState(next);
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
  drawLiveDetection(ctx, freshPreview(), state?.palette, width, height);
}

function renderPalette(cameraReady) {
  const palette = state?.palette;
  const editable = Boolean(palette) && previewSupported;
  if (pickRole && (!editable || !cameraReady || tuning || scanBusy)) pickRole = null;
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
    $(`pick-${role}`).disabled = !editable || !cameraReady || Boolean(tuning) || scanBusy || pickBusy;
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
  $("scan-button").disabled = scanBusy || Boolean(tuning) || pickBusy || !controls.scan;
  const lock = tuning ? { tone: "busy", text: `Buscando ajuste fijo de cámara ${tuning.done}/${tuning.total}…` } : cameraLock;
  $("camera-lock").textContent = lock.text;
  $("camera-lock").title = lock.detail ? `${lock.text}\n\nChrome informa: ${lock.detail}` : lock.text;
  $("camera-lock").dataset.tone = lock.tone;
  $("camera-tune").disabled = !cameraReady || Boolean(tuning) || scanBusy || pickBusy || !cameraSupport(cameraCaps).any
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
  const alignment = state?.alignment;
  $("align-projector").disabled = aligning || scanBusy || Boolean(tuning) || pickBusy || !controls.align;
  $("align-projector").dataset.attention = String(guide.step === "align");
  $("align-status").textContent = !alignment ? "" : alignment.status === "ready"
    ? `${alignment.detail} ${alignment.overlay?.detail ?? ""}`.trim()
    : alignment.detail;
  $("align-status").dataset.tone = alignment?.status === "ready" ? (alignment.overlay?.fill ? "ok" : "warn")
    : alignment?.status === "failed" ? "error" : alignment?.status === "stale" ? "warn" : "";
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
  $("count-markers").textContent = scanned ? String(counts.markers) : "—";

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
  maybeAutoAlign(cameraReady, projectionReady);
}

channel.onmessage = (event) => {
  const data = event.data || {};
  if (data.type === "ready") {
    popupReady = true;
    projectorFullscreen = Boolean(data.fullscreen);
    channel.postMessage({ type: "theme", theme: projectorBackdrop });
    render();
  } else if (data.type === "visibility") {
    projectorHidden = Boolean(data.hidden);
    if (projectorHidden) notice("La ventana de proyección está oculta: movela a su propia ventana en el proyector.", "warn");
    else if (localNotice?.message?.startsWith("La ventana de proyección está oculta")) clearNotice();
    render();
  } else if (data.type === "theme") {
    projectorBackdrop = projectorTheme(data.theme); // switched from the projector window
    render();
  } else if (data.type === "closed") {
    popupReady = false;
    render();
  } else if (["blackout_ack", "neutral_ack", "align_ack", "overlay_test_ack"].includes(data.type)) {
    pendingAcks.get(data.id)?.(data);
  } else if (data.type === "projector_error") {
    notice(data.message, "error");
  }
};

$("open-projector").addEventListener("click", openProjector);
$("align-projector").addEventListener("click", () => alignProjector());
// After a failed start the list is still empty: retry the way the page
// starts, so the remembered camera (and the board scanned with it) comes back.
$("retry-camera").addEventListener("click", () => startCamera(cameraSelect.value || "", !cameraSelect.value));
cameraSelect.addEventListener("change", () => {
  rememberCamera(cameraSelect.selectedOptions[0]?.textContent);
  startCamera(cameraSelect.value, false);
});
$("scan-button").addEventListener("click", scanBoard);
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
  preview = null;
  sendAction("palette_reset");
});
$("camera-stage").addEventListener("click", (event) => {
  if (!pickRole || pickBusy || scanBusy || tuning || !stream || !video.videoWidth) return;
  const point = framePointFromClick(event.clientX, event.clientY,
    $("camera-stage").getBoundingClientRect(), video.videoWidth, video.videoHeight);
  if (!point) return;
  const role = pickRole;
  pickRole = null;
  pickColor(role, point);
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
  } else if (key === "a" && !$("align-projector").disabled) {
    event.preventDefault();
    alignProjector();
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
// The iPhone joins the list a few seconds after it wakes up near the Mac,
// often after the page already fell back to the FaceTime camera.
navigator.mediaDevices?.addEventListener?.("devicechange", async () => {
  const activeId = stream?.getVideoTracks()[0]?.getSettings().deviceId || "";
  try {
    await updateCameraList(activeId);
    const target = cameraToSwitchTo({
      devices: await navigator.mediaDevices.enumerateDevices(),
      activeId,
      remembered: rememberedCamera(),
      scanned: Boolean(state?.camera_board || state?.board),
      busy: cameraBusy || scanBusy,
    });
    if (target) await startCamera(target, false);
  } catch {
    // The selector still lists every camera; the operator can pick one.
  }
});

// Over USB nothing tells the panel if the camera goes back to automatic
// (unplugged, the Mac slept, another app touched it), so it checks.
const WATCH_CAMERA_MS = 20000;
async function watchCamera() {
  if (cameraDriver !== "usb" || tuning || scanBusy || !currentTrack()) return;
  const preset = state?.camera_presets?.[cameraLabel];
  if (!preset) return;
  try {
    const current = (await usbCamera(cameraUsb)).settings;
    const drifted = driftedModes(preset.settings, current);
    if (!drifted.length) return;
    const actual = await applyCamera(currentTrack(), preset.settings);
    const fixed = fixedPart(actual, cameraCaps);
    await reportCamera("reapplied", { drifted, actual: cameraKeys(actual), unfixed: fixed.unfixed });
    cameraLock = { ...lockStatus(fixed.settings, fixed, preset.black), detail: cameraLock.detail };
    render();
  } catch {
    // The next check tries again; a failed check changes nothing.
  }
}

async function pollState() {
  if (polling) return;
  polling = true;
  try {
    const response = await api("/api/state", { cache: "no-store" });
    const next = await responseJSON(response);
    if (connectionNotice && localNotice === connectionNotice) clearNotice();
    connectionNotice = null;
    applyState(next);
    if (popup && popup.closed) popupReady = false;
  } catch (error) {
    notice(`No se puede conectar con el servidor local: ${error.message}`, "error");
    connectionNotice = localNotice;
  } finally {
    polling = false;
  }
}

render();
pollState();
setInterval(pollState, 300);
setInterval(watchCamera, WATCH_CAMERA_MS);
startCamera("", true);
schedulePreview(0);
