import {
  isThemeShortcut,
  projectionSize,
  projectorBackground,
  projectorTheme,
  THEME_KEY,
  withTimeout,
} from "./ui-core.mjs";
import { createApi } from "./api.mjs";
import { drawAlignmentPattern, drawProjectorScene, LIVE_MAX_AGE_MS, projectorScene } from "./live-view.mjs";

const api = createApi("projector");

const canvas = document.getElementById("projector-canvas");
const ctx = canvas.getContext("2d", { alpha: false });
const channel = new BroadcastChannel("postit-wall");
const fullscreenButton = document.getElementById("fullscreen-button");
const toast = document.getElementById("projector-toast");
const stateLabel = document.getElementById("projector-state");

let state = null;
let stateReceivedAt = 0;
let blackoutForced = false;
// What the panel asked this window to show, ahead of state.projection.mode
// (docs/alignment-and-color.md 3): {kind: "pattern", layout, frame},
// {kind: "neutral"} or {kind: "overlay_test"}. The channel handlers set it
// and {type: "resume"} clears it; null draws from the state.
let localScene = null;
let tickBusy = false;
let resizeTimer = null;
let viewport = projectionSize(window);
let theme = storedTheme();

function storedTheme() {
  try {
    return projectorTheme(localStorage.getItem(THEME_KEY));
  } catch {
    return "dark"; // storage blocked: the panel still sends its choice
  }
}

function setTheme(next) {
  theme = projectorTheme(next);
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    // Not remembered across reloads; the switch still works now.
  }
  channel.postMessage({ type: "theme", theme });
  draw();
}

function showError(error) {
  toast.textContent = error.message || String(error);
  toast.hidden = false;
  channel.postMessage({ type: "projector_error", message: toast.textContent });
}

async function responseJSON(response) {
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new Error(`El servidor respondió con HTTP ${response.status} sin datos válidos.`);
  }
  if (!response.ok) throw new Error(payload.error || payload.detail || `Error HTTP ${response.status}.`);
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

function resizeCanvas() {
  viewport = projectionSize(window);
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(viewport.width * dpr);
  canvas.height = Math.round(viewport.height * dpr);
  canvas.style.width = `${viewport.width}px`;
  canvas.style.height = `${viewport.height}px`;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

let lastRegister = 0;

function applyState(next) {
  state = next;
  stateReceivedAt = performance.now();
  // A backend that forgot this window (web.py restarted, or the panel
  // reloaded and started a new in-browser backend) learns it again.
  if (next?.projection?.open === false && performance.now() - lastRegister > 1000) {
    lastRegister = performance.now();
    registerProjector();
  }
  draw();
}

function projectorMetrics() {
  return { width: viewport.width, height: viewport.height, dpr: window.devicePixelRatio || 1,
    fullscreen: Boolean(document.fullscreenElement) };
}

async function registerProjector() {
  try {
    const next = await action("projection_open", { width: viewport.width, height: viewport.height });
    state = next;
    stateReceivedAt = performance.now();
    toast.hidden = true;
    channel.postMessage({ type: "ready", ...projectorMetrics() });
  } catch (error) {
    showError(error);
  }
}

/** Draw the requested scene, let the compositor show it, then confirm. */
async function ackAfterDraw(type, id) {
  draw();
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  channel.postMessage({ type, id, ...projectorMetrics() });
}

function drawBackground() {
  // A scan or camera search needs pure black whatever the theme.
  ctx.fillStyle = projectorBackground(theme, blackoutForced);
  ctx.fillRect(0, 0, viewport.width, viewport.height);
}

function draw() {
  drawBackground();
  const scene = projectorScene({ blackout: blackoutForced, local: localScene, mode: state?.projection?.mode });
  document.body.dataset.mode = scene.mode;
  document.body.dataset.theme = theme;
  document.body.dataset.blackout = String(blackoutForced);
  // Anything the camera is about to capture hides the window's own labels.
  document.body.dataset.capture = String(Boolean(localScene || blackoutForced));
  stateLabel.textContent = scene.kind === "live" ? "Post-it Wall · Detección en vivo" : "";
  if (scene.kind === "black") {
    // "black" and "align" before its first pattern: pure black whatever the theme.
    ctx.fillStyle = projectorBackground(theme, true);
    ctx.fillRect(0, 0, viewport.width, viewport.height);
    return;
  }
  if (scene.kind === "pattern") {
    drawAlignmentPattern(ctx, scene.layout, scene.frame, viewport.width, viewport.height);
    return;
  }
  if (scene.kind === "neutral") return; // the theme background only: colors are sampled under play light
  // A stale reply never reaches the renderer, and the renderer refuses stale notes too.
  const fresh = performance.now() - stateReceivedAt < LIVE_MAX_AGE_MS;
  drawProjectorScene(ctx, {
    state, preview: fresh ? state?.live_detection : null, palette: state?.palette,
    theme, width: viewport.width, height: viewport.height, fresh, forceFill: scene.forceFill,
  });
}

async function syncProjectionSize() {
  const previous = viewport;
  resizeCanvas();
  if (previous.width === viewport.width && previous.height === viewport.height && state) {
    channel.postMessage({ type: "ready", ...projectorMetrics() }); // fullscreen may have changed
    return;
  }
  lastRegister = performance.now();
  await registerProjector();
}

document.addEventListener("keydown", (event) => {
  if (isThemeShortcut(event)) {
    event.preventDefault();
    setTheme(theme === "dark" ? "light" : "dark");
  }
});

fullscreenButton.addEventListener("click", async () => {
  try {
    await document.documentElement.requestFullscreen();
    await syncProjectionSize();
  } catch (error) {
    showError(new Error("No se pudo activar pantalla completa. Usá el botón en esta ventana: " + error.message));
  }
});
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(syncProjectionSize, 100);
});
document.addEventListener("fullscreenchange", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(syncProjectionSize, 100);
});

