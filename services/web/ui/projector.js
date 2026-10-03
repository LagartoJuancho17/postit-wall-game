import {
  isThemeShortcut,
  pointInCanvas,
  projectionSize,
  projectorBackground,
  projectorColors,
  projectorTheme,
  THEME_KEY,
} from "./ui-core.mjs";

const canvas = document.getElementById("projector-canvas");
const ctx = canvas.getContext("2d", { alpha: false });
const channel = new BroadcastChannel("postit-wall");
const fullscreenButton = document.getElementById("fullscreen-button");
const confirmButton = document.getElementById("confirm-target");
const dock = document.getElementById("calibration-dock");
const toast = document.getElementById("projector-toast");
const stateLabel = document.getElementById("projector-state");
const calibrationLabel = document.getElementById("calibration-label");

let state = null;
let localTarget = null;
let targetIndex = -1;
let blackoutForced = false;
let dragging = false;
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
  const response = await fetch("/api/action", {
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
  const target = state?.projection?.target;
  if (!dragging && target && (target.index !== targetIndex || !localTarget)) {
    localTarget = { x: target.x, y: target.y };
    targetIndex = target.index;
  } else if (!target) {
    localTarget = null;
    targetIndex = -1;
  }
  draw();
}

function drawBackground() {
  // A scan or camera search needs pure black whatever the theme.
  ctx.fillStyle = projectorBackground(theme, blackoutForced);
  ctx.fillRect(0, 0, viewport.width, viewport.height);
}

function drawPolygon(polygon, color, width = 3) {
  if (!polygon?.points?.length) return;
  ctx.beginPath();
  polygon.points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.closePath();
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.stroke();
}

function drawBoard(board, grid = false) {
  if (!board) return;
  const colors = projectorColors(theme);
  drawPolygon(board.playfield, colors.playfield, 3);
  if (grid && board.playfield?.points?.length === 4) {
    const [tl, tr, br, bl] = board.playfield.points;
    const mix = (a, b, t) => ({ x: a.x + (b.x - a.x) * t, y: a.y + (b.y - a.y) * t });
    ctx.strokeStyle = colors.grid;
    ctx.lineWidth = 1;
    for (const t of [.25, .5, .75]) {
      const lines = [[mix(tl, bl, t), mix(tr, br, t)], [mix(tl, tr, t), mix(bl, br, t)]];
      for (const [a, b] of lines) {
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
    }
  }
  drawPolygon(board.start, colors.start, 4);
  drawPolygon(board.goal, colors.goal, 4);
  board.bumpers?.forEach((polygon) => drawPolygon(polygon, colors.bumper, 3));
}

function drawTarget(target) {
  const colors = projectorColors(theme);
  const x = target.x;
  const y = target.y;
  ctx.strokeStyle = colors.cross;
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.moveTo(x - 29, y);
  ctx.lineTo(x + 29, y);
  ctx.moveTo(x, y - 29);
  ctx.lineTo(x, y + 29);
  ctx.stroke();
  ctx.strokeStyle = colors.crossRing;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.arc(x, y, 9, 0, Math.PI * 2);
  ctx.stroke();
}

function drawGame(game, board) {
  if (!board || !game?.position) return;
  const colors = projectorColors(theme);
  drawBoard(board);
  const ball = game.position;
  if (game.status === "ready") {
    const angle = (game.aim_degrees ?? -45) * Math.PI / 180;
    ctx.strokeStyle = colors.ball;
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(ball.x, ball.y);
    ctx.lineTo(ball.x + 70 * Math.cos(angle), ball.y + 70 * Math.sin(angle));
    ctx.stroke();
  }
  ctx.fillStyle = colors.ball;
  ctx.beginPath();
  ctx.arc(ball.x, ball.y, 10, 0, Math.PI * 2);
  ctx.fill();
  if (game.status === "won" || game.status === "lost") {
    ctx.font = "700 42px -apple-system, BlinkMacSystemFont, sans-serif";
    ctx.textAlign = "center";
    ctx.fillStyle = game.status === "won" ? colors.won : colors.lost;
    ctx.fillText(game.status === "won" ? "¡LLEGASTE!" : "FUERA DEL CAMPO", viewport.width / 2, 75);
  }
}

function draw() {
  drawBackground();
  const mode = blackoutForced ? "black" : (state?.projection?.mode || "black");
  document.body.dataset.mode = mode;
  document.body.dataset.theme = theme;
  document.body.dataset.blackout = String(blackoutForced);
  const calibrating = mode === "calibration" && Boolean(state?.projection?.target);
  dock.hidden = !calibrating;
  stateLabel.textContent = calibrating
    ? "Calibración · " + (state?.projection?.target?.index ?? 1) + "/4"
    : mode === "game" ? "Post-it Wall" : "";
  if (mode === "black") return;
  if (calibrating) {
    const index = state?.projection?.target?.index ?? state?.calibration?.index ?? 1;
    calibrationLabel.textContent = `Esquina ${index}/4 · ${state.projection.target.label || "post-it de esquina"}`;
    drawTarget(localTarget || state.projection.target);
  } else if (mode === "game") {
    drawGame(state?.game, state?.board);
  }
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

function moveTarget(point) {
  if (state?.projection?.mode !== "calibration" || !state?.projection?.target) return;
  localTarget = {
    x: Math.max(0, Math.min(viewport.width - 1, point.x)),
    y: Math.max(0, Math.min(viewport.height - 1, point.y)),
  };
  draw();
}

async function confirmTarget() {
  if (!localTarget || state?.projection?.mode !== "calibration") return;
  try {
    applyState(await action("calibration_confirm", localTarget));
    toast.hidden = true;
  } catch (error) {
    showError(error);
  }
}

canvas.addEventListener("pointerdown", (event) => {
  if (state?.projection?.mode !== "calibration") return;
  dragging = true;
  canvas.setPointerCapture(event.pointerId);
  moveTarget(pointInCanvas(event.clientX, event.clientY, canvas.getBoundingClientRect(), viewport.width, viewport.height));
});
canvas.addEventListener("pointermove", (event) => {
  if (!dragging) return;
  moveTarget(pointInCanvas(event.clientX, event.clientY, canvas.getBoundingClientRect(), viewport.width, viewport.height));
});
canvas.addEventListener("pointerup", (event) => {
  if (!dragging) return;
  dragging = false;
  canvas.releasePointerCapture(event.pointerId);
  moveTarget(localTarget);
});
canvas.addEventListener("pointercancel", () => { dragging = false; });

document.addEventListener("keydown", (event) => {
  if (isThemeShortcut(event)) {
    event.preventDefault();
    setTheme(theme === "dark" ? "light" : "dark");
    return;
  }
  if (state?.projection?.mode !== "calibration" || !localTarget) return;
  const step = event.shiftKey ? 1 : 8;
  const moves = {
    ArrowLeft: [-step, 0],
    ArrowRight: [step, 0],
    ArrowUp: [0, -step],
    ArrowDown: [0, step],
  };
  if (moves[event.key]) {
    event.preventDefault();
    const [dx, dy] = moves[event.key];
    moveTarget({ x: localTarget.x + dx, y: localTarget.y + dy });
  } else if (event.key === "Enter") {
    event.preventDefault();
    confirmTarget();
  }
});

confirmButton.addEventListener("click", confirmTarget);
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
  fetch("/api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Postit-Client": "1" },
    body: JSON.stringify({ action: "projection_close" }),
    keepalive: true,
  }).catch(() => {});
});

async function tick() {
  if (tickBusy) return;
  tickBusy = true;
  try {
    const response = await fetch("/api/tick", { cache: "no-store" });
    applyState(await responseJSON(response));
  } catch (error) {
    showError(error);
  } finally {
    tickBusy = false;
  }
}

resizeCanvas();
syncProjectionSize();
setInterval(tick, 33);
