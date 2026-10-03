/** Gate tests: fixing white balance, exposure and focus by hand. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  bestTrial,
  cameraDiagnosis,
  cameraSupport,
  describeCamera,
  driftedModes,
  fixedPart,
  lockConstraints,
  mergeSets,
  presetScore,
  refinePlan,
  searchPlan,
  settleMs,
  sharpestTrial,
  snap,
  usbIdsFromLabel,
} from "../ui/ui-core.mjs";

const read = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const c920 = read("../contract/c920-capabilities.json");
const trials = read("../contract/camera-trials.json");
const samples = read("../contract/preview-samples.json").samples;

test("lock needs only a manual mode; search also needs a value range", () => {
  const full = { lock: true, search: true };
  assert.deepEqual(cameraSupport(c920),
    { whiteBalance: full, exposure: full, focus: full, any: true, searchable: true });
  const autoOnly = cameraSupport({ focusMode: ["continuous"], focusDistance: { min: 0, max: 1 } });
  assert.deepEqual([autoOnly.focus, autoOnly.any], [{ lock: false, search: false }, false]);
  // Regression: modes without ranges used to count as "no control at all",
  // so the panel left white balance and focus automatic.
  const modesOnly = cameraSupport({ whiteBalanceMode: ["continuous", "manual"], focusMode: ["manual"] });
  assert.deepEqual([modesOnly.whiteBalance, modesOnly.focus, modesOnly.any, modesOnly.searchable],
    [{ lock: true, search: false }, { lock: true, search: false }, true, false]);
  assert.equal(cameraSupport(null).any, false);
});

test("without ranges the panel still switches the automatic off", () => {
  const modesOnly = { whiteBalanceMode: ["continuous", "manual"], exposureMode: ["continuous", "manual"],
    focusMode: ["continuous", "manual"] };
  assert.deepEqual(lockConstraints({ colorTemperature: 4000, focusDistance: 30 }, modesOnly), [
    { whiteBalanceMode: "manual" }, { exposureMode: "manual" }, { focusMode: "manual" },
  ]);
  const plan = searchPlan(modesOnly);
  assert.deepEqual(plan, { exposureTime: [undefined], colorTemperature: [undefined], focusDistance: [] });
  const fixed = fixedPart({ whiteBalanceMode: "manual", exposureMode: "manual", focusMode: "continuous" }, modesOnly);
  assert.deepEqual(fixed.settings, { whiteBalanceMode: "manual", exposureMode: "manual" });
  assert.deepEqual([fixed.unfixed, fixed.unavailable], [["foco"], []]);
});

test("the diagnosis says what Chrome reported for each control", () => {
  assert.equal(cameraDiagnosis(c920),
    "balance de blancos: modos manual/continuous, 2000–6500 · exposición: modos manual/continuous, 3–2047 · foco: modos manual/continuous, 0–250");
  assert.equal(cameraDiagnosis({}),
    "balance de blancos: modos no informado, sin rango · exposición: modos no informado, sin rango · foco: modos no informado, sin rango");
});

test("snap clamps into the range and onto the step", () => {
  assert.equal(snap(37, c920.focusDistance), 35);
  assert.equal(snap(-10, c920.focusDistance), 0);
  assert.equal(snap(999, c920.focusDistance), 250);
  assert.equal(snap(0.3, { min: 0, max: 1, step: 0.1 }), 0.3);
  assert.equal(snap(4321.4, { min: 2000, max: 6500 }), 4321.4);
});

test("lock constraints are manual, one control per advanced set", () => {
  const sets = lockConstraints({ colorTemperature: 4100, exposureTime: 199.6, focusDistance: 33, brightness: 128 }, c920);
  assert.deepEqual(sets, [
    { whiteBalanceMode: "manual", colorTemperature: 4100 },
    { exposureMode: "manual", exposureTime: 200 },
    { focusMode: "manual", focusDistance: 35 },
    { brightness: 128 },
  ]);
  const noFocus = { ...c920, focusMode: ["continuous"] };
  assert.equal(lockConstraints({ focusDistance: 30 }, noFocus).some((set) => "focusMode" in set), false);
});

test("only what really went manual is saved; the rest is reported", () => {
  const actual = { whiteBalanceMode: "manual", colorTemperature: 4000, exposureMode: "continuous",
    exposureTime: 333, focusMode: "manual", focusDistance: 35, brightness: 128, width: 1280 };
  const { settings, unfixed, unavailable } = fixedPart(actual, c920);
  assert.deepEqual(settings, { whiteBalanceMode: "manual", colorTemperature: 4000,
    focusMode: "manual", focusDistance: 35, brightness: 128 });
  assert.deepEqual(unfixed, ["exposición"]);
  assert.deepEqual(unavailable, []);
  assert.deepEqual(fixedPart(actual, { ...c920, focusMode: ["continuous"] }).unavailable, ["foco"]);
});

test("the search grid for a C920 is the grid the simulated rooms were scored on", () => {
  const plan = searchPlan(c920);
  assert.deepEqual(plan.exposureTime, trials.grid.exposureTime);
  assert.deepEqual(plan.colorTemperature, trials.grid.colorTemperature);
  assert.deepEqual(plan.focusDistance, [0, 40, 85, 125, 165, 210, 250]);
  assert.deepEqual(searchPlan({}), { exposureTime: [undefined], colorTemperature: [undefined], focusDistance: [] });
});

test("refinement tries shorter, longer, cooler and warmer, never the same point twice", () => {
  const best = { exposureTime: 200, colorTemperature: 4000, focusDistance: 35 };
  assert.deepEqual(refinePlan(best, c920), [
    { exposureTime: 150, colorTemperature: 4000, focusDistance: 35 },
    { exposureTime: 266, colorTemperature: 4000, focusDistance: 35 },
    { exposureTime: 200, colorTemperature: 3500, focusDistance: 35 },
    { exposureTime: 200, colorTemperature: 4500, focusDistance: 35 },
  ]);
  const edge = { exposureTime: 3, colorTemperature: 6500 };
  for (const plan of refinePlan(edge, c920)) assert.notDeepEqual(plan, edge);
});

test("a frame the scan accepts always outscores one it refuses", () => {
  const score = (name) => presetScore(samples[name].preview);
  assert.ok(score("full_wall") > score("missing_start"));
  assert.ok(score("full_wall") > score("extra_corner"));
  assert.ok(score("recolored_picked_colors") > score("recolored_default_colors"));
  assert.equal(presetScore({ roles: {} }), -Infinity);
  for (const room of trials.rooms) {
    const ready = room.trials.filter((t) => t.preview.quality.ready).map((t) => presetScore(t.preview));
    const refused = room.trials.filter((t) => !t.preview.quality.ready).map((t) => presetScore(t.preview));
    if (ready.length && refused.length) assert.ok(Math.min(...ready) > Math.max(...refused), room.name);
  }
});

test("clipping costs more than being a little dark", () => {
  const base = { roles: {}, quality: { ready: true, clipped: 0, dark: 0, brightness: 0.62, note_saturation: 200 } };
  const clipped = { ...base, quality: { ...base.quality, clipped: 0.3, brightness: 0.9 } };
  const darkish = { ...base, quality: { ...base.quality, brightness: 0.45 } };
  assert.ok(presetScore(darkish) > presetScore(clipped));
  assert.ok(presetScore(base) > presetScore(darkish));
});

test("glare and black areas cost score even when the wall median looks right", () => {
  // A lamp reflection can clip a patch without moving the median brightness.
  const base = { roles: {}, quality: { ready: true, clipped: 0, dark: 0, brightness: 0.62, note_saturation: 200 } };
  const glare = { ...base, quality: { ...base.quality, clipped: 0.2 } };
  const shadow = { ...base, quality: { ...base.quality, dark: 0.2 } };
  assert.ok(presetScore(glare) < presetScore(base) - 50);
  assert.ok(presetScore(shadow) < presetScore(base) - 20);
});

test("best and sharpest pick the top trial, earliest on ties", () => {
  const a = { score: 5, preview: { quality: { sharpness: 10 } } };
  const b = { score: 9, preview: { quality: { sharpness: 30 } } };
  const c = { score: 9, preview: { quality: { sharpness: 30 } } };
  assert.equal(bestTrial([a, b, c]), b);
  assert.equal(sharpestTrial([a, b, c]), b);
  assert.equal(bestTrial([]), null);
});

test("the panel fixes the camera on every start and saves what the search finds", () => {
  const operator = readFileSync(new URL("../ui/app.js", import.meta.url), "utf8");
  const page = readFileSync(new URL("../ui/index.html", import.meta.url), "utf8");
  assert.match(page, /id="camera-lock"/);
  assert.match(page, /id="camera-tune"/);
  // After a successful start, never before the camera hop settles.
  assert.match(operator, /clearNotice\(\);\s*render\(\);\s*setupCameraLock\(\);\s*\} catch/);
  // A saved preset is applied; without one the search runs and saves.
  assert.match(operator, /const preset = state\.camera_presets\[cameraLabel\];\s*if \(!preset\) \{\s*await tuneCamera\(\);/);
  assert.match(operator, /action\("camera_preset_save", \{ camera: cameraLabel, settings, score: winner\.score,/);
  // Every trial is scored by the backend detector and only fixed values are applied.
  assert.match(operator, /return \{ settings, preview: result, score: presetScore\(result\) \};/);
  assert.match(operator, /advanced: sets \}/);
  // The scan and the regular preview wait while the search runs.
  assert.match(operator, /async function scanBoard\(\) \{\s*if \(scanBusy \|\| tuning\) return;/);
  // The projector goes black for the search, like for a scan, and comes back.
  assert.match(operator, /applyState\(await action\("projection_black"\)\);\s*await waitForBlackout\(\);\s*black = true;/);
  assert.match(operator, /if \(black\) \{\s*await restoreProjection\(\);\s*channel\.postMessage\(\{ type: "resume" \}\);/);
  // A failed scan also brings the projection back instead of leaving it black.
  assert.match(operator, /notice\(error\.message, "error"\);\s*await restoreProjection\(\);/);
  assert.match(operator, /action\("projection_restore"\)/);
  // What Chrome exposes and what really got applied go to the log.
  for (const kind of ["capabilities", "applied", "search"]) assert.match(operator, new RegExp(`reportCamera\\("${kind}"`));
});

test("the USB ids come from the label Chrome gives the camera", () => {
  assert.deepEqual(usbIdsFromLabel("HD Pro Webcam C920 (046d:082d)"), { vendor: "046d", product: "082d" });
  assert.deepEqual(usbIdsFromLabel("Cam (046D:082D)"), { vendor: "046d", product: "082d" });
  assert.equal(usbIdsFromLabel("Cámara FaceTime HD"), null);
  assert.equal(usbIdsFromLabel(undefined), null);
});

test("drift is a manual mode of the preset that is automatic again", () => {
  const preset = { whiteBalanceMode: "manual", exposureMode: "manual", colorTemperature: 4000 };
  assert.deepEqual(driftedModes(preset, { whiteBalanceMode: "continuous", exposureMode: "manual" }),
    ["whiteBalanceMode"]);
  assert.deepEqual(driftedModes(preset, { whiteBalanceMode: "manual", exposureMode: "manual" }), []);
  assert.deepEqual(driftedModes(preset, {}), ["whiteBalanceMode", "exposureMode"]);
  assert.deepEqual(driftedModes({}, { focusMode: "continuous" }), []);
});

test("the USB route receives one manual settings object, gain included", () => {
  const usbCaps = { ...c920, gain: { min: 0, max: 255, step: 1 } };
  const merged = mergeSets(lockConstraints({ colorTemperature: 4100, exposureTime: 200, focusDistance: 33,
    gain: 0 }, usbCaps));
  assert.deepEqual(merged, { whiteBalanceMode: "manual", colorTemperature: 4100, exposureMode: "manual",
    exposureTime: 200, focusMode: "manual", focusDistance: 35, gain: 0 });
});

test("without Chrome controls the panel fixes the camera through the server over USB", () => {
  const operator = readFileSync(new URL("../ui/app.js", import.meta.url), "utf8");
  // Chrome first; the USB route only when Chrome offers no manual mode.
  assert.match(operator, /if \(!cameraSupport\(caps\)\.any\) \{\s*try \{\s*const usb = await usbCamera\(usbIdsFromLabel\(track\.label\)\);/);
  assert.match(operator, /cameraDriver = "usb";/);
  assert.match(operator, /if \(cameraDriver === "usb"\) \{[\s\S]*?fetch\("\/api\/camera", \{\s*method: "POST",[\s\S]*?settings: mergeSets\(sets\)/);
  // The search reads its starting point through the same route.
  assert.match(operator, /const start = await readCameraSettings\(track\);/);
  // A watchdog puts back a preset when the camera returns to automatic.
  assert.match(operator, /setInterval\(watchCamera, WATCH_CAMERA_MS\);/);
  assert.match(operator, /const drifted = driftedModes\(preset\.settings, current\);/);
  assert.match(operator, /reportCamera\("reapplied"/);
});

test("describeCamera and settleMs speak in operator units", () => {
  assert.equal(describeCamera({ colorTemperature: 4000, exposureTime: 200, focusDistance: 35 }), "4000 K · 20 ms · foco 35");
  assert.equal(describeCamera({ exposureTime: 333 }), "33.3 ms");
  assert.equal(describeCamera({}), "");
  assert.equal(settleMs({ exposureTime: 800 }), 490);
  assert.equal(settleMs({}), 250);
});