channel.onmessage = async (event) => {
  const data = event.data || {};
  if (data.type === "blackout") {
    blackoutForced = true;
    await ackAfterDraw("blackout_ack", data.id);
  } else if (data.type === "align_pattern") {
    localScene = { kind: "pattern", layout: data.layout, frame: data.frame };
    await ackAfterDraw("align_ack", data.id);
  } else if (data.type === "neutral") {
    localScene = { kind: "neutral" };
    await ackAfterDraw("neutral_ack", data.id);
  } else if (data.type === "overlay_test") {
    localScene = { kind: "overlay_test" };
    await ackAfterDraw("overlay_test_ack", data.id);
  } else if (data.type === "resume") {
    blackoutForced = false;
    localScene = null;
    draw();
  } else if (data.type === "theme") {
    theme = projectorTheme(data.theme);
    draw();
  }
};

// A projector left as a background tab is throttled: no animation frames,
// ~1 timer per second. The panel warns the operator.
document.addEventListener("visibilitychange", () => {
  channel.postMessage({ type: "visibility", hidden: document.hidden });
});

window.addEventListener("storage", (event) => {
  if (event.key !== THEME_KEY) return;
  theme = projectorTheme(event.newValue);
  draw();
});

window.addEventListener("pagehide", () => {
  channel.postMessage({ type: "closed" });
  api("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Postit-Client": "1" },
    body: JSON.stringify({ action: "projection_close" }),
    keepalive: true,
  }).catch(() => {});
});

async function tick() {
  if (tickBusy) return;
  tickBusy = true;
  const request = new AbortController();
  try {
    const next = await withTimeout(
      api("/api/tick", { cache: "no-store", signal: request.signal }).then(responseJSON),
      LIVE_MAX_AGE_MS, "Se interrumpió la actualización del proyector.",
    );
    applyState(next);
    toast.hidden = true;
  } catch (error) {
    state = null; // never leave old detections on the wall after disconnection
    draw();
    showError(error);
  } finally {
    request.abort();
    tickBusy = false;
  }
}

resizeCanvas();
syncProjectionSize();
channel.postMessage({ type: "visibility", hidden: document.hidden });
// Draw independently of HTTP: a hung request cannot keep old contours alive.
setInterval(() => { draw(); tick(); }, 33);
