/**
 * Local browser acceptance test. Uses a synthetic camera and isolated data.
 *
 * The server here has no alignment, so the projector draws the unaligned
 * scene (docs/alignment-and-color.md 2.1): neutral rims 5 px outside each
 * note through the contain transform, no fills, no role hue anywhere, and
 * the "Sin alinear" hint bottom-left.
 */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { containTransform, offsetConvexPolygon } from "../services/web/ui/ui-core.mjs";
import { NEUTRAL, RIM_OFFSET_PX } from "../services/web/ui/live-view.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const output = process.env.POSTIT_BROWSER_OUTPUT || "/tmp/postit-live-browser";
mkdirSync(output, { recursive: true });
const python = process.env.POSTIT_PYTHON || (existsSync(join(root, ".venv/bin/python")) ? join(root, ".venv/bin/python") : "python3");
const playwright = process.env.POSTIT_PLAYWRIGHT_MODULE
  ? await import(pathToFileURL(resolve(process.env.POSTIT_PLAYWRIGHT_MODULE)).href)
  : await import("playwright");
const fixture = spawnSync(python, ["-c", `
from pathlib import Path
import cv2
import numpy as np
from services.runtime.tests.fixtures import camera_frame
out = Path(${JSON.stringify(output)})
base = camera_frame()
moved = camera_frame(green_center=(540, 250))
no_markers = base.copy()
for x, y in [(80,75), (720,85), (710,520), (90,510)]:
    cv2.rectangle(no_markers, (x-35,y-35), (x+35,y+35), (220,220,220), -1)
partial = base.copy()
cv2.rectangle(partial, (610,270), (690,350), (220,220,220), -1)
for name, image in [('base',base), ('moved',moved), ('no-markers',no_markers), ('partial',partial)]:
    okay, jpeg = cv2.imencode('.jpg', image)
    assert okay
    (out / (name + '.jpg')).write_bytes(jpeg.tobytes())
yuv = cv2.cvtColor(base, cv2.COLOR_BGR2YUV_I420).tobytes()
with (out / 'camera.y4m').open('wb') as stream:
    stream.write(b'YUV4MPEG2 W800 H600 F30:1 Ip A1:1 C420jpeg\\n')
    for _ in range(2):
        stream.write(b'FRAME\\n' + yuv)
`], { cwd: root, encoding: "utf8" });
assert.equal(fixture.status, 0, fixture.stderr);

let serverOutput = "";
const server = spawn(python, ["-u", "-c", `
from pathlib import Path
from services.web.backend.server import GameController, create_server
controller = GameController(log_path=Path(${JSON.stringify(join(output, "events.jsonl"))}))
server = create_server(port=0, controller=controller)
print(server.server_address[1], flush=True)
server.serve_forever(poll_interval=0.1)
`], { cwd: root, stdio: ["ignore", "pipe", "pipe"] });
server.stdout.on("data", (chunk) => { serverOutput += chunk; });
server.stderr.on("data", (chunk) => { serverOutput += chunk; });
const failures = [];
const actions = [];
const checks = [];
const rasters = [];

/** A keystoned camera (800x600) -> projector (1280x800) mapping that keeps the frame on screen. */
const SKEW = [[1.45, 0.05, 60], [-0.02, 1.25, 40], [-4e-5, 5e-5, 1]];

/** Contract 1.1 layout, written out here so the test does not trust any backend's copy. */
function alignmentLayout(width, height) {
  const pyRound = (value) => {
    const floor = Math.floor(value);
    const rest = value - floor;
    return rest !== 0.5 ? (rest < 0.5 ? floor : floor + 1) : (floor % 2 === 0 ? floor : floor + 1);
  };
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const cols = clamp(pyRound(Math.sqrt(60 * width / height)), 4, 12);
  let rows = clamp(pyRound(60 / cols), 3, 9);
  while (cols * rows > 63) rows -= 1;
  const dots = [];
  for (let j = 0; j < rows; j++) {
    for (let i = 0; i < cols; i++) {
      dots.push({ id: j * cols + i + 1, x: width * (0.06 + 0.88 * i / (cols - 1)), y: height * (0.06 + 0.88 * j / (rows - 1)) });
    }
  }
  return { version: 1, width, height, cols, rows, radius: Math.max(4, 0.012 * Math.min(width, height)), bits: 7, dots,
    frames: Array.from({ length: 14 }, (_, k) => ({ bit: k >> 1, inverse: Boolean(k & 1) })) };
}

