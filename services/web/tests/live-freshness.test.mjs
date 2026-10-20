/** Exercise the real async UI functions with delayed HTTP responses and a fake clock. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

import { drawAlignmentPattern, drawProjectorScene, LIVE_MAX_AGE_MS, projectorScene } from "../ui/live-view.mjs";
import { projectorBackground } from "../ui/ui-core.mjs";

const operator = readFileSync(new URL("../ui/app.js", import.meta.url), "utf8");
const projector = readFileSync(new URL("../ui/projector.js", import.meta.url), "utf8");
const segment = (source, start, end) => {
  const first = source.indexOf(start);
  const last = source.indexOf(end, first + start.length);
  assert.ok(first >= 0 && last > first, `${start} remains available for behavioral testing`);
  return source.slice(first, last);
};
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const response = (body) => ({ status: 200, ok: true, json: async () => body });
const oldPreview = { width: 960, height: 720,
  roles: { start: { count: 1, notes: [{ points: [{ x: 111, y: 222 }, { x: 120, y: 222 }, { x: 120, y: 230 }] }] } } };
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

function panel() {
  const request = deferred();
  const paletteAction = deferred();
  const clock = { now: 0 };
  const calls = [];
  const track = { label: "Camera A", readyState: "live", stop() {} };
  const stream = { getTracks: () => [track], getVideoTracks: () => [track] };
  const video = { videoWidth: 960, srcObject: stream };
  const context = vm.createContext({
    Promise, console, performance: { now: () => clock.now },
    api: () => request.promise,
    responseJSON: (answer) => answer.json(),
    action: () => paletteAction.promise,
    applyState: (next) => calls.push({ type: "state", next }),
    jpegFromVideo: async () => ({ size: 1 }),
    schedulePreview: () => calls.push({ type: "schedule" }),
    render: () => {}, freshPreview: () => null,
    pickVerdict: () => null, weakColor: () => false,
    clearNotice: () => {}, notice: (message) => calls.push({ type: "notice", message }),
    ROLE_LABELS: { start: "inicio" }, document: { activeElement: null },
    $: () => ({ value: "" }),
    initialStream: stream, initialVideo: video,
  });
  vm.runInContext(`
    let previewGeneration = 0, previewTimer = null, previewBusy = false;
    let scanBusy = false, tuning = null, stream = initialStream, video = initialVideo;
    let previewSupported = true, preview = null, pickCheck = null, paletteError = null;
    let state = { palette: { roles: { start: { hex: "#ffdf53" } } } };
    ${segment(operator, "function stopCamera()", "// The camera the operator picked")}
    ${segment(operator, "async function previewTick()", "function sampleHex(")}
    ${segment(operator, "async function setColor(", "function currentTrack()")}
    ${segment(operator, "function currentTrack()", "function cameraKeys(")}
    ${segment(operator, "async function sendAction(", "function setAngle(")}
    globalThis.controls = {
      previewTick, setColor, sendAction, stopCamera,
      current: () => preview,
      installCamera: (next) => { stream = next; },
    };
  `, context);
  return { ...context.controls, request, paletteAction, clock, calls, track };
}

test("a preview from before a successful color change cannot repaint old notes", async () => {
  const ui = panel();
  const pendingPreview = ui.previewTick();
  await flush();
  const pendingColor = ui.setColor("start", "#30d0c0");
  ui.paletteAction.resolve({ palette: { roles: { start: { hex: "#30d0c0" } } } });
  await pendingColor;
  ui.request.resolve(response(oldPreview));
  await pendingPreview;
  assert.equal(ui.current(), null, "the old palette's pending result must stay discarded");
  assert.equal(ui.calls.filter((call) => call.type === "schedule").length, 1,
    "discarding an old response still schedules the next preview");
});

test("palette reset invalidates pending previews while the reset request is still waiting", async () => {
  const ui = panel();
  const pendingPreview = ui.previewTick();
  await flush();
  const pendingReset = ui.sendAction("palette_reset");
  ui.request.resolve(response(oldPreview));
  await pendingPreview;
  const duringReset = ui.current();
  ui.paletteAction.resolve({ palette: { roles: {} } });
  await pendingReset;
  assert.equal(duringReset, null, "reset must invalidate old responses before its HTTP request completes");
  assert.equal(ui.current(), null);
});

test("a preview begun during palette reset cannot outlive the successful reset", async () => {
  const ui = panel();
  const pendingReset = ui.sendAction("palette_reset");
  const pendingPreview = ui.previewTick();
  await flush();
  ui.paletteAction.resolve({ palette: { roles: {} } });
  await pendingReset;
  ui.request.resolve(response(oldPreview));
  await pendingPreview;
  assert.equal(ui.current(), null, "a response from the palette being reset is obsolete after success");
});

test("a color change while the response JSON is pending also discards that preview", async () => {
  const ui = panel();
  const body = deferred();
  const pendingPreview = ui.previewTick();
  await flush();
  ui.request.resolve({ status: 200, ok: true, json: () => body.promise });
  await flush();
  const pendingColor = ui.setColor("start", "#30d0c0");
  ui.paletteAction.resolve({ palette: { roles: { start: { hex: "#30d0c0" } } } });
  await pendingColor;
  body.resolve(oldPreview);
  await pendingPreview;
  assert.equal(ui.current(), null, "checking generation only before response.json() is insufficient");
});

test("stopping and replacing the camera discards its pending preview", async () => {
  const ui = panel();
  const pendingPreview = ui.previewTick();
  await flush();
  ui.stopCamera();
  const nextTrack = { label: "Camera B", readyState: "live", stop() {} };
  ui.installCamera({ getTracks: () => [nextTrack], getVideoTracks: () => [nextTrack] });
  ui.request.resolve(response(oldPreview));
  await pendingPreview;
  assert.equal(ui.current(), null, "Camera A polygons cannot appear over Camera B");
  assert.equal(ui.calls.filter((call) => call.type === "schedule").length, 1);
});

test("a current preview is accepted and a failed palette action leaves it usable", async () => {
  const ui = panel();
  const pendingPreview = ui.previewTick();
  await flush();
  const pendingColor = ui.setColor("start", "#badbad");
  ui.paletteAction.reject(new Error("Rol de color rechazado."));
  await pendingColor;
  ui.request.resolve(response(oldPreview));
  await pendingPreview;
  assert.deepEqual(JSON.parse(JSON.stringify(ui.current().roles)), oldPreview.roles,
    "a failed edit does not invalidate an otherwise current detection");
  assert.equal(ui.current().at, 0);
});

/** A 2D context that records what reaches the projector: background rects, contours, dots and texts. */
function recordingContext() {
  const draws = [];
  const stack = [];
  let path = [];
  return {
    draws, fillStyle: "#000000", strokeStyle: "#000000", globalAlpha: 1,
    save() { stack.push({ fillStyle: this.fillStyle, strokeStyle: this.strokeStyle, globalAlpha: this.globalAlpha }); },
    restore() { Object.assign(this, stack.pop()); },
    beginPath() { path = []; }, closePath() {},
    moveTo(x, y) { path.push({ x, y }); }, lineTo(x, y) { path.push({ x, y }); },
    arc(x, y, radius) { path.push({ x, y, radius }); },
    fillRect(...rect) { draws.push({ kind: "rect", color: this.fillStyle, rect }); },
    fill() { draws.push({ kind: path.length === 1 && path[0].radius ? "dot" : "fill", color: this.fillStyle, alpha: this.globalAlpha }); },
    stroke() { draws.push({ kind: "stroke", color: this.strokeStyle }); },
    fillText(text) { draws.push({ kind: "text", text, color: this.fillStyle }); },
  };
}
const contours = (draws) => draws.filter((draw) => draw.kind === "stroke" || draw.kind === "fill");

