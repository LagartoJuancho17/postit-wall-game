/**
 * Closed loop in a real Chrome: the panel, the projector window and either
 * backend (the Python server, or the static Vercel build with its
 * in-browser backend), with a simulated camera that films the wall lit by
 * the projector.
 *
 * The camera (an init script in the panel page) renders, for every camera
 * pixel, wall reflectance × (ambient + projector light), where the light is
 * read from the projector window's own canvas and carried into the camera
 * by a known homography G (projector -> camera). Nothing is mocked between
 * the camera and the projector: the panel's real alignment run (coded
 * patterns, acks, settle, uploads), the backend's decoder, the overlay
 * check, the live detection and the projector's drawing all run as shipped.
 *
 *   node tests/closed-loop-browser.mjs [server|static|https://…][:normal|:bright]...
 *   (default: server, static, static:bright; a URL runs against that
 *   deployment of the static build, e.g. the Vercel one)
 *
 * Passes when, for each backend: the alignment decodes the true mapping
 * (projector px error at the papers < 1 px), the overlay check completes,
 * the live outlines sit on the true papers (< 1.5 camera px), every paper
 * keeps a rim with clearance >= 1 camera px (our rim never lights the
 * paper), detection never flips, and an attempt plays in projector px. The
 * papers carry the role color only when the overlay check allowed it, and
 * neutral light otherwise: a normal room turns the fill off (the probes
 * read as post-its), a bright room keeps it.
 * Writes report.json and screenshots to POSTIT_BROWSER_OUTPUT.
 */
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { build } from "../scripts/build-static.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = process.env.POSTIT_BROWSER_OUTPUT || "/tmp/postit-closed-loop";
mkdirSync(output, { recursive: true });
const python = process.env.POSTIT_PYTHON || (existsSync(join(root, ".venv/bin/python")) ? join(root, ".venv/bin/python") : "python3");
const GSTACK_PLAYWRIGHT = "/Users/tobiasarraiza/gstack/node_modules/playwright/index.mjs";
const playwrightModule = process.env.POSTIT_PLAYWRIGHT_MODULE || (existsSync(GSTACK_PLAYWRIGHT) ? GSTACK_PLAYWRIGHT : null);
const playwright = playwrightModule ? await import(pathToFileURL(resolve(playwrightModule)).href) : await import("playwright");
const runs = (process.argv.slice(2).length ? process.argv.slice(2) : ["server", "static", "static:bright"])
  .map((arg) => {
    const remote = arg.match(/^(https?:\/\/[^\s]+?)(?::(normal|bright))?$/);
    if (remote) return { kind: "remote", url: remote[1].replace(/\/$/, ""), room: remote[2] ?? "normal" };
    const [kind, room = "normal"] = arg.split(":");
    if (!["server", "static"].includes(kind) || !["normal", "bright"].includes(room)) throw new Error(`unknown run ${arg}`);
    return { kind, room };
  });

// ------------------------------------------------------------------ the rig

const CAMERA = { width: 960, height: 720 };
const PROJECTOR = { width: 1280, height: 800 };
/** Where the projector's corners land in the camera, continuous camera coordinates. */
const QUAD = [[30, 25], [930, 20], [935, 700], [25, 695]];
const WALL = [204, 204, 204];                  // reflectance 0.8
const BLACK_LEVEL = 0.02;                       // light a black projector frame still emits
/** Room light (projector full white = 1) and the fixed exposure that suits it. */
const ROOMS = { normal: { ambient: 0.35, gain: 2.0 }, bright: { ambient: 1.0, gain: 0.75 } };
const COLORS = { start: "#e6e617", goal: "#e66a17", bumper: "#17e639", marker: "#e617b6" };

function rotated(cx, cy, w, h, degrees) {
  const a = (degrees * Math.PI) / 180;
  return [[-w / 2, -h / 2], [w / 2, -h / 2], [w / 2, h / 2], [-w / 2, h / 2]]
    .map(([x, y]) => [cx + Math.cos(a) * x - Math.sin(a) * y, cy + Math.sin(a) * x + Math.cos(a) * y]);
}

