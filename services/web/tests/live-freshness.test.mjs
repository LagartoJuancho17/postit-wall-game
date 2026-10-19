/** Exercise the real async UI functions with delayed HTTP responses and a fake clock. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import test from "node:test";

import { LIVE_MAX_AGE_MS } from "../ui/live-view.mjs";

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

function projectorView() {
  const request = deferred();
  const clock = { now: 0 };
  const draws = [];
  const frames = [];
  const ctx = { fillRect() {} };
  const context = vm.createContext({
    Promise, console, performance: { now: () => clock.now },
    AbortController, LIVE_MAX_AGE_MS,
    api: () => request.promise,
    responseJSON: (answer) => answer.json(),
    withTimeout: (promise) => promise,
    projectorBackground: () => "#000000",
    drawLiveDetection: (context, detection) => { if (detection) draws.push("detection"); },
    drawLiveGame: (context, game) => { if (game?.position) draws.push("game"); },
    document: { body: { dataset: {} } },
    ctx, stateLabel: {}, toast: {}, showError: () => {},
    setTimeout: () => 1, clearTimeout: () => {},
    resizeCanvas: () => {}, syncProjectionSize: () => {},
    setInterval: (callback, ms) => { frames.push({ callback, ms }); return frames.length; },
  });
  vm.runInContext(`
    let state = null, stateReceivedAt = 0, blackoutForced = false, tickBusy = false;
    let viewport = { width: 1280, height: 720 }, theme = "dark";
    ${segment(projector, "function applyState(", "async function syncProjectionSize()")}
    ${segment(projector, "async function tick()", "\nresizeCanvas();")}
    globalThis.controls = { applyState, draw, tick };
    ${projector.slice(projector.lastIndexOf("\nresizeCanvas();"))}
  `, context);
  assert.equal(frames.length, 1, "projector installs one rendering/tick clock");
  assert.equal(frames[0].ms, 33);
  return { ...context.controls, request, clock, draws, frame: frames[0].callback };
}

test("projector clears expired contours while its next tick request is stalled", async () => {
  const view = projectorView();
  view.applyState({ live_detection: oldPreview, palette: {}, projection: { mode: "live" }, game: { status: "none" } });
  assert.ok(view.draws.includes("detection"), "a fresh reply is drawn");
  const pendingTick = view.tick();
  view.draws.length = 0;
  view.clock.now = LIVE_MAX_AGE_MS + 1;
  view.frame(); // exercise the real interval callback while its HTTP request waits
  const expiredDraws = [...view.draws];
  view.request.resolve(response({ live_detection: null, projection: { mode: "live" }, game: { status: "none" } }));
  await pendingTick;
  assert.deepEqual(expiredDraws, [], "a stalled network request cannot keep old contours on the wall");
});