function projectorView() {
  const request = deferred();
  const clock = { now: 0 };
  const frames = [];
  const ctx = recordingContext();
  const body = { dataset: {} };
  const stateLabel = { textContent: "" };
  const context = vm.createContext({
    Promise, console, performance: { now: () => clock.now },
    AbortController, LIVE_MAX_AGE_MS,
    api: () => request.promise,
    responseJSON: (answer) => answer.json(),
    withTimeout: (promise) => promise,
    projectorBackground, projectorScene, drawAlignmentPattern, drawProjectorScene,
    document: { body },
    ctx, stateLabel, toast: {}, showError: () => {},
    setTimeout: () => 1, clearTimeout: () => {},
    resizeCanvas: () => {}, syncProjectionSize: () => {},
    setInterval: (callback, ms) => { frames.push({ callback, ms }); return frames.length; },
  });
  vm.runInContext(`
    let state = null, stateReceivedAt = 0, blackoutForced = false, tickBusy = false, localScene = null;
    let viewport = { width: 1280, height: 720 }, theme = "dark";
    ${segment(projector, "function applyState(", "async function syncProjectionSize()")}
    ${segment(projector, "async function tick()", "\nresizeCanvas();")}
    globalThis.controls = {
      applyState, draw, tick,
      setLocal: (scene) => { localScene = scene; },
      setBlackout: (value) => { blackoutForced = value; },
      setTheme: (value) => { theme = value; },
    };
    ${projector.slice(projector.lastIndexOf("\nresizeCanvas();"))}
  `, context);
  assert.equal(frames.length, 1, "projector installs one rendering/tick clock");
  assert.equal(frames[0].ms, 33);
  const fresh = () => { ctx.draws.length = 0; };
  return { ...context.controls, request, clock, draws: ctx.draws, fresh, body, stateLabel, frame: frames[0].callback };
}

