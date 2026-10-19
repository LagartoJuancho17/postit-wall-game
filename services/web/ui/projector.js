import {
  isThemeShortcut,
  projectionSize,
  projectorBackground,
  projectorTheme,
  THEME_KEY,
  withTimeout,
} from "./ui-core.mjs";
import { createApi } from "./api.mjs";
import { drawLiveDetection, drawLiveGame, LIVE_MAX_AGE_MS } from "./live-view.mjs";

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

function applyState(next) {
  state = next;
  stateReceivedAt = performance.now();
  draw();
}

function drawBackground() {
  // A scan or camera search needs pure black whatever the theme.
  ctx.fillStyle = projectorBackground(theme, blackoutForced);
  ctx.fillRect(0, 0, viewport.width, viewport.height);
}

function draw() {
  drawBackground();
  const mode = blackoutForced ? "black" : (state?.projection?.mode || "live");
  document.body.dataset.mode = mode;
  document.body.dataset.theme = theme;
  document.body.dataset.blackout = String(blackoutForced);
  stateLabel.textContent = mode === "black" ? "" : "Post-it Wall · Detección en vivo";
  if (mode === "black") return;
  const live = performance.now() - stateReceivedAt < LIVE_MAX_AGE_MS ? state?.live_detection : null;
  drawLiveDetection(ctx, live, state?.palette, viewport.width, viewport.height, theme);
  drawLiveGame(ctx, state?.game, state?.camera?.width, state?.camera?.height,
    viewport.width, viewport.height, theme);
}

async function syncProjectionSize() {
  const previous = viewport;
  resizeCanvas();
  if (previous.width === viewport.width && previous.height === viewport.height && state) return;
  try {
    applyState(await action("projection_open", viewport));
    toast.hidden = true;
    channel.postMessage({ type: "ready" });
  } catch (error) {
    showError(error);
  }
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
    draw();
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    channel.postMessage({ type: "blackout_ack", id: data.id });
  } else if (data.type === "resume") {
    blackoutForced = false;
    draw();
  } else if (data.type === "theme") {
    theme = projectorTheme(data.theme);
    draw();
  }
};

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
// Draw independently of HTTP: a hung request cannot keep old contours alive.
setInterval(() => { draw(); tick(); }, 33);
