/** Gate tests: color pickers, eyedropper and live detection lights. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  attemptView,
  averageHex,
  containTransform,
  framePointFromClick,
  PALETTE_ROLES,
  patchOrigin,
  ROLE_LABELS,
  roleLights,
  setupGuide,
} from "../ui/ui-core.mjs";

const read = (path) => readFileSync(new URL(path, import.meta.url), "utf8");
const samples = JSON.parse(read("../contract/preview-samples.json")).samples;
const matrix = JSON.parse(read("../contract/state-matrix.json"));
const page = read("../ui/index.html");
const operator = read("../ui/app.js");
const projectorPage = read("../ui/projector.html");
const projector = read("../ui/projector.js");

const lightStates = (preview) => {
  const lights = roleLights(preview);
  return Object.fromEntries(PALETTE_ROLES.map((role) => [role, lights[role].state]));
};

test("lights follow real preview responses from the backend", () => {
  assert.deepEqual(lightStates(samples.full_wall.preview),
    { start: "on", goal: "on", bumper: "on", marker: "on" });
  assert.deepEqual(lightStates(samples.missing_start.preview),
    { start: "off", goal: "on", bumper: "on", marker: "on" });
  // Default colors on a recolored wall: a yellow bumper passes for a start
  // (the halo shows which note), the red goal adds a fifth corner.
  assert.deepEqual(lightStates(samples.recolored_default_colors.preview),
    { start: "on", goal: "off", bumper: "off", marker: "warn" });
  assert.deepEqual(lightStates(samples.recolored_picked_colors.preview),
    { start: "on", goal: "on", bumper: "on", marker: "on" });
  const full = roleLights(samples.full_wall.preview);
  assert.equal(full.marker.text, "4/4");
  assert.equal(full.start.text, "1/1");
  assert.equal(full.bumper.text, "1");
});

test("lights stay neutral before any preview and warn on wrong counts", () => {
  for (const role of PALETTE_ROLES) {
    assert.deepEqual(roleLights(null)[role], { state: "", count: null, text: "—" });
  }
  const preview = { roles: { start: { count: 2 }, goal: { count: 0 }, bumper: { count: 3 }, marker: { count: 3 } } };
  assert.deepEqual(lightStates(preview), { start: "warn", goal: "off", bumper: "on", marker: "warn" });
  assert.equal(roleLights(preview).start.text, "2/1");
});

test("containTransform matches object-fit: contain", () => {
  assert.deepEqual(containTransform(1280, 720, 640, 480), { scale: 1.5, offsetX: 160, offsetY: 0 });
  assert.deepEqual(containTransform(800, 800, 1600, 900), { scale: 0.5, offsetX: 0, offsetY: 175 });
});

test("a click maps to the frame pixel under it; the black bars map to nothing", () => {
  const rect = { left: 100, top: 50, width: 1280, height: 720 };
  // A 640×480 camera shown at 1.5× leaves 160 px bars left and right.
  assert.deepEqual(framePointFromClick(260, 50, rect, 640, 480), { x: 0, y: 0 });
  assert.deepEqual(framePointFromClick(740, 410, rect, 640, 480), { x: 320, y: 240 });
  assert.equal(framePointFromClick(200, 350, rect, 640, 480), null);
  assert.equal(framePointFromClick(1300, 350, rect, 640, 480), null);
  assert.equal(framePointFromClick(500, 350, rect, 0, 0), null);
});

test("the eyedropper patch stays inside the frame", () => {
  assert.deepEqual(patchOrigin({ x: 320, y: 240 }, 640, 480, 7), { x: 317, y: 237 });
  assert.deepEqual(patchOrigin({ x: 1, y: 478 }, 640, 480, 7), { x: 0, y: 473 });
  assert.deepEqual(patchOrigin({ x: 639.6, y: 0 }, 640, 480, 7), { x: 633, y: 0 });
});

test("averageHex is the mean RGB of the patch, ignoring alpha", () => {
  assert.equal(averageHex(new Uint8ClampedArray([255, 0, 0, 255, 0, 0, 255, 0])), "#800080");
  assert.equal(averageHex([16, 32, 48, 255]), "#102030");
  assert.throws(() => averageHex([]), TypeError);
  assert.throws(() => averageHex([1, 2, 3]), TypeError);
});

test("every role has its light, swatch, count and eyedropper in the page", () => {
  assert.deepEqual(Object.keys(ROLE_LABELS), PALETTE_ROLES);
  for (const role of PALETTE_ROLES) {
    for (const kind of ["light", "color", "seen", "pick"]) {
      assert.match(page, new RegExp(`id="${kind}-${role}"`), `${kind}-${role}`);
    }
    assert.match(page, new RegExp(`class="note-dot" data-role="${role}"`), role);
  }
  for (const id of ["palette-reset", "palette-note", "pick-banner"]) {
    assert.match(page, new RegExp(`id="${id}"`), id);
  }
});

test("the panel wires pickers, eyedropper and live preview to the backend", () => {
  assert.match(operator, /\$\(`color-\$\{role\}`\)\.addEventListener\("change", \(event\) => setColor\(role, event\.target\.value\)\)/);
  assert.match(operator, /\$\(`pick-\$\{role\}`\)\.addEventListener\("click", \(\) => togglePick\(role\)\)/);
  assert.match(operator, /action\("palette_set", \{ role, hex \}\)/);
  assert.match(operator, /sendAction\("palette_reset"\)/);
  assert.match(operator, /setColor\(role, sampleHex\(point\)\)/);
  assert.match(operator, /fetch\("\/api\/preview"/);
  assert.match(operator, /if \(previewBusy \|\| scanBusy \|\| tuning \|\| document\.hidden/);
  assert.match(operator, /response\.status === 404\) \{\s*previewSupported = false;/);
});

test("no text names a fixed color now that the operator chooses them", () => {
  const texts = [
    setupGuide(null, false, false).message,
    setupGuide(null, true, false).message,
    setupGuide({}, true, true).message,
    setupGuide({ camera_board: {} }, true, true).message,
    setupGuide({ calibration: { status: "collecting" } }, true, true).message,
    ...["none", "ready", "running", "won", "lost"].map((status) => attemptView({ status }).hint),
    attemptView({ status: "running", paused: true }).hint,
    ...Object.values(matrix.phases).flatMap(({ state }) => [state.message, state.calibration.detail]),
    projectorPage,
    projector,
  ];
  for (const text of texts) assert.doesNotMatch(text, /fucsia|amarill|naranja|verde/i, text.slice(0, 80));
});
