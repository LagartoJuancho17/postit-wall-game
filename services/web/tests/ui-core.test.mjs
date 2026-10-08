/** Gate tests for the pure view logic in ui/ui-core.mjs. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  attemptPhase,
  attemptView,
  boardCounts,
  cameraName,
  controlsFor,
  isTypingTarget,
  normalizeAngle,
  pointInCanvas,
  preferredCamera,
  projectionSize,
  SETUP_STAGES,
  setupGuide,
  statusText,
} from "../ui/ui-core.mjs";

const matrix = JSON.parse(readFileSync(new URL("../contract/state-matrix.json", import.meta.url), "utf8"));
const CONTROLS = ["scan", "calibrate", "launch", "aim", "reset", "resume"];

// Expected panel controls per backend phase, with the camera live and the
// projector window ready whenever the backend reports it open. Hand-written:
// the eval derives agreement from the backend, this pins the intended UI.
const EXPECTED = {
  projector_closed: [],
  waiting_camera: ["scan"],
  scanned: ["scan", "calibrate"],
  calibrating: ["scan", "calibrate"],
  ready: ["scan", "calibrate", "launch", "aim", "reset"],
  ready_closed: [],
  running: ["reset"],
  paused_open: ["scan", "calibrate", "reset", "resume"],
  paused_closed: [],
  won: ["scan", "calibrate", "reset"],
  lost: ["scan", "calibrate", "reset"],
};

function enabled(controls) {
  return CONTROLS.filter((name) => controls[name]);
}

test("every backend phase enables exactly the intended controls", () => {
  assert.deepEqual(Object.keys(matrix.phases).sort(), Object.keys(EXPECTED).sort());
  for (const [name, { state }] of Object.entries(matrix.phases)) {
    const controls = controlsFor(state, true, state.projection.open);
    assert.deepEqual(enabled(controls), EXPECTED[name], name);
    for (const value of Object.values(controls)) assert.equal(typeof value, "boolean", name);
  }
});

test("a paused attempt can be resumed, rescanned and recalibrated", () => {
  // Regression: closing the projector pauses the attempt (status stays
  // "running"); the panel used to treat that as moving and lock the operator out.
  const { state } = matrix.phases.paused_open;
  assert.equal(state.game.status, "running");
  assert.equal(state.game.paused, true);
  const controls = controlsFor(state, true, true);
  assert.equal(controls.resume, true);
  assert.equal(controls.scan, true);
  assert.equal(controls.calibrate, true);
  assert.equal(controls.launch, false);
});

test("without a live camera nothing that needs a frame is enabled", () => {
  for (const [name, { state }] of Object.entries(matrix.phases)) {
    const controls = controlsFor(state, false, true);
    assert.equal(controls.scan, false, name);
    assert.equal(controls.calibrate, false, name);
    assert.equal(controls.launch, false, name);
  }
});

test("without the projector window every control is disabled", () => {
  for (const [name, { state }] of Object.entries(matrix.phases)) {
    assert.deepEqual(enabled(controlsFor(state, true, false)), [], name);
  }
});

test("controlsFor tolerates the empty state before the first poll", () => {
  assert.deepEqual(enabled(controlsFor(null, true, true)), ["scan"]);
  assert.deepEqual(enabled(controlsFor(undefined, undefined, undefined)), []);
});

// Next setup step per backend phase, camera live, projector ready when open.
const EXPECTED_STEP = {
  projector_closed: "projector",
  waiting_camera: "scan",
  scanned: "calibrate",
  calibrating: "align",
  ready: "play",
  ready_closed: "projector",
  running: "play",
  paused_open: "play",
  paused_closed: "projector",
  won: "play",
  lost: "play",
};

test("setupGuide names the next step for every backend phase", () => {
  assert.deepEqual(Object.keys(EXPECTED_STEP).sort(), Object.keys(matrix.phases).sort());
  for (const [name, { state }] of Object.entries(matrix.phases)) {
    assert.equal(setupGuide(state, true, state.projection.open).step, EXPECTED_STEP[name], name);
    assert.equal(setupGuide(state, false, state.projection.open).step, "camera", name);
  }
});

test("live camera without the projector window points at J, not at scanning", () => {
  // Regression: every button was disabled while the only message said
  // "Cambió la cámara. Escaneá y calibrá otra vez." and the projector was closed.
  const state = {
    ...matrix.phases.projector_closed.state,
    message: "Cambió la cámara. Escaneá y calibrá otra vez.",
  };
  assert.deepEqual(enabled(controlsFor(state, true, false)), []);
  const guide = setupGuide(state, true, false);
  assert.deepEqual([guide.step, guide.stage, guide.blocking], ["projector", "projector", true]);
  assert.match(guide.message, /\bJ\b/);
  assert.doesNotMatch(guide.message, /escaneá/i);
  assert.equal(statusText(null, state, guide), guide.message);
});

test("the guide never sends the operator to a disabled button", () => {
  for (const [name, { state }] of Object.entries(matrix.phases)) {
    for (const camera of [true, false]) {
      for (const popup of [true, false]) {
        const ready = popup && state.projection.open;
        const where = `${name} camera=${camera} projector=${ready}`;
        const guide = setupGuide(state, camera, ready);
        const controls = controlsFor(state, camera, ready);
        if (guide.step === "scan" || guide.step === "calibrate") {
          assert.equal(controls[guide.step], true, where);
        }
        assert.ok(SETUP_STAGES.includes(guide.stage), where);
        assert.ok(guide.message, where);
      }
    }
  }
});

test("statusText: local notice, then browser blocker, then backend message", () => {
  const state = { message: "Tablero leído. Iniciá la calibración." };
  const blocking = { blocking: true, message: "Siguiente: abrí la proyección con J." };
  const clear = { blocking: false, message: "La proyección se apaga un instante al escanear." };
  assert.equal(statusText({ message: "Leyendo los post-its…" }, state, blocking), "Leyendo los post-its…");
  assert.equal(statusText(null, state, blocking), blocking.message);
  assert.equal(statusText(null, state, clear), state.message);
  assert.match(statusText(null, null, clear), /Activá la cámara/);
});

test("attemptPhase separates a paused attempt from a moving one", () => {
  assert.equal(attemptPhase(undefined), "none");
  assert.equal(attemptPhase({ status: "ready", paused: false }), "ready");
  assert.equal(attemptPhase({ status: "running", paused: false }), "running");
  assert.equal(attemptPhase({ status: "running", paused: true }), "paused");
  assert.equal(attemptPhase({ status: "won", paused: true }), "won");
});

test("attemptView labels every backend phase and uses a styled tone", () => {
  const css = readFileSync(new URL("../ui/app.css", import.meta.url), "utf8");
  for (const [name, { state }] of Object.entries(matrix.phases)) {
    const view = attemptView(state.game);
    assert.ok(view.label && view.hint, name);
    assert.notEqual(view.label, view.phase, `${name} has no Spanish label`);
    if (!["none", "ready"].includes(view.phase)) {
      assert.match(css, new RegExp(`\\.game-pill\\[data-tone="${view.phase}"\\]`), name);
    }
  }
  assert.equal(attemptView(matrix.phases.paused_open.state.game).label, "En pausa");
  assert.deepEqual(attemptView({ status: "mystery" }), { phase: "mystery", label: "mystery", hint: "" });
});

test("preferredCamera picks the Logitech C920, else the first camera", () => {
  const facetime = { kind: "videoinput", deviceId: "a", label: "FaceTime HD Camera" };
  const c920 = { kind: "videoinput", deviceId: "b", label: "HD Pro Webcam C920 (046d:082d)" };
  const logitech = { kind: "videoinput", deviceId: "c", label: "Logitech BRIO" };
  const mic = { kind: "audioinput", deviceId: "d", label: "Logitech C920 microphone" };
  assert.equal(preferredCamera([facetime, c920]), c920);
  assert.equal(preferredCamera([mic, facetime, logitech]), logitech);
  assert.equal(preferredCamera([mic, facetime]), facetime);
  assert.equal(preferredCamera([mic]), null);
  assert.equal(preferredCamera([]), null);
});

test("the camera the operator picked wins over the C920 after a reload", () => {
  const c920 = { kind: "videoinput", deviceId: "b", label: "HD Pro Webcam C920 (046d:082d)" };
  const phone = { kind: "videoinput", deviceId: "p", label: "Cámara de iPhone de Tobias" };
  assert.equal(preferredCamera([c920, phone], "Cámara de iPhone de Tobias"), phone);
  // A remembered camera that is gone falls back to the usual order.
  assert.equal(preferredCamera([c920], "Cámara de iPhone de Tobias"), c920);
  assert.equal(preferredCamera([c920, phone], ""), c920);
  const operator = readFileSync(new URL("../ui/app.js", import.meta.url), "utf8");
  assert.match(operator, /rememberCamera\(cameraSelect\.selectedOptions\[0\]\?\.textContent\);\s*startCamera\(cameraSelect\.value, false\);/);
  assert.match(operator, /const preferred = preferredCamera\(cameras, rememberedCamera\(\)\);/);
  // The list shows trimmed labels, so the remembered one is compared trimmed.
  assert.equal(preferredCamera([c920, { ...phone, label: "Cámara de iPhone de Tobias " }], "Cámara de iPhone de Tobias")?.deviceId, "p");
  // Regression: a retry after a failed start opened the default camera, not
  // the remembered one, and that discarded the board scanned with it.
  assert.match(operator, /\$\("retry-camera"\)\.addEventListener\("click", \(\) => startCamera\(cameraSelect\.value \|\| "", !cameraSelect\.value\)\);/);
});

test("cameraName falls back to a numbered name when the label is hidden", () => {
  assert.equal(cameraName({ label: "  FaceTime HD Camera " }), "FaceTime HD Camera");
  assert.equal(cameraName({ label: "" }, 1), "Cámara 2");
  assert.equal(cameraName(undefined), "Cámara 1");
});

test("boardCounts prefers the camera board and survives missing data", () => {
  const { state } = matrix.phases.ready;
  assert.deepEqual(boardCounts(state), {
    start: 1, goal: 1, bumpers: state.camera_board.bumpers.length, markers: 4,
  });
  assert.deepEqual(boardCounts({ camera_board: null, board: { start: {}, bumpers: [{}] } }),
    { start: 1, goal: 0, bumpers: 1, markers: 0 });
  assert.deepEqual(boardCounts(null), { start: 0, goal: 0, bumpers: 0, markers: 0 });
});

test("isTypingTarget ignores the angle slider but not text fields", () => {
  const selectors = [];
  const target = (match) => ({ closest(selector) { selectors.push(selector); return match; } });
  assert.equal(isTypingTarget(target({})), true);
  assert.equal(isTypingTarget(target(null)), false);
  assert.match(selectors[0], /input:not\(\[type='range'\]\)/);
  assert.match(selectors[0], /select/);
  assert.match(selectors[0], /textarea/);
  assert.equal(isTypingTarget(null), false);
  assert.equal(isTypingTarget({}), false);
});

test("pointInCanvas maps CSS pixels to canvas pixels and clamps to the edge", () => {
  const rect = { left: 10, top: 20, width: 640, height: 400 };
  assert.deepEqual(pointInCanvas(330, 220, rect, 1280, 800), { x: 640, y: 400 });
  assert.deepEqual(pointInCanvas(-50, -50, rect, 1280, 800), { x: 0, y: 0 });
  assert.deepEqual(pointInCanvas(5000, 5000, rect, 1280, 800), { x: 1279, y: 799 });
});

test("normalizeAngle clamps to ±180 and rejects non-numbers", () => {
  assert.equal(normalizeAngle("-45"), -45);
  assert.equal(normalizeAngle(185), 180);
  assert.equal(normalizeAngle(-400), -180);
  assert.throws(() => normalizeAngle("abc"), TypeError);
  assert.throws(() => normalizeAngle(Infinity), TypeError);
  assert.throws(() => normalizeAngle(undefined), TypeError);
});

test("projectionSize reports whole positive CSS pixels", () => {
  assert.deepEqual(projectionSize({ innerWidth: 1280.7, innerHeight: 800.2 }), { width: 1280, height: 800 });
  assert.deepEqual(projectionSize({ innerWidth: 0, innerHeight: -3 }), { width: 1, height: 1 });
});