/** Papers in continuous camera coordinates (a pixel i spans [i, i + 1)). */
const PAPERS = [
  { role: "marker", points: rotated(110, 105, 44, 44, 6) },
  { role: "marker", points: rotated(850, 100, 44, 44, -4) },
  { role: "marker", points: rotated(855, 615, 44, 44, 9) },
  { role: "marker", points: rotated(105, 610, 44, 44, -7) },
  { role: "start", points: rotated(250, 360, 58, 58, 12) },
  { role: "goal", points: rotated(720, 350, 60, 58, -10) },
  { role: "bumper", points: rotated(480, 230, 72, 50, 21) },
  { role: "bumper", points: rotated(470, 500, 66, 48, -15) },
];

/** 4-point homography (continuous coordinates), by Gaussian elimination. */
function homography(from, to) {
  const rows = [];
  const rhs = [];
  from.forEach(([x, y], k) => {
    const [u, v] = to[k];
    rows.push([x, y, 1, 0, 0, 0, -u * x, -u * y]);
    rhs.push(u);
    rows.push([0, 0, 0, x, y, 1, -v * x, -v * y]);
    rhs.push(v);
  });
  const n = 8;
  const m = rows.map((row, i) => [...row, rhs[i]]);
  for (let c = 0; c < n; c += 1) {
    let pivot = c;
    for (let r = c + 1; r < n; r += 1) if (Math.abs(m[r][c]) > Math.abs(m[pivot][c])) pivot = r;
    [m[c], m[pivot]] = [m[pivot], m[c]];
    for (let r = 0; r < n; r += 1) {
      if (r === c) continue;
      const f = m[r][c] / m[c][c];
      for (let k = c; k <= n; k += 1) m[r][k] -= f * m[c][k];
    }
  }
  const h = m.map((row, i) => row[n] / row[i]);
  return [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1]];
}

