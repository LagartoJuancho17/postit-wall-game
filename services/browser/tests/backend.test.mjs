/**
 * Gate test: the in-browser backend answers the panel's HTTP calls with the
 * routes, checks and statuses of the Python handler, and the full operator
 * journey (preview → launch → win) works through those routes.
 * The JPEG decoder is replaced by golden frames; the rest is the real code.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { BrowserBackend } from "../backend.mjs";
import { GameController, RequestError } from "../controller.mjs";
import { frameMat, loadCv, loadGolden } from "./golden.mjs";

const golden = loadGolden();
const cv = await loadCv();
const WRITE = { "Content-Type": "application/json", "X-Postit-Client": "1" };
const JPEG = { "Content-Type": "image/jpeg", "X-Postit-Client": "1" };

function setup() {
  const clock = { now: 0 };
  const events = [];
  const decoded = [];
  const controller = new GameController({ clock: () => clock.now, storage: null, log: (e) => events.push(e) });
  const decode = async (module, blob) => {
    decoded.push(blob.name);
    if (blob.name === "broken") throw new RequestError("No se pudo decodificar la imagen de la cámara.");
    return frameMat(module, golden, blob.name);
  };
  let tick = 0;
  const backend = new BrowserBackend({ controller, cv: Promise.resolve(cv), decode,
    log: (e) => events.push(e), now: () => (tick += 7) });
  const post = (path, payload, headers = WRITE) => backend.handle(path, { method: "POST", headers,
    body: typeof payload === "string" ? payload : JSON.stringify(payload) });
  const act = (action, data = {}) => post("/api/action", { action, ...data });
  const frame = (path, name, headers = JPEG) => backend.handle(path, { method: "POST", headers, body: { name, size: 10, arrayBuffer() {} } });
  return { backend, controller, clock, events, decoded, post, act, frame };
}

test("reads: state, tick and the USB camera, which the browser build does not have", () => {
  const { backend } = setup();
  const state = backend.handle("/api/state", { cache: "no-store" });
  assert.equal(state.status, 200);
  assert.equal(state.body.phase, "waiting_camera");
  assert.equal(backend.handle("http://example.test/api/tick?x=1").body.phase, "waiting_camera");
  const usb = backend.handle("/api/camera?vendor=046d&product=082d");
  assert.deepEqual(Object.keys(usb.body).sort(), ["available", "reason"]);
  assert.equal(usb.body.available, false);
});

test("refusals carry the Python statuses, messages and the current state", () => {
  const { backend, post } = setup();
  const cases = [
    [backend.handle("/api/nope"), 404, "Ruta desconocida."],
    [backend.handle("/api/state", { method: "DELETE" }), 404, "Ruta desconocida."],
    [post("/api/action", { action: "reset" }, { "Content-Type": "application/json" }), 403,
      "Falta la cabecera X-Postit-Client: 1."],
    [post("/api/elsewhere", {}), 404, "Ruta desconocida."],
    [post("/api/action", { action: "reset" }, { ...WRITE, "Content-Type": "text/plain" }), 415,
      "Content-Type esperado: application/json."],
    [post("/api/frame", "{}", WRITE), 415, "Content-Type esperado: image/jpeg."],
    [post("/api/action", "{nope"), 400, "JSON inválido."],
    [post("/api/action", "[1]"), 400, "La acción debe ser un objeto JSON."],
    [post("/api/action", "null"), 400, "La acción debe ser un objeto JSON."],
    [post("/api/action", { action: "launch" }), 409, "Abrí el proyector y mostrale a la cámara un inicio y una llegada."],
    [post("/api/camera", { settings: { exposureTime: 1 } }), 502,
      "La cámara no aceptó el ajuste por USB: la versión web no tiene control USB."],
  ];
  for (const [answer, status, message] of cases) {
    assert.equal(answer.status, status, message);
    assert.equal(answer.body.error, message);
    assert.equal(answer.body.state.phase, "waiting_camera");
  }
  assert.equal(post("/api/camera", { settings: { exposureMode: "auto" } }).body.error,
    "exposureMode tiene que ser manual: nada queda en automático.");
});

test("headers work as a plain object, a Headers instance or pairs", () => {
  const { backend } = setup();
  const body = JSON.stringify({ action: "projection_open", width: 1280, height: 800 });
  for (const headers of [WRITE, new Headers(WRITE), Object.entries(WRITE)]) {
    assert.equal(backend.handle("/api/action", { method: "POST", headers, body }).status, 200);
  }
});

test("the whole operator journey works through the routes", async () => {
  const { backend, act, frame, clock, controller, events, decoded } = setup();
  const closed = await frame("/api/frame", "wall_430");
  assert.equal(closed.status, 200);
  assert.equal(closed.body.phase, "ready");
  assert.equal(act("projection_open", { width: 1280, height: 800 }).status, 200);
  act("projection_black");
  const label = "Cámara de iPhone de Tobias";
  const scanned = await frame("/api/frame", "wall_430", { ...JPEG, "X-Postit-Camera": encodeURIComponent(label) });
  assert.equal(scanned.status, 200, scanned.body.error);
  assert.equal(scanned.body.phase, "ready");
  assert.equal(scanned.body.camera.label, label);
  assert.deepEqual([scanned.body.camera.width, scanned.body.camera.height], [960, 720]);

  const preview = await frame("/api/preview", "wall_430");
  assert.equal(preview.status, 200);
  assert.deepEqual(Object.fromEntries(Object.entries(preview.body.roles).map(([r, v]) => [r, v.count])),
    { start: 1, goal: 1, bumper: 1, marker: 4 });
  assert.equal(preview.body.quality.ready, true);

  assert.equal(controller.snapshot().phase, "ready");
  assert.equal(controller.snapshot().projection.mode, "live");
  act("aim", { degrees: 0 });
  assert.equal(act("launch").body.phase, "running");
  let state;
  for (let i = 0; i < 30; i += 1) {
    clock.now += 0.1;
    state = backend.handle("/api/tick", { cache: "no-store" }).body;
    if (state.game.status !== "running") break;
  }
  assert.equal(state.game.status, "won");
  assert.ok(state.game.elapsed_s > 1.5 && state.game.elapsed_s < 2.5, state.game.elapsed_s);
  assert.deepEqual(decoded, ["wall_430", "wall_430", "wall_430"]);
  const names = events.map((e) => e.event);
  for (const event of ["board_scanned", "live_board_ready", "attempt_started", "attempt_finished"]) {
    assert.ok(names.includes(event), `${event} is logged`);
  }
});

test("a bad camera label is refused before the frame is decoded; decoder errors keep their status", async () => {
  const { act, frame, decoded } = setup();
  act("projection_open", { width: 1280, height: 800 });
  const long = await frame("/api/frame", "wall_430", { ...JPEG, "X-Postit-Camera": "x".repeat(201) });
  assert.equal(long.status, 400);
  assert.equal(long.body.error, "camera debe ser el nombre de la cámara (1 a 200 caracteres).");
  assert.deepEqual(decoded, []);
  const broken = await frame("/api/preview", "broken");
  assert.equal(broken.status, 400);
  assert.equal(broken.body.error, "No se pudo decodificar la imagen de la cámara.");
  const tiny = await frame("/api/preview", "tiny");
  assert.equal(tiny.body.error, "La imagen de la webcam es demasiado pequeña");
});

test("a failed OpenCV download is reported, not hung on", async () => {
  const controller = new GameController({ storage: null });
  const backend = new BrowserBackend({ controller, cv: Promise.reject(new Error("No se pudo descargar OpenCV.")),
    decode: async () => assert.fail("never decodes") });
  const answer = await backend.handle("/api/preview", { method: "POST", headers: JPEG, body: { size: 1 } });
  assert.equal(answer.status, 502);
  assert.equal(answer.body.error, "No se pudo descargar OpenCV.");
});

test("live previews leave a timing trace every 25 frames", async () => {
  const { frame, events, controller } = setup();
  controller.preview = () => ({ roles: {} }); // the trace, not the detection, is under test
  for (let i = 0; i < 25; i += 1) await frame("/api/preview", "tiny");
  const timing = events.filter((e) => e.event === "preview_timing");
  assert.equal(timing.length, 1);
  assert.deepEqual(Object.keys(timing[0]).sort(), ["at", "event", "frames", "height", "p50_ms", "p95_ms", "width"]);
  assert.equal(timing[0].frames, 25);
  assert.equal(timing[0].p50_ms, 7);
});

test("palette and camera presets survive a reload through storage; damaged storage falls back", () => {
  const store = new Map();
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)) };
  const first = new GameController({ storage });
  first.action({ action: "palette_set", role: "goal", hex: "#e0402a" });
  first.action({ action: "camera_preset_save", camera: "C920", settings: { exposureTime: 20 }, score: 1 });
  const second = new GameController({ storage }).snapshot();
  assert.equal(second.palette.roles.goal.hex, "#e0402a");
  assert.equal(second.palette.custom, true);
  assert.deepEqual(second.camera_presets.C920.settings, { exposureTime: 20 });
  store.set("postit-wall:palette", "{broken");
  store.set("postit-wall:camera-presets", JSON.stringify({ presets: { C920: { settings: { zoom: 1 } } } }));
  const events = [];
  const third = new GameController({ storage, log: (e) => events.push(e.event) }).snapshot();
  assert.equal(third.palette.custom, false);
  assert.deepEqual(third.camera_presets, {});
  assert.deepEqual(events, ["palette_ignored", "camera_presets_ignored"]);
  const blocked = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };
  const logged = [];
  const private_ = new GameController({ storage: blocked, log: (e) => logged.push(e.event) });
  assert.equal(private_.action({ action: "palette_set", role: "goal", hex: "#e0402a" }).palette.roles.goal.hex, "#e0402a");
  assert.deepEqual(logged.filter((e) => e !== "palette_changed"), ["palette_not_saved"]);
});