/**
 * Runs in the projector page: the aligned scene with its fill allowed, drawn
 * by Chrome on a detached canvas, then every lit pixel classified by its
 * distance to the nearest mapped paper (max over the paper's edge lines,
 * positive outward). Fill: 5 px inside; rim: 3 px stroke centered 5 px out.
 * Pixel centers within 0.71 px of a boundary can be partly covered, so the
 * bands keep that margin: inside <= -4, gap (-4, 2.5), rim [2.5, 7.5].
 */
async function alignedRaster({ H, preview, palette, theme, width, height }) {
  const live = await import("/live-view.mjs");
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const background = theme === "light" ? [243, 242, 236] : [0, 0, 0];
  ctx.fillStyle = `rgb(${background.join(",")})`;
  ctx.fillRect(0, 0, width, height);
  const state = { palette, alignment: { status: "ready", matrix: H, projector: { width, height },
    camera: { width: preview.width, height: preview.height }, overlay: { status: "ok", fill: true } } };
  live.drawProjectorScene(ctx, { state, preview, palette, theme, width, height });
  const map = ({ x, y }) => {
    const w = H[2][0] * x + H[2][1] * y + H[2][2];
    return { x: (H[0][0] * x + H[0][1] * y + H[0][2]) / w, y: (H[1][0] * x + H[1][1] * y + H[1][2]) / w };
  };
  const papers = Object.values(preview.roles).flatMap((role) => (role.notes ?? []).map((note) => note.points.map(map)));
  const signedArea = (poly) => poly.reduce((sum, p, i) => sum + p.x * poly[(i + 1) % poly.length].y - poly[(i + 1) % poly.length].x * p.y, 0) / 2;
  const edgeDistance = (poly, x, y) => {
    const sign = Math.sign(signedArea(poly));
    return Math.max(...poly.map((a, i) => {
      const b = poly[(i + 1) % poly.length];
      return sign * ((b.y - a.y) * (x - a.x) - (b.x - a.x) * (y - a.y)) / Math.hypot(b.x - a.x, b.y - a.y);
    }));
  };
  const { data } = ctx.getImageData(0, 0, width, height);
  const pixel = (x, y) => {
    const k = (Math.floor(y) * width + Math.floor(x)) * 4;
    return [data[k], data[k + 1], data[k + 2]];
  };
  const spread = ([r, g, b]) => Math.max(r, g, b) - Math.min(r, g, b);
  // Neutral ink, the light background and their blends spread at most 7; the
  // palest default fill (bumper #6cc678 at .35 on black) spreads 31.
  const HUE_SPREAD = 12;
  const stats = { notes: papers.length, fills: 0, edges: 0, rimMidpoints: 0, huedInside: 0, huedOutside: 0, litInGap: 0, litOutside: 0, lit: 0 };
  for (let py = 0; py < height; py++) {
    for (let px = 0; px < width; px++) {
      const k = (py * width + px) * 4;
      const color = [data[k], data[k + 1], data[k + 2]];
      if (color.every((value, c) => Math.abs(value - background[c]) <= 2)) continue;
      stats.lit += 1;
      const d = Math.min(...papers.map((poly) => edgeDistance(poly, px + 0.5, py + 0.5)));
      const hued = spread(color) > HUE_SPREAD;
      if (d <= -4) stats.huedInside += hued ? 1 : 0;
      else if (hued) stats.huedOutside += 1;
      if (d > -4 && d < 2.5) stats.litInGap += 1;
      if (d > 7.5) stats.litOutside += 1;
    }
  }
  const rim = theme === "light" ? [0x2b, 0x2f, 0x2e] : [255, 255, 255];
  for (const poly of papers) {
    const center = poly.reduce((sum, p) => ({ x: sum.x + p.x / poly.length, y: sum.y + p.y / poly.length }), { x: 0, y: 0 });
    if (spread(pixel(center.x, center.y)) > HUE_SPREAD) stats.fills += 1;
    const sign = Math.sign(signedArea(poly));
    poly.forEach((a, i) => {
      const b = poly[(i + 1) % poly.length];
      const length = Math.hypot(b.x - a.x, b.y - a.y);
      const out = { x: sign * (b.y - a.y) / length, y: -sign * (b.x - a.x) / length };
      const at = pixel((a.x + b.x) / 2 + 5 * out.x, (a.y + b.y) / 2 + 5 * out.y);
      stats.edges += 1;
      if (at.every((value, c) => Math.abs(value - rim[c]) <= 10)) stats.rimMidpoints += 1;
    });
  }
  stats.png = canvas.toDataURL("image/png");
  return stats;
}

