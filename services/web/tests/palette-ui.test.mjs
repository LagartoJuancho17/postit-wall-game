/** Gate tests: color pickers, eyedropper and live detection lights. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  attemptView,
  averageHex,
  invalidatesBoard,
  noteColorHex,
  weakColor,
  containTransform,
  framePointFromClick,
  PALETTE_ROLES,
  patchOrigin,
  pickVerdict,
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
    { start: "on", goal: "off", bumper: "off", marker: "on" });
  assert.deepEqual(lightStates(samples.recolored_picked_colors.preview),
    { start: "on", goal: "on", bumper: "on", marker: "on" });
  const full = roleLights(samples.full_wall.preview);
  assert.equal(full.marker.text, "4");
  assert.equal(full.start.text, "1/1");
  assert.equal(full.bumper.text, "1");
});

test("lights stay neutral before any preview and warn on wrong counts", () => {
  for (const role of PALETTE_ROLES) {
    assert.deepEqual(roleLights(null)[role], { state: "", count: null, text: "—" });
  }
  const preview = { roles: { start: { count: 2 }, goal: { count: 0 }, bumper: { count: 3 }, marker: { count: 3 } } };
  assert.deepEqual(lightStates(preview), { start: "warn", goal: "off", bumper: "on", marker: "on" });
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
  assert.match(operator, /pickRole = null;\s*pickColor\(role, point\);/);
  assert.match(operator, /const hex = sampleHex\(point\);\s*clearNotice\(\);\s*await setColor\(role, hex\);/);
  // The eyedropper reads the robust note color under the play light: the
  // projector shows its theme background only (contract 2.2), not black.
  assert.match(operator, /return noteColorHex\(ctx\.getImageData\(origin\.x, origin\.y, SAMPLE_SIZE, SAMPLE_SIZE\)\.data\);/);
  assert.match(operator, /const darken = projectorReady && attemptPhase\(state\?\.game\) !== "running";/);
  // Owned as soon as the server went black: a lost acknowledgement still restores it.
  assert.match(operator, /if \(darken\) \{[\s\S]*?applyState\(await action\("projection_neutral"\)\);\s*black = true;\s*await waitForNeutral\(\);\s*await sleep\(scanSettleMs\(cameraFixed\)\);/);
  assert.doesNotMatch(operator.slice(operator.indexOf("async function pickColor"), operator.indexOf("function togglePick")), /projection_black|waitForBlackout/);
  assert.match(operator, /if \(black\) \{\s*await restoreProjection\(\);\s*channel\.postMessage\(\{ type: "resume" \}\);\s*\}\s*pickBusy = false;/);
  // Pale picks are flagged; no picking while the camera search runs.
  assert.match(operator, /if \(weakColor\(hex\)\) \{/);
  assert.match(operator, /\$\(`pick-\$\{role\}`\)\.disabled = !editable \|\| !cameraReady \|\| Boolean\(tuning\) \|\| scanBusy \|\| pickBusy \|\| aligning;/);
  // A pick during a scan would restore the projection mid-scan, so it waits.
  assert.match(operator, /if \(pickRole && \(!editable \|\| !cameraReady \|\| tuning \|\| scanBusy \|\| aligning\)\) pickRole = null;/);
  assert.match(operator, /if \(!pickRole \|\| pickBusy \|\| scanBusy \|\| tuning \|\| aligning \|\| !stream \|\| !video\.videoWidth\) return;/);
  // While a pick holds the projector black, the buttons that would also use it wait.
  assert.match(operator, /\$\("scan-button"\)\.disabled = aligning \|\| scanBusy \|\| Boolean\(tuning\) \|\| pickBusy \|\| !controls\.scan;/);
  assert.doesNotMatch(operator, /calibrate-button/);
  assert.match(operator, /\$\("camera-tune"\)\.disabled = aligning \|\| !cameraReady \|\| Boolean\(tuning\) \|\| scanBusy \|\| pickBusy \|\|/);
  assert.match(operator, /api\("\/api\/preview"/);
  assert.doesNotMatch(operator, /tuning \|\| document\.hidden/);
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
    ...Object.values(matrix.phases).flatMap(({ state }) => [state.message]),
    projectorPage,
    projector,
  ];
  for (const text of texts) assert.doesNotMatch(text, /fucsia|amarill|naranja|verde/i, text.slice(0, 80));
});

test("the eyedropper ignores pale wall pixels at a note's edge", () => {
  const note = [230, 60, 150];   // a magenta note
  const wall = [205, 200, 198];  // a pale wall
  const patch = (noteShare, total = 121) => {
    const data = [];
    for (let i = 0; i < total; i += 1) data.push(...(i < Math.round(total * noteShare) ? note : wall), 255);
    return data;
  };
  assert.equal(noteColorHex(patch(1)), "#e63c96");
  // Half wall, half note: a plain average washes it out to a pale pink.
  assert.equal(averageHex(patch(0.5)), "#da81ae");
  assert.equal(noteColorHex(patch(0.55)), "#e63c96");
  // A click near the note's edge: only a third of the patch is note, still its color.
  assert.equal(noteColorHex(patch(0.35)), "#e63c96");
  // All wall: the result stays wall-like, and the server refuses it as gray.
  assert.equal(noteColorHex(patch(0)), "#cdc8c6");
  assert.throws(() => noteColorHex([]), TypeError);
});

test("pale picks are flagged; vivid ones are not", () => {
  assert.equal(weakColor("#9e9c73"), true);   // the bumper color that matched wood and skin
  assert.equal(weakColor("#cdc8c6"), true);
  assert.equal(weakColor("#000000"), true);
  assert.equal(weakColor("#e63c96"), false);
  assert.equal(weakColor("#2d8de6"), false);
});

test("only a board from a different camera is discarded", () => {
  assert.equal(invalidatesBoard("HD Pro Webcam C920 (046d:082d)", "HD Pro Webcam C920 (046d:082d)"), false);
  assert.equal(invalidatesBoard("HD Pro Webcam C920 (046d:082d)", "Cámara de iPhone de Tobias"), true);
  assert.equal(invalidatesBoard(null, "Cámara de iPhone de Tobias"), false);  // nothing scanned yet
  assert.equal(invalidatesBoard("C920", ""), false);                          // label not known yet
});

test("after a color change the panel says what the next preview lost", () => {
  const seen = (start, goal, bumper, marker) => ({ roles: {
    start: { count: start }, goal: { count: goal }, bumper: { count: bumper }, marker: { count: marker } } });
  const full = seen(1, 1, 1, 4);
  assert.equal(pickVerdict("bumper", "#e6d817", full, full), null);
  assert.equal(pickVerdict("bumper", "#e6d817", null, seen(1, 1, 3, 4)), null);  // any bumper count is fine
  assert.match(pickVerdict("bumper", "#d2bf73", full, seen(1, 1, 0, 4)),
    /^Con #d2bf73 no veo ningún post-it de rebotes\. Si ya está en la pared, tocá su centro/);
  assert.match(pickVerdict("start", "#e05a9b", full, seen(2, 1, 1, 4)),
    /^Con #e05a9b veo 2 post-its de inicio y van 1: el color se parece al de otro post-it o a la pared\.$/);
  // The picked role is fine but it stole another role's notes.
  assert.match(pickVerdict("goal", "#e0525a", full, seen(0, 1, 1, 4)),
    /^Con #e0525a para llegada dejé de ver bien inicio: los colores se parecen\./);
  assert.match(pickVerdict("goal", "#e0525a", full, seen(0, 1, 1, 3)), /dejé de ver bien inicio:/);
  // Only what got worse: a role already off before is not blamed on this pick.
  assert.equal(pickVerdict("goal", "#e0525a", seen(0, 1, 1, 4), seen(0, 1, 1, 4)), null);
  // No preview yet: nothing to judge.
  assert.equal(pickVerdict("goal", "#e0525a", full, null), null);
});

test("the verdict comes from the first preview sent after the color was set", () => {
  assert.match(operator, /const before = freshPreview\(\);\s*applyState\(await action\("palette_set", \{ role, hex \}\)\);\s*pickCheck = \{ role, hex, before, at: performance\.now\(\) \};/);
  assert.match(operator, /const sentAt = performance\.now\(\);\s*try \{\s*const response = await api\("\/api\/preview"/);
  assert.match(operator, /if \(pickCheck && sentAt > pickCheck\.at\) \{\s*const message = pickVerdict\(pickCheck\.role, pickCheck\.hex, pickCheck\.before, preview\);\s*if \(message\) paletteError = \{ message, at: performance\.now\(\) \};\s*pickCheck = null;/);
});
