/** Local browser acceptance test. Uses a synthetic camera and isolated data. */
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

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

  async function pixelAtCamera(page, point, frame = { width: 800, height: 600 }) {
    return page.evaluate(({ point, frame }) => {
      const canvas = document.getElementById("projector-canvas");
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      const scale = Math.min(width / frame.width, height / frame.height);
      const x = Math.round(((width - frame.width * scale) / 2 + point.x * scale) * canvas.width / width);
      const y = Math.round(((height - frame.height * scale) / 2 + point.y * scale) * canvas.height / height);
      return [...canvas.getContext("2d").getImageData(x, y, 1, 1).data];
    }, { point, frame });
  }
  const background = (page) => page.evaluate(() => [...document.getElementById("projector-canvas").getContext("2d").getImageData(2, 2, 1, 1).data]);
  await panel.locator("#theme-dark").click();
  await until("dark theme", async () => (await background(projector)).slice(0, 3).every((n) => n === 0));
  for (const viewport of [{ width: 1280, height: 720 }, { width: 600, height: 1000 }]) {
    await resizeProjector(viewport);
    await until("live green halo uses contained camera coordinates", async () => {
      const [r, g, b] = await pixelAtCamera(projector, { x: 405, y: 190 });
      return g > r + 10 && g > b + 10;
    });
    const ball = (await state()).game.position;
    const ballPixel = await pixelAtCamera(projector, ball);
    assert.ok(ballPixel[0] > 200 && ballPixel[1] > 200 && ballPixel[2] > 200, "ball stays in camera coordinates exactly once");
    await projector.screenshot({ path: join(output, `projector-dark-${viewport.width}x${viewport.height}.png`) });
  }
  checks.push("landscape and portrait contain transforms, one correctly positioned ball");
  await resizeProjector({ width: 1280, height: 720 });
  await panel.locator("#theme-light").click();
  await until("light theme", async () => (await background(projector)).slice(0, 3).join() === "243,242,236");
  const markerEdge = (await state()).live_detection.roles.marker.notes[0].points;
  const edgeMidpoint = { x: (markerEdge[0].x + markerEdge[1].x) / 2, y: (markerEdge[0].y + markerEdge[1].y) / 2 };
  const lightRim = await pixelAtCamera(projector, edgeMidpoint);
  assert.ok(lightRim.slice(0, 3).every((n) => n < 180), "light projector passes theme to the contrasting dark rim");
  await projector.screenshot({ path: join(output, "projector-light.png") });
  await projector.keyboard.press("f");
  await until("projector F changes both views back to dark", async () =>
    (await panel.locator("#theme-dark").getAttribute("aria-pressed")) === "true");
  await panel.screenshot({ path: join(output, "panel.png"), fullPage: true });
  checks.push("dark/light background selector and F synchronization");

  currentFrame = "moved";
  await until("live detection moves on projector", async () => {
    const [r, g, b] = await pixelAtCamera(projector, { x: 540, y: 250 });
    const old = await pixelAtCamera(projector, { x: 405, y: 190 });
    return g > r + 10 && g > b + 10 && old.slice(0, 3).every((n) => n < 5);
  });
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
    await until("local detection expires while request remains pending", async () =>
      (await pixelAtCamera(projector, { x: 540, y: 250 })).slice(0, 3).every((n) => n < 5), 1800);
    assert.ok(performance.now() - stallStarted < 1800, "1.5-second local TTL plus rendering allowance");
    assert.ok((await state()).live_detection, "server still has fresh data from the active camera");
    await projector.screenshot({ path: join(output, "projector-stalled.png") });
    checks.push("projector clears locally within TTL even while API request never resolves");
  } finally {
    releaseTick();
    await projector.unroute("**/api/tick", blockTick);
  }
  await until("live overlay recovers when tick resumes", async () => {
    const [r, g, b] = await pixelAtCamera(projector, { x: 540, y: 250 });
    return g > r + 10 && g > b + 10;
  });

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
  await projector.screenshot({ path: join(output, "projector-partial.png") });
  currentFrame = "stopped";
  await until("server expires stale detection", async () => (await state()).live_detection === null, 5000);
  await until("projector removes stale outline", async () =>
    (await pixelAtCamera(projector, { x: 405, y: 190 })).slice(0, 3).every((n) => n < 5), 5000);
  await projector.screenshot({ path: join(output, "projector-expired.png") });
  checks.push("partial board remains visible during attempt and stale detection expires");
  // Aborted preview requests deliberately produce browser network errors. Any
  // error before this fault-injection phase is a real regression.
  const unexpected = failures.filter((message) => !message.includes("net::ERR_FAILED"));
  assert.deepEqual(unexpected, [], "no browser runtime errors");
  console.log(JSON.stringify({ passed: true, checks, screenshots: output }, null, 2));
  writeFileSync(join(output, "report.json"), JSON.stringify({ passed: true, checks, actions, consoleErrors: unexpected }, null, 2));
} catch (error) {
  await panel?.screenshot({ path: join(output, "panel-failure.png"), fullPage: true }).catch(() => {});
  await projector?.screenshot({ path: join(output, "projector-failure.png") }).catch(() => {});
  writeFileSync(join(output, "report.json"), JSON.stringify({ passed: false, checks, actions, errors: failures, failure: error.stack }, null, 2));
  throw error;
} finally {
  await browser?.close();
  server.kill("SIGTERM");
  writeFileSync(join(output, "server.log"), serverOutput);
}