/** Runs in the projector page: the warm-up and all 14 frames on a canvas left light by the theme. */
async function patternRaster({ layout }) {
  const live = await import("/live-view.mjs");
  const { width, height } = layout;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d");
  const draw = (frame) => {
    ctx.fillStyle = "#f3f2ec";
    ctx.fillRect(0, 0, width, height);
    const count = live.drawAlignmentPattern(ctx, layout, frame, width, height);
    return { count, data: ctx.getImageData(0, 0, width, height).data };
  };
  const red = (data, x, y) => data[(Math.floor(y) * width + Math.floor(x)) * 4];
  const warm = draw("warmup").data;
  const stats = { frames: 0, dots: layout.dots.length, radius: layout.radius, wrongCenters: 0, litBetween: 0, colored: 0,
    worstAreaError: 0, warmupMatchesFirst: false };
  for (const frame of layout.frames) {
    const { count, data } = draw(frame);
    if (frame.bit === 0 && !frame.inverse) stats.warmupMatchesFirst = warm.every((value, i) => value === data[i]);
    let light = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (data[i] !== data[i + 1] || data[i] !== data[i + 2]) stats.colored += 1;
      light += data[i] / 255;
    }
    for (const dot of layout.dots) {
      const lit = ((live.alignmentCode(dot.id) >> frame.bit) & 1) === (frame.inverse ? 0 : 1);
      if (red(data, dot.x, dot.y) !== (lit ? 255 : 0)) stats.wrongCenters += 1;
      const right = layout.dots.find((other) => other.id === dot.id + 1 && other.y === dot.y);
      if (right && red(data, (dot.x + right.x) / 2, dot.y) !== 0) stats.litBetween += 1;
    }
    const expected = count * Math.PI * layout.radius ** 2;
    stats.worstAreaError = Math.max(stats.worstAreaError, expected ? Math.abs(light - expected) / expected : light);
    stats.frames += 1;
  }
  return stats;
}

/** Projector canvas pixels that are not exactly `rgb`. */
const offColorPixels = (page, rgb) => page.evaluate((rgb) => {
  const canvas = document.getElementById("projector-canvas");
  const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
  let count = 0;
  for (let i = 0; i < data.length; i += 4) if (data[i] !== rgb[0] || data[i + 1] !== rgb[1] || data[i + 2] !== rgb[2]) count += 1;
  return count;
}, rgb);
let browser;
let panel;
let projector;

async function until(description, predicate, timeout = 15000) {
  const end = Date.now() + timeout;
  while (Date.now() < end) {
    const value = await predicate();
    if (value) return value;
    await new Promise((done) => setTimeout(done, 100));
  }
  throw new Error(`Timed out: ${description}`);
}