test("projector clears expired contours while its next tick request is stalled", async () => {
  const view = projectorView();
  view.applyState({ live_detection: oldPreview, palette: {}, projection: { mode: "live" }, game: { status: "none" } });
  assert.ok(contours(view.draws).length > 0, "a fresh reply is drawn");
  const pendingTick = view.tick();
  view.fresh();
  view.clock.now = LIVE_MAX_AGE_MS + 1;
  view.frame(); // exercise the real interval callback while its HTTP request waits
  const expiredDraws = contours(view.draws);
  view.request.resolve(response({ live_detection: null, projection: { mode: "live" }, game: { status: "none" } }));
  await pendingTick;
  assert.deepEqual(expiredDraws, [], "a stalled network request cannot keep old contours on the wall");
});

// docs/alignment-and-color.md 2.2 and 3, through projector.js's real draw().
test("the projector draw path follows the projection mode and the panel's local scenes", () => {
  const H = [[2, 0, 0], [0, 1, 0], [0, 0, 1]];
  const live = {
    live_detection: { ...oldPreview, roles: { start: { count: 1, notes: [{ points: [
      { x: 100, y: 100 }, { x: 160, y: 100 }, { x: 160, y: 160 }, { x: 100, y: 160 }] }] } } },
    palette: { roles: { start: { hex: "#30d0c0" } } }, game: { status: "ready", position: { x: 130, y: 130 }, width: 960, height: 720 },
    alignment: { status: "ready", matrix: H, projector: { width: 1280, height: 720 }, camera: { width: 960, height: 720 },
      overlay: { status: "unchecked", fill: false } },
  };
  const layout = { radius: 4, dots: [{ id: 1, x: 20, y: 20 }, { id: 2, x: 60, y: 20 }, { id: 3, x: 100, y: 20 }] };
  const view = projectorView();
  const show = (state, local = null) => {
    view.setLocal(local);
    view.fresh();
    view.applyState(state);
    return [...view.draws];
  };
  for (const theme of ["dark", "light"]) {
    view.setTheme(theme);
    const background = projectorBackground(theme, false);

    const neutral = show({ ...live, projection: { mode: "neutral" } });
    assert.deepEqual(neutral.map((draw) => [draw.kind, draw.color]), [["rect", background]], `${theme}: neutral is the theme background only`);
    assert.equal(view.body.dataset.mode, "neutral");
    assert.equal(view.stateLabel.textContent, "");

    for (const mode of ["align", "black"]) {
      const black = show({ ...live, projection: { mode } });
      assert.deepEqual(black.at(-1), { kind: "rect", color: "#000000", rect: [0, 0, 1280, 720] }, `${theme}: ${mode} ends pure black`);
      assert.equal(black.filter((draw) => draw.kind !== "rect").length, 0);
      assert.equal(view.body.dataset.mode, mode);
    }

    const pattern = show({ ...live, projection: { mode: "live" } }, { kind: "pattern", layout, frame: { bit: 0, inverse: false } });
    assert.deepEqual(pattern.filter((draw) => draw.kind === "dot").map((draw) => draw.color), ["#ffffff", "#ffffff"], "ids 1 and 3 have bit 0 set");
    assert.equal(pattern.filter((draw) => draw.kind === "rect").at(-1).color, "#000000");
    assert.equal(contours(pattern).length, 0, "no notes or ball over a pattern");
    assert.equal(view.body.dataset.mode, "align");
    assert.equal(drawAlignmentPattern(recordingContext(), layout, "warmup", 1280, 720), 2, "warm-up = bit 0 frame");

    const checked = show({ ...live, projection: { mode: "live" } }, { kind: "overlay_test" });
    assert.deepEqual(checked.filter((draw) => draw.kind === "fill").map((draw) => [draw.color, draw.alpha]), [["#30d0c0", 0.35]],
      "the overlay check fills even before overlay.fill");
    const plain = show({ ...live, projection: { mode: "live" } });
    assert.equal(plain.filter((draw) => draw.kind === "fill").length, 0, "no fill until the check allows it");
    assert.equal(plain.filter((draw) => draw.kind === "stroke").length, 2, "rim and aim line");
    assert.equal(view.body.dataset.mode, "live");
    assert.equal(view.stateLabel.textContent, "Post-it Wall · Detección en vivo");

    view.setBlackout(true);
    const scan = show({ ...live, projection: { mode: "live" } }, { kind: "pattern", layout, frame: { bit: 0, inverse: false } });
    assert.equal(scan.filter((draw) => draw.kind !== "rect").length, 0, "a scan blackout beats every local scene");
    assert.ok(scan.every((draw) => draw.color === "#000000"));
    view.setBlackout(false);
  }
});