const apply = (H, [x, y]) => {
  const w = H[2][0] * x + H[2][1] * y + H[2][2];
  return [(H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w];
};

const G = homography([[0, 0], [PROJECTOR.width, 0], [PROJECTOR.width, PROJECTOR.height], [0, PROJECTOR.height]], QUAD);
const G_INVERSE = homography(QUAD, [[0, 0], [PROJECTOR.width, 0], [PROJECTOR.width, PROJECTOR.height], [0, PROJECTOR.height]]);
/** The backend's H in its own convention: camera pixel index -> projector canvas coordinates. */
const TRUE_H = ([i, j]) => apply(G_INVERSE, [i + 0.5, j + 0.5]);

/**
 * The camera, installed in the panel page before its scripts run. Every
 * frame: projector canvas -> light in camera space (bilinear, through
 * G^-1 precomputed per pixel) -> wall × (ambient + light) × gain.
 */
function installFakeCamera({ camera, projector, gInverse, wall, papers, colors, ambient, blackLevel, gain }) {
  const { width, height } = camera;
  const reflect = document.createElement("canvas");
  reflect.width = width;
  reflect.height = height;
  const rctx = reflect.getContext("2d");
  rctx.fillStyle = `rgb(${wall.join(",")})`;
  rctx.fillRect(0, 0, width, height);
  for (const paper of papers) {
    rctx.fillStyle = colors[paper.role];
    rctx.beginPath();
    paper.points.forEach(([x, y], k) => (k ? rctx.lineTo(x, y) : rctx.moveTo(x, y)));
    rctx.closePath();
    rctx.fill();
  }
  const reflectance = Float32Array.from(rctx.getImageData(0, 0, width, height).data, (v) => v / 255);
  // Projector pixel-index coordinates of every camera pixel center.
  const map = new Float32Array(width * height * 2);
  for (let j = 0; j < height; j += 1) {
    for (let i = 0; i < width; i += 1) {
      const x = i + 0.5;
      const y = j + 0.5;
      const w = gInverse[2][0] * x + gInverse[2][1] * y + gInverse[2][2];
      map[2 * (j * width + i)] = (gInverse[0][0] * x + gInverse[0][1] * y + gInverse[0][2]) / w - 0.5;
      map[2 * (j * width + i) + 1] = (gInverse[1][0] * x + gInverse[1][1] * y + gInverse[1][2]) / w - 0.5;
    }
  }
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const image = ctx.createImageData(width, height);
  const light = new Float32Array(width * height * 3);
  let projectorWindow = null;
  const open = window.open;
  window.open = function openProjector(...args) {
    projectorWindow = open.apply(this, args);
    return projectorWindow;
  };
  const projectorPixels = () => {
    const target = projectorWindow && !projectorWindow.closed
      ? projectorWindow.document?.getElementById("projector-canvas") : null;
    if (!target || target.width !== projector.width || target.height !== projector.height) return null;
    return target.getContext("2d").getImageData(0, 0, target.width, target.height).data;
  };
  let frames = 0;
  function paint() {
    const source = projectorPixels();
    const pw = projector.width;
    const ph = projector.height;
    for (let k = 0, p = 0; k < width * height; k += 1, p += 4) {
      let r = 0;
      let g = 0;
      let b = 0;
      if (source) {
        const x = map[2 * k];
        const y = map[2 * k + 1];
        if (x >= -0.5 && y >= -0.5 && x <= pw - 0.5 && y <= ph - 0.5) {
          const cx = Math.min(Math.max(x, 0), pw - 1);
          const cy = Math.min(Math.max(y, 0), ph - 1);
          const x0 = Math.floor(cx);
          const y0 = Math.floor(cy);
          const x1 = Math.min(x0 + 1, pw - 1);
          const y1 = Math.min(y0 + 1, ph - 1);
          const fx = cx - x0;
          const fy = cy - y0;
          const a = (y0 * pw + x0) * 4;
          const bb = (y0 * pw + x1) * 4;
          const c = (y1 * pw + x0) * 4;
          const d = (y1 * pw + x1) * 4;
          const lerp = (o) => {
            const top = source[a + o] + fx * (source[bb + o] - source[a + o]);
            const bottom = source[c + o] + fx * (source[d + o] - source[c + o]);
            return (top + fy * (bottom - top)) / 255;
          };
          r = blackLevel + (1 - blackLevel) * lerp(0);
          g = blackLevel + (1 - blackLevel) * lerp(1);
          b = blackLevel + (1 - blackLevel) * lerp(2);
        }
      }
      light[3 * k] = r;
      light[3 * k + 1] = g;
      light[3 * k + 2] = b;
      image.data[p] = Math.min(255, 255 * gain * reflectance[p] * (ambient + r));
      image.data[p + 1] = Math.min(255, 255 * gain * reflectance[p + 1] * (ambient + g));
      image.data[p + 2] = Math.min(255, 255 * gain * reflectance[p + 2] * (ambient + b));
      image.data[p + 3] = 255;
    }
    ctx.putImageData(image, 0, 0);
    frames += 1;
  }
  paint();
  setInterval(paint, 50);
  const label = "Cámara simulada (lazo cerrado)";
  const device = { kind: "videoinput", deviceId: "closed-loop", groupId: "closed-loop", label, toJSON() { return this; } };
  // A fresh stream per request: the panel stops the tracks it no longer
  // uses, and a stopped track never delivers frames again.
  navigator.mediaDevices.getUserMedia = async () => {
    const stream = canvas.captureStream(20);
    const track = stream.getVideoTracks()[0];
    Object.defineProperty(track, "label", { value: label });
    const settings = track.getSettings.bind(track);
    track.getSettings = () => ({ ...settings(), deviceId: device.deviceId });
    return stream;
  };
  navigator.mediaDevices.enumerateDevices = async () => [device];

  /** Signed distance of a point to a convex polygon, positive outside. */
  const outside = (poly, x, y) => {
    let area = 0;
    poly.forEach(([ax, ay], k) => {
      const [bx, by] = poly[(k + 1) % poly.length];
      area += ax * by - bx * ay;
    });
    const sign = Math.sign(area);
    return Math.max(...poly.map(([ax, ay], k) => {
      const [bx, by] = poly[(k + 1) % poly.length];
      return (sign * ((by - ay) * (x - ax) - (bx - ax) * (y - ay))) / Math.hypot(bx - ax, by - ay);
    }));
  };
  window.closedLoopCamera = {
    frames: () => frames,
    /** Per paper: the mean projector light (r, g, b) well inside it (>= 4 px from the edge). */
    fills() {
      return papers.map((paper) => {
        const xs = paper.points.map(([x]) => x);
        const ys = paper.points.map(([, y]) => y);
        const sum = [0, 0, 0];
        let count = 0;
        for (let j = Math.floor(Math.min(...ys)); j <= Math.ceil(Math.max(...ys)); j += 1) {
          for (let i = Math.floor(Math.min(...xs)); i <= Math.ceil(Math.max(...xs)); i += 1) {
            if (outside(paper.points, i + 0.5, j + 0.5) > -4) continue;
            const k = j * width + i;
            for (let c = 0; c < 3; c += 1) sum[c] += light[3 * k + c];
            count += 1;
          }
        }
        return { role: paper.role, light: sum.map((value) => value / Math.max(count, 1)), pixels: count };
      });
    },
    png: () => canvas.toDataURL("image/png"),
    /**
     * Per paper: rim-bright projector light (>= 0.6) near it, and how far
     * outside it stays. `exclude`: capsules {a, b, r} in camera coordinates
     * (the ball and its aim line, white by design and drawn on the start).
     */
    rims(exclude = []) {
      const excluded = (x, y) => exclude.some(({ a, b, r }) => {
        const [dx, dy] = [b[0] - a[0], b[1] - a[1]];
        const t = Math.max(0, Math.min(1, ((x - a[0]) * dx + (y - a[1]) * dy) / (dx * dx + dy * dy || 1)));
        return Math.hypot(x - (a[0] + t * dx), y - (a[1] + t * dy)) < r;
      });
      return papers.map((paper) => {
        const xs = paper.points.map(([x]) => x);
        const ys = paper.points.map(([, y]) => y);
        let count = 0;
        let clearance = Infinity;
        let onPaper = 0;
        for (let j = Math.floor(Math.min(...ys) - 14); j <= Math.ceil(Math.max(...ys) + 14); j += 1) {
          for (let i = Math.floor(Math.min(...xs) - 14); i <= Math.ceil(Math.max(...xs) + 14); i += 1) {
            const k = j * width + i;
            if (Math.max(light[3 * k], light[3 * k + 1], light[3 * k + 2]) < 0.6) continue;
            const d = outside(paper.points, i + 0.5, j + 0.5);
            if (d > 12 || excluded(i + 0.5, j + 0.5)) continue;
            count += 1;
            clearance = Math.min(clearance, d);
            if (d < 0) onPaper += 1;
          }
        }
        return { role: paper.role, rimPixels: count, clearance, onPaper };
      });
    },
  };
}

// ------------------------------------------------------------------ backends

async function until(description, predicate, timeout = 20000, every = 150) {
  const end = Date.now() + timeout;
  let last;
  while (Date.now() < end) {
    last = await predicate();
    if (last) return last;
    await new Promise((done) => setTimeout(done, every));
  }
  throw new Error(`Timed out: ${description}`);
}

async function startPythonServer() {
  let log = "";
  const child = spawn(python, ["-u", "-c", `
from pathlib import Path
from services.web.backend.server import GameController, create_server
controller = GameController(log_path=Path(${JSON.stringify(join(output, "server-events.jsonl"))}))
server = create_server(port=0, controller=controller)
print(server.server_address[1], flush=True)
server.serve_forever(poll_interval=0.1)
`], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
  child.stdout.on("data", (chunk) => { log += chunk; });
  child.stderr.on("data", (chunk) => { log += chunk; });
  const port = await until("python server starts", () => {
    if (child.exitCode !== null) throw new Error(`server exited: ${log}`);
    return log.match(/^(\d+)\s*$/m)?.[1];
  });
  return { url: `http://127.0.0.1:${port}`, stop: () => child.kill("SIGTERM"), log: () => log };
}

const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json" };

/** dist/ served the way Vercel serves it with cleanUrls (scripts/serve-static.mjs). */
async function startStaticBuild() {
  const dir = mkdtempSync(join(tmpdir(), "postit-closed-loop-dist-"));
  build(dir);
  const server = createServer((request, response) => {
    const path = normalize(decodeURIComponent(new URL(request.url, "http://x").pathname));
    let file = join(dir, path === "/" ? "index.html" : path);
    if (!extname(file) && existsSync(`${file}.html`)) file = `${file}.html`;
    if (!file.startsWith(dir) || !existsSync(file) || statSync(file).isDirectory()) {
      response.writeHead(404).end("404");
      return;
    }
    response.writeHead(200, { "Content-Type": TYPES[extname(file)] || "application/octet-stream" });
    response.end(readFileSync(file));
  });
  await new Promise((done) => server.listen(0, "127.0.0.1", done));
  return { url: `http://127.0.0.1:${server.address().port}`, stop: () => { server.close(); rmSync(dir, { recursive: true, force: true }); },
    log: () => "" };
}

// ------------------------------------------------------------------ one run

async function run({ kind, room, url }) {
  const name = `${kind}-${room}`;
  const backend = kind === "server" ? await startPythonServer()
    : kind === "remote" ? { url, stop: () => {}, log: () => "" } : await startStaticBuild();
  const chrome = process.env.POSTIT_BROWSER_EXECUTABLE || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  const browser = await playwright.chromium.launch({ headless: true, ...(existsSync(chrome) ? { executablePath: chrome } : {}),
    args: ["--autoplay-policy=no-user-gesture-required"] });
  const errors = [];
  const timeline = [];   // every /api request: what, when it started and ended (ms since the run began)
  const began = Date.now();
  const report = { backend: kind === "remote" ? url : kind, room, checks: [] };
  let panel;
  let projector;
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    context.on("page", (page) => {
      page.on("pageerror", (error) => errors.push(`${page.url()}: ${error.message}`));
      page.on("console", (message) => { if (message.type() === "error") errors.push(`${page.url()}: ${message.text()}`); });
      page.on("response", (response) => {
        if (response.status() >= 400) errors.push(`${response.status()} ${response.url()}`);
      });
      page.on("request", (request) => {
        if (!request.url().includes("/api/")) return;
        const entry = { page: page === panel ? "panel" : "projector", what: new URL(request.url()).pathname,
          action: request.url().endsWith("/api/action") ? JSON.parse(request.postData() || "{}").action : undefined,
          start: Date.now() - began };
        timeline.push(entry);
        request.response().then(() => { entry.end = Date.now() - began; }).catch(() => { entry.end = "failed"; });
      });
    });
    panel = await context.newPage();
    await panel.addInitScript(installFakeCamera, { camera: CAMERA, projector: PROJECTOR, gInverse: G_INVERSE, wall: WALL,
      papers: PAPERS, colors: COLORS, ambient: ROOMS[room].ambient, blackLevel: BLACK_LEVEL, gain: ROOMS[room].gain });
    await panel.goto(backend.url);
    const state = kind === "server"
      ? async () => (await context.request.get(`${backend.url}/api/state`)).json()
      : async () => panel.evaluate(() => window.postitBackend?.controller.snapshot() ?? null);
    await panel.waitForFunction(() => document.querySelector("video")?.videoWidth > 0, null, { timeout: 30000 });
    const roles = await until("the live detection sees every paper", async () => {
      const s = await state();
      const counts = Object.fromEntries(["start", "goal", "bumper", "marker"].map((r) => [r, s?.live_detection?.roles?.[r]?.count]));
      return counts.marker === 4 && counts.start === 1 && counts.goal === 1 && counts.bumper === 2 ? counts : null;
    }, 60000);
    report.checks.push(`camera sees ${JSON.stringify(roles)} before the projector opens`);

    const popup = panel.waitForEvent("popup");
    await panel.locator("#open-projector").click();
    projector = await popup;
    await projector.waitForLoadState();
    await projector.setViewportSize(PROJECTOR);
    await until("the backend knows the projector size", async () => {
      const s = await state();
      return s.projection.open && s.projection.width === PROJECTOR.width && s.projection.height === PROJECTOR.height;
    });
    await panel.locator("#theme-dark").click();
    await until("align button enabled", () => panel.locator("#align-projector").isEnabled(), 20000);
    const started = Date.now();
    await panel.locator("#align-projector").click();
    const aligned = await until("alignment and overlay check finish", async () => {
      const s = await state();
      const a = s?.alignment;
      if (a?.status === "failed") throw new Error(`alignment failed: ${a.detail}`);
      return a?.status === "ready" && a.overlay.status !== "unchecked" ? s : null;
    }, 90000, 250);
    report.alignSeconds = (Date.now() - started) / 1000;
    const { alignment } = aligned;
    report.alignment = { quality: alignment.quality, overlay: alignment.overlay.status, fill: alignment.overlay.fill,
      detail: alignment.detail, overlayDetail: alignment.overlay.detail };
    // The decoded H against the truth, at every paper corner, in projector px.
    const H = alignment.matrix;
    let worst = 0;
    for (const paper of PAPERS) {
      for (const [x, y] of paper.points) {
        const corner = [x - 0.5, y - 0.5];   // continuous -> pixel index
        const got = apply(H, corner);
        const want = TRUE_H(corner);
        worst = Math.max(worst, Math.hypot(got[0] - want[0], got[1] - want[1]));
      }
    }
    report.alignment.mappingErrorPx = worst;
    assert.ok(alignment.quality.decoded >= 50, `decoded ${alignment.quality.decoded} of ${alignment.quality.total}`);
    assert.ok(worst < 1.0, `decoded mapping ${worst.toFixed(3)} projector px from the truth at the papers`);
    report.checks.push(`aligned through the real panel in ${report.alignSeconds.toFixed(1)} s: ${alignment.quality.decoded}/` +
      `${alignment.quality.total} dots, holdout p95 ${alignment.quality.holdout_p95_px} px, truth error ${worst.toFixed(3)} px`);
    report.checks.push(`overlay check: ${alignment.overlay.status} (${alignment.overlay.detail})`);

    // The live loop: outlines on the papers, rims off them, no flips.
    await new Promise((done) => setTimeout(done, 2500));
    const samples = [];
    for (let k = 0; k < 10; k += 1) {
      const s = await state();
      // The ball (radius 10) and its aim line (70 px) in projector px, as capsules in the camera.
      const center = [s.game.position.x, s.game.position.y];
      const angle = (s.game.aim_degrees * Math.PI) / 180;
      const tip = [center[0] + 70 * Math.cos(angle), center[1] + 70 * Math.sin(angle)];
      const exclude = [{ a: apply(G, center), b: apply(G, tip), r: 12 }];
      samples.push({ ready: s.live_detection?.quality?.ready,
        counts: Object.fromEntries(Object.entries(s.live_detection?.roles ?? {}).map(([r, v]) => [r, v.count])),
        notes: s.live_detection?.roles, space: s.game.space,
        rims: await panel.evaluate((capsules) => window.closedLoopCamera.rims(capsules), exclude) });
      await new Promise((done) => setTimeout(done, 300));
    }
    const flips = samples.filter((sample, k) => k && sample.ready !== samples[k - 1].ready).length;
    assert.equal(flips, 0, "detection never flips while the projector lights the wall");
    assert.ok(samples.every((sample) => sample.ready), "the board stays playable");
    assert.ok(samples.every((sample) => JSON.stringify(sample.counts) === JSON.stringify(roles)), "every paper, every sample");
    let cornerWorst = 0;
    for (const sample of samples) {
      for (const paper of PAPERS) {
        const truth = paper.points.map(([x, y]) => [x - 0.5, y - 0.5]);
        const nearest = sample.notes[paper.role].notes.map((note) => note.points.map((p) => [p.x, p.y]))
          .map((poly) => ({ poly, error: Math.max(...truth.map(([tx, ty]) => Math.min(...poly.map(([x, y]) => Math.hypot(x - tx, y - ty))))) }))
          .sort((a, b) => a.error - b.error)[0];
        cornerWorst = Math.max(cornerWorst, nearest.error);
      }
    }
    report.cornerErrorCameraPx = cornerWorst;
    assert.ok(cornerWorst < 1.5, `live outline corners ${cornerWorst.toFixed(2)} camera px from the papers`);
    const rims = samples.flatMap((sample) => sample.rims);
    report.rims = { minClearanceCameraPx: Math.min(...rims.map((r) => r.clearance)), onPaper: rims.reduce((sum, r) => sum + r.onPaper, 0),
      minRimPixels: Math.min(...rims.map((r) => r.rimPixels)) };
    assert.ok(report.rims.minRimPixels > 40, `every paper has its rim (${report.rims.minRimPixels} px)`);
    assert.equal(report.rims.onPaper, 0, "no rim light on any paper");
    assert.ok(report.rims.minClearanceCameraPx >= 1.0, `rims clear the papers by ${report.rims.minClearanceCameraPx.toFixed(2)} camera px`);
    report.checks.push(`live loop: 0 flips, outline corners within ${cornerWorst.toFixed(2)} camera px, rims ` +
      `${report.rims.minClearanceCameraPx.toFixed(2)} camera px off every paper`);
    // What lights the papers: their role color only when the check allowed it.
    const fills = await panel.evaluate(() => window.closedLoopCamera.fills());
    const spreads = fills.map(({ light }) => Math.max(...light) - Math.min(...light));
    report.fills = fills.map((fill, k) => ({ role: fill.role, light: fill.light.map((v) => +v.toFixed(3)), spread: +spreads[k].toFixed(3) }));
    if (alignment.overlay.fill) {
      assert.ok(spreads.every((spread) => spread > 0.08), `every paper carries its role color (${JSON.stringify(report.fills)})`);
      report.checks.push(`fill on: every paper lit in its role color (min spread ${Math.min(...spreads).toFixed(2)})`);
    } else {
      assert.ok(spreads.every((spread) => spread < 0.02), `fill off: neutral light only (${JSON.stringify(report.fills)})`);
      assert.ok(fills.every(({ light }) => light[0] > 0.2), "the dark theme still lights the papers (neutral fill)");
      report.checks.push("fill off: papers lit only by neutral white");
    }

    // An attempt in projector px.
    assert.equal(samples.at(-1).space, "projector");
    await until("launch enabled", () => panel.locator("#launch-button").isEnabled());
    await panel.locator("#launch-button").click();
    const finished = await until("the attempt finishes", async () => {
      const s = await state();
      return ["won", "lost"].includes(s?.game?.status) ? s : null;
    }, 30000);
    report.attempt = { status: finished.game.status, space: finished.game.space, size: [finished.game.width, finished.game.height] };
    assert.equal(finished.game.space, "projector");
    assert.deepEqual(report.attempt.size, [PROJECTOR.width, PROJECTOR.height]);
    report.checks.push(`attempt ${finished.game.status} in projector px ${finished.game.width}x${finished.game.height}`);

    const png = await panel.evaluate(() => window.closedLoopCamera.png());
    writeFileSync(join(output, `${name}-camera.png`), Buffer.from(png.split(",")[1], "base64"));
    await projector.screenshot({ path: join(output, `${name}-projector.png`) });
    await panel.screenshot({ path: join(output, `${name}-panel.png`), fullPage: true });
    const unexpected = errors.filter((message) => !/favicon/.test(message));
    assert.deepEqual(unexpected, [], "no browser errors");
    report.passed = true;
    return report;
  } catch (error) {
    report.passed = false;
    report.failure = error.stack;
    report.errors = errors;
    report.visibility = await Promise.all([panel, projector].map((page) => page?.evaluate(() =>
      ({ visibility: document.visibilityState, focus: document.hasFocus() })).catch((e) => String(e))));
    report.timeline = timeline.filter((entry) => entry.end === undefined || entry.action || entry.what !== "/api/tick").slice(-60);
    await panel?.screenshot({ path: join(output, `${name}-panel-failure.png`), fullPage: true }).catch(() => {});
    await projector?.screenshot({ path: join(output, `${name}-projector-failure.png`) }).catch(() => {});
    const png = await panel?.evaluate(() => window.closedLoopCamera?.png()).catch(() => null);
    if (png) writeFileSync(join(output, `${name}-camera-failure.png`), Buffer.from(png.split(",")[1], "base64"));
    return report;
  } finally {
    await browser.close();
    backend.stop();
    if (backend.log()) writeFileSync(join(output, `${name}-server.log`), backend.log());
  }
}

const reports = [];
for (const entry of runs) reports.push(await run(entry));
writeFileSync(join(output, "report.json"), JSON.stringify(reports, null, 2));
console.log(JSON.stringify(reports.map(({ backend, room, passed, checks, failure }) => ({ backend, room, passed, checks,
  failure: failure?.split("\n")[0] })), null, 2));
if (!reports.every((report) => report.passed)) process.exitCode = 1;