try {
  const port = await until("local server starts", () => {
    if (server.exitCode !== null) throw new Error(`Local server exited: ${serverOutput}`);
    return serverOutput.match(/^(\d+)\s*$/m)?.[1];
  });
  const url = `http://127.0.0.1:${port}`;
  const chrome = process.env.POSTIT_BROWSER_EXECUTABLE || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
  browser = await playwright.chromium.launch({ headless: true,
    ...(existsSync(chrome) ? { executablePath: chrome } : {}),
    args: ["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
      `--use-file-for-fake-video-capture=${join(output, "camera.y4m")}`],
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, permissions: ["camera"], deviceScaleFactor: 1 });
  context.on("page", (page) => {
    page.on("pageerror", (error) => failures.push(error.message));
    page.on("console", (message) => { if (message.type() === "error") failures.push(message.text()); });
    page.on("request", (request) => {
      if (request.url().endsWith("/api/action") && request.postData()) actions.push(JSON.parse(request.postData()).action);
    });
  });
  panel = await context.newPage();
  let currentFrame = "base";
  await panel.route("**/api/preview", async (route) => {
    if (currentFrame === "stopped") return route.abort("failed");
    await route.continue({ postData: readFileSync(join(output, `${currentFrame}.jpg`)) });
  });
  await panel.goto(url);
  await panel.waitForFunction(() => document.querySelector("video")?.videoWidth > 0);
  const state = async () => (await context.request.get(`${url}/api/state`)).json();
  await until("real detector sees all four roles", async () => {
    const s = await state();
    return ["start", "goal", "bumper", "marker"].every((role) => s.live_detection?.roles?.[role]?.count > 0);
  });
  assert.equal(await panel.getByRole("button", { name: /calibr/i }).count(), 0);
  const popup = panel.waitForEvent("popup");
  await panel.locator("#open-projector").click();
  projector = await popup;
  await projector.waitForLoadState();
  async function resizeProjector(viewport) {
    await projector.setViewportSize(viewport);
    await projector.waitForFunction(({ width, height }) => {
      const canvas = document.getElementById("projector-canvas");
      return window.innerWidth === width && window.innerHeight === height
        && canvas.clientWidth === width && canvas.clientHeight === height
        && canvas.width === Math.round(width * devicePixelRatio)
        && canvas.height === Math.round(height * devicePixelRatio);
    }, viewport);
    await until("backend projector viewport synchronized", async () => {
      const s = await state();
      return s.projection.width === viewport.width && s.projection.height === viewport.height;
    });
  }
  await resizeProjector({ width: 1280, height: 720 });
  await until("detected board prepares ball without calibration or scanning", () => panel.locator("#launch-button").isEnabled());
  assert.equal(await projector.locator("video").count(), 0);
  assert.equal(await projector.locator("[id*='calibrat']").count(), 0);
  assert.ok(!actions.some((action) => action.startsWith("calibration")));
  assert.ok(!actions.includes("scan"), "live data alone prepares a playable ball");
  checks.push("auto-ready, no calibration controls or actions, no projected camera video");

  /** Canvas pixels under projector CSS points (the canvas applies devicePixelRatio itself). */
  async function pixelsAt(page, points) {
    return page.evaluate((points) => {
      const canvas = document.getElementById("projector-canvas");
      const ratio = canvas.width / canvas.clientWidth;
      const ctx = canvas.getContext("2d");
      return points.map(({ x, y }) => [...ctx.getImageData(Math.floor(x * ratio), Math.floor(y * ratio), 1, 1).data]);
    }, points);
  }
  const viewportOf = (page) => page.evaluate(() => {
    const canvas = document.getElementById("projector-canvas");
    return { width: canvas.clientWidth, height: canvas.clientHeight };
  });
  /** Camera px -> projector CSS px through the unaligned contain transform. */
  async function toProjector(page, points, frame = { width: 800, height: 600 }) {
    const { width, height } = await viewportOf(page);
    const fit = containTransform(width, height, frame.width, frame.height);
    return points.map((p) => ({ x: fit.offsetX + p.x * fit.scale, y: fit.offsetY + p.y * fit.scale }));
  }
  /** Midpoints of the rim's edges: 5 px outside the contained note, on the 3 px stroke's center line. */
  async function rimSamples(page, note) {
    const rim = offsetConvexPolygon(await toProjector(page, note.points), RIM_OFFSET_PX);
    assert.ok(rim, "a detected note always has a rim");
    return rim.map((a, i) => {
      const b = rim[(i + 1) % rim.length];
      return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
    });
  }
  const centerOf = (note) => ({
    x: note.points.reduce((sum, p) => sum + p.x, 0) / note.points.length,
    y: note.points.reduce((sum, p) => sum + p.y, 0) / note.points.length,
  });
  const isColor = (pixel, hex, tolerance = 8) => [1, 3, 5].every((at, channel) =>
    Math.abs(pixel[channel] - parseInt(hex.slice(at, at + 2), 16)) <= tolerance);
  const rimDrawn = async (page, note, hex) => (await pixelsAt(page, await rimSamples(page, note))).every((pixel) => isColor(pixel, hex));
  const rimGone = async (page, note, hex) => (await pixelsAt(page, await rimSamples(page, note))).every((pixel) => isColor(pixel, hex, 0));
  /** Pixels whose channels spread more than a neutral gray, anti-aliasing included, can: a role hue. */
  const huedPixels = (page) => page.evaluate(() => {
    const canvas = document.getElementById("projector-canvas");
    const { data } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
    let count = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (Math.max(data[i], data[i + 1], data[i + 2]) - Math.min(data[i], data[i + 1], data[i + 2]) > 40) count += 1;
    }
    return count;
  });
  /** Rim-colored pixels in the bottom-left band where the "Sin alinear" hint sits. */
  const hintPixels = (page, hex) => page.evaluate(({ hex }) => {
    const canvas = document.getElementById("projector-canvas");
    const ratio = canvas.width / canvas.clientWidth;
    const top = Math.floor((canvas.clientHeight - 16 - 20) * ratio);
    const { data } = canvas.getContext("2d").getImageData(Math.floor(16 * ratio), top, Math.floor(240 * ratio), Math.floor(20 * ratio));
    const want = [1, 3, 5].map((at) => parseInt(hex.slice(at, at + 2), 16));
    let count = 0;
    for (let i = 0; i < data.length; i += 4) if (want.every((value, c) => Math.abs(data[i + c] - value) <= 24)) count += 1;
    return count;
  }, { hex });
  const background = (page) => page.evaluate(() => [...document.getElementById("projector-canvas").getContext("2d").getImageData(2, 2, 1, 1).data]);
  const liveNote = async (role) => (await state()).live_detection?.roles?.[role]?.notes?.[0] ?? null;
  await panel.locator("#theme-dark").click();
  await until("dark theme", async () => (await background(projector)).slice(0, 3).every((n) => n === 0));
  const baseBumper = await until("bumper detected", () => liveNote("bumper"));
  for (const viewport of [{ width: 1280, height: 720 }, { width: 600, height: 1000 }]) {
    await resizeProjector(viewport);
    await until("neutral white rim 5 px outside the contained bumper", () => rimDrawn(projector, baseBumper, NEUTRAL.dark.rim));
    const [inside] = await pixelsAt(projector, await toProjector(projector, [centerOf(baseBumper)]));
    assert.deepEqual(inside.slice(0, 3), [0, 0, 0], "no fill on the paper without alignment");
    assert.equal(await huedPixels(projector), 0, "no role hue anywhere on the projector");
    assert.ok(await hintPixels(projector, NEUTRAL.dark.rim) > 20, "unaligned hint drawn bottom-left in the rim color");
    const ball = (await state()).game.position;
    const [ballPixel] = await pixelsAt(projector, await toProjector(projector, [ball]));
    assert.ok(isColor(ballPixel, NEUTRAL.dark.ball), "neutral ball stays in camera coordinates exactly once");
    await projector.screenshot({ path: join(output, `projector-dark-${viewport.width}x${viewport.height}.png`) });
  }
  checks.push("unaligned: neutral rims through landscape and portrait contain transforms, no fill, no hue, hint, one ball");
  await resizeProjector({ width: 1280, height: 720 });
  await panel.locator("#theme-light").click();
  await until("light theme", async () => (await background(projector)).slice(0, 3).join() === "243,242,236");
  const marker = await liveNote("marker");
  await until("light projector draws dark neutral rims", () => rimDrawn(projector, marker, NEUTRAL.light.rim));
  const markerEdge = marker.points;
  const edgeMidpoint = { x: (markerEdge[0].x + markerEdge[1].x) / 2, y: (markerEdge[0].y + markerEdge[1].y) / 2 };
  const [paperEdge] = await pixelsAt(projector, await toProjector(projector, [edgeMidpoint]));
  assert.equal(paperEdge.slice(0, 3).join(), "243,242,236", "the rim sits outside the paper, its edge stays unlit");
  assert.equal(await huedPixels(projector), 0, "no role hue in the light theme either");
  assert.ok(await hintPixels(projector, NEUTRAL.light.rim) > 20, "light hint in the dark rim color");
  await projector.screenshot({ path: join(output, "projector-light.png") });
  await projector.keyboard.press("f");
  await until("projector F changes both views back to dark", async () =>
    (await panel.locator("#theme-dark").getAttribute("aria-pressed")) === "true");
  await panel.screenshot({ path: join(output, "panel.png"), fullPage: true });
  checks.push("dark/light background selector and F synchronization");

  // The backend here has no alignment yet, so the aligned renderer and the
  // patterns are rasterized by Chrome's own canvas in the projector page,
  // from the same module the projector draws with. A recording mock cannot
  // see anti-aliasing; these pixels can.
  const detection = (await state()).live_detection;
  const palette = (await state()).palette;
  for (const theme of ["dark", "light"]) {
    const raster = await projector.evaluate(alignedRaster, {
      H: SKEW, preview: detection, palette, theme, width: 1280, height: 800,
    });
    assert.ok(raster.notes >= 4 && raster.fills === raster.notes, `${theme}: every real note filled once checked (${JSON.stringify(raster)})`);
    assert.equal(raster.huedOutside, 0, `${theme}: no role hue outside the paper's 5 px inset`);
    assert.equal(raster.litInGap, 0, `${theme}: nothing lit between the fill and the rim`);
    assert.equal(raster.litOutside, 0, `${theme}: nothing lit beyond the rims`);
    assert.ok(raster.rimMidpoints === raster.edges, `${theme}: rim color 5 px outside every edge midpoint`);
    assert.ok(raster.huedInside > 100, `${theme}: the fill is visible`);
    writeFileSync(join(output, `aligned-raster-${theme}.png`), Buffer.from(raster.png.split(",")[1], "base64"));
    delete raster.png;
    raster.theme = theme;
    rasters.push(raster);
  }
  checks.push("aligned rims and fills rasterized by Chrome: no hue outside the paper, a dark gap at every paper edge");
  const pattern = await projector.evaluate(patternRaster, { layout: alignmentLayout(1280, 720) });
  assert.equal(pattern.wrongCenters, 0, `pattern dot centers lit exactly as coded (${JSON.stringify(pattern)})`);
  assert.equal(pattern.litBetween, 0, "black between dots");
  assert.ok(pattern.worstAreaError < 0.03, `dot areas within 3% of pi r^2 (${pattern.worstAreaError})`);
  assert.equal(pattern.warmupMatchesFirst, true);
  assert.equal(pattern.colored, 0, "the pattern has no color, only white on black");
  rasters.push(pattern);
  checks.push("all 14 coded pattern frames and the warm-up rasterized as white dots on pure black");

  // projection.mode from the backend: neutral is the theme background alone,
  // align is pure black until a pattern arrives (2.2, 3). Injected into the
  // projector's own tick replies, since this backend has neither mode yet.
  let injectedMode = null;
  const injectMode = async (route) => {
    const reply = await route.fetch();
    const json = await reply.json();
    if (injectedMode) json.projection = { ...json.projection, mode: injectedMode };
    await route.fulfill({ response: reply, json });
  };
  await projector.route("**/api/tick", injectMode);
  try {
    for (const theme of ["light", "dark"]) {
      await panel.locator(`#theme-${theme}`).click();
      const backdrop = theme === "light" ? [243, 242, 236] : [0, 0, 0];
      for (const [mode, color] of [["neutral", backdrop], ["align", [0, 0, 0]]]) {
        injectedMode = mode;
        await until(`${theme} ${mode}: one flat color on the whole projector`, async () =>
          (await projector.evaluate(() => document.body.dataset.mode)) === mode
          && (await offColorPixels(projector, color)) === 0);
        await projector.screenshot({ path: join(output, `projector-${mode}-${theme}.png`) });
      }
      injectedMode = null;
      await until(`${theme} live drawing returns`, async () => (await projector.evaluate(() => document.body.dataset.mode)) === "live"
        && (await offColorPixels(projector, backdrop)) > 0);
    }
  } finally {
    injectedMode = null;
    await projector.unroute("**/api/tick", injectMode);
  }
  checks.push("neutral mode projects only the theme background and align mode pure black, in both themes");

  currentFrame = "moved";
  const movedBumper = await until("detection follows the moved bumper", async () => {
    const note = await liveNote("bumper");
    return note && Math.hypot(centerOf(note).x - 540, centerOf(note).y - 250) < 10 ? note : null;
  });
  await until("live rim moves on projector and the old one clears", async () =>
    await rimDrawn(projector, movedBumper, NEUTRAL.dark.rim) && await rimGone(projector, baseBumper, "#000000"));
  await projector.screenshot({ path: join(output, "projector-moved.png") });

  // Keep the camera publishing while the projector's request never resolves.
  // The projector must expire its own copy instead of waiting for a response.
  let releaseTick;
  let tickHeld = false;
  const holdTick = new Promise((resolve) => { releaseTick = resolve; });
  const blockTick = async (route) => {
    tickHeld = true;
    await holdTick;
    await route.continue().catch(() => {});
  };
  await projector.route("**/api/tick", blockTick);
  await until("projector tick request held", () => tickHeld);
  const stallStarted = performance.now();
  try {
    await until("local detection expires while request remains pending", () =>
      rimGone(projector, movedBumper, "#000000"), 1800);
    assert.ok(performance.now() - stallStarted < 1800, "1.5-second local TTL plus rendering allowance");
    assert.ok((await state()).live_detection, "server still has fresh data from the active camera");
    await projector.screenshot({ path: join(output, "projector-stalled.png") });
    checks.push("projector clears locally within TTL even while API request never resolves");
  } finally {
    releaseTick();
    await projector.unroute("**/api/tick", blockTick);
  }
  await until("live rim recovers when tick resumes", () => rimDrawn(projector, movedBumper, NEUTRAL.dark.rim));

  currentFrame = "no-markers";
  await until("corners are optional", async () => {
    const s = await state();
    return s.live_detection?.roles.marker.count === 0 && s.game?.status === "ready";
  });
  await panel.locator("#launch-button").click();
  await until("ball launches", async () => (await state()).game?.status === "running");
  checks.push("moving live contours clear old position, playable without corner markers");

  currentFrame = "partial";
  await until("partial detection continues while playing", async () => {
    const s = await state();
    return s.live_detection?.roles.goal.count === 0 && s.live_detection.roles.bumper.count === 1;
  });
  const partialBumper = await liveNote("bumper");
  await until("partial board is still drawn", () => rimDrawn(projector, partialBumper, NEUTRAL.dark.rim));
  await projector.screenshot({ path: join(output, "projector-partial.png") });
  currentFrame = "stopped";
  await until("server expires stale detection", async () => (await state()).live_detection === null, 5000);
  await until("projector removes stale outline", () => rimGone(projector, partialBumper, "#000000"), 5000);
  await projector.screenshot({ path: join(output, "projector-expired.png") });
  checks.push("partial board remains visible during attempt and stale detection expires");
  // Aborted preview requests deliberately produce browser network errors. Any
  // error before this fault-injection phase is a real regression.
  const unexpected = failures.filter((message) => !message.includes("net::ERR_FAILED"));
  assert.deepEqual(unexpected, [], "no browser runtime errors");
  console.log(JSON.stringify({ passed: true, checks, rasters, screenshots: output }, null, 2));
  writeFileSync(join(output, "report.json"), JSON.stringify({ passed: true, checks, rasters, actions, consoleErrors: unexpected }, null, 2));
} catch (error) {
  await panel?.screenshot({ path: join(output, "panel-failure.png"), fullPage: true }).catch(() => {});
  await projector?.screenshot({ path: join(output, "projector-failure.png") }).catch(() => {});
  writeFileSync(join(output, "report.json"), JSON.stringify({ passed: false, checks, rasters, actions, errors: failures, failure: error.stack }, null, 2));
  throw error;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  writeFileSync(join(output, "server.log"), serverOutput);
}
