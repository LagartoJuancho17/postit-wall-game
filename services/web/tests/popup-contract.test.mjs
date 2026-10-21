/** Browser interaction contract without opening a real camera or projector. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

import { isProjectorShortcut, SETUP_STAGES } from "../ui/ui-core.mjs";

const read = (name) => readFileSync(new URL(`../ui/${name}`, import.meta.url), "utf8");
const operator = read("app.js");
const projector = read("projector.js");
const operatorPage = read("index.html");
const projectorPage = read("projector.html");

function idsIn(html) {
  return new Set([...html.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1]));
}

test("every element the scripts look up exists in its page", () => {
  // A renamed id makes $() return null and the whole panel dies on load.
  const operatorIds = [...operator.matchAll(/\$\(["']([^"']+)["']\)/g)].map((m) => m[1]);
  const projectorIds = [...projector.matchAll(/getElementById\(["']([^"']+)["']\)/g)].map((m) => m[1]);
  assert.ok(operatorIds.length > 20);
  assert.deepEqual(new Set(projectorIds), new Set(["projector-canvas", "fullscreen-button", "projector-toast", "projector-state"]));
  assert.doesNotMatch(operatorPage + projectorPage, /calibrat|confirm-target/i);
  const operatorPageIds = idsIn(operatorPage);
  const projectorPageIds = idsIn(projectorPage);
  for (const id of operatorIds) assert.ok(operatorPageIds.has(id), `index.html lacks #${id}`);
  for (const id of projectorIds) assert.ok(projectorPageIds.has(id), `projector.html lacks #${id}`);
});

test("the next setup step is shown above the buttons it unblocks", () => {
  const steps = [...operatorPage.matchAll(/data-step="([^"]+)"/g)].map((match) => match[1]);
  assert.deepEqual(steps, SETUP_STAGES);
  assert.match(operator, /const guide = setupGuide\(state, cameraReady, projectionReady\);/);
  assert.match(operator, /const controls = controlsFor\(state, cameraReady, projectionReady\);/);
  assert.match(operator, /\$\(["']setup-hint["']\)\.textContent = guide\.message;/);
  assert.match(operator, /\$\(["']open-projector["']\)\.dataset\.attention = String\(guide\.step === ["']projector["']\)/);
  assert.match(operator, /\.step-hint \[data-step\]/);
  assert.match(operator, /\$\(["']status-message["']\)\.textContent = statusText\(localNotice, state, guide\);/);
});

test("a paused attempt shows a working resume button", () => {
  assert.match(operatorPage, /<button id="resume-button"[^>]*\shidden[\s>]/);
  assert.match(operator, /\$\(["']resume-button["']\)\.hidden\s*=\s*!controls\.resume;/);
  assert.match(operator, /\$\(["']resume-button["']\)\.disabled\s*=\s*aligning \|\| !controls\.resume;/);
  assert.match(operator, /\$\(["']resume-button["']\)\.addEventListener\(["']click["'],\s*\(\)\s*=>\s*sendAction\(["']resume["']\)\)/);
  assert.match(operator, /event\.code\s*===\s*["']Space["']\s*&&\s*!\$\(["']resume-button["']\)\.disabled\)\s*\{\s*event\.preventDefault\(\);\s*sendAction\(["']resume["']\)/);
  assert.match(operator, /\$\(["']game-pill["']\)\.dataset\.tone\s*=\s*attempt\.phase/);
});

test("J opens the projector only from an unmodified, non-repeated shortcut", () => {
  const base = { key: "j", repeat: false, altKey: false, ctrlKey: false,
    metaKey: false, target: null };
  assert.equal(isProjectorShortcut(base), true);
  assert.equal(isProjectorShortcut({ ...base, key: "J" }), true);
  for (const patch of [{ key: "k" }, { repeat: true }, { altKey: true },
    { ctrlKey: true }, { metaKey: true }]) {
    assert.equal(isProjectorShortcut({ ...base, ...patch }), false);
  }

  const previousElement = globalThis.Element;
  try {
    globalThis.Element = class {
      closest() { return this; }
    };
    assert.equal(isProjectorShortcut({ ...base, target: new Element() }), false);
  } finally {
    if (previousElement === undefined) delete globalThis.Element;
    else globalThis.Element = previousElement;
  }
});

test("operator keydown directly opens the same projector route as the button", () => {
  assert.match(operator, /const PROJECTOR_URL\s*=\s*["']\/projector["']/);
  assert.match(operator, /function openProjector\(\)\s*\{/);
  assert.match(operator, /window\.open\(PROJECTOR_URL,/);
  assert.match(operator, /["']open-projector["']\)\.addEventListener\(["']click["'],\s*openProjector\)/);
  assert.match(operator, /document\.addEventListener\(["']keydown["'],\s*\(event\)\s*=>\s*\{\s*if\s*\(isProjectorShortcut\(event\)\)\s*\{\s*event\.preventDefault\(\);\s*openProjector\(\)/);
});

test("projector reports its size and answers every capture scene after drawing it", () => {
  assert.match(projector, /action\("projection_open", \{ width: viewport\.width, height: viewport\.height \}\)/);
  assert.match(projector, /async function ackAfterDraw\(type, id\) \{\s*draw\(\);\s*await new Promise\(\(resolve\) => requestAnimationFrame\(\(\) => requestAnimationFrame\(resolve\)\)\);/);
  for (const [request, ack] of [["blackout", "blackout_ack"], ["align_pattern", "align_ack"],
    ["neutral", "neutral_ack"], ["overlay_test", "overlay_test_ack"]]) {
    assert.match(projector, new RegExp(`data\\.type === "${request}"[\\s\\S]*?await ackAfterDraw\\("${ack}", data\\.id\\)`), request);
    if (ack !== "blackout_ack") assert.match(operator, new RegExp(`"${ack}"`), `panel waits for ${ack}`);
  }
  // Colors and the camera search sample under the play light, not black (contract 2.2).
  assert.match(operator, /await waitForNeutral\(\)/);
  const scan = operator.slice(operator.indexOf("async function scanBoard()"), operator.indexOf("async function sendAction"));
  assert.doesNotMatch(scan, /projection_black|waitForBlackout/);
});

test("a different camera discards the old alignment; a reload with the same one keeps it", () => {
  // Regression: the startup hop (default camera -> preferred one) used to send
  // camera_changed, so reloading the page wiped scan and calibration.
  assert.doesNotMatch(operator, /previousId/);
  assert.match(operator, /if \(invalidatesBoard\(latest\?\.camera\?\.label, label\)\) applyState\(await action\("camera_changed"\)\);/);
  assert.match(operator, /"X-Postit-Camera": encodeURIComponent\(currentTrack\(\)\?\.label \|\| ""\)/);
});

test("the server-unreachable notice clears once the server answers again", () => {
  assert.match(operator, /if \(connectionNotice && localNotice === connectionNotice\) clearNotice\(\);/);
  assert.match(operator, /notice\(`No se puede conectar con el servidor local: \$\{error\.message\}`, "error"\);\s*connectionNotice = localNotice;/);
});

// A named popup (contract 3): a reloaded panel reuses the open projector
// instead of opening a second one, and Chrome gives it its own window to
// move to the projector screen. A projector left as a background tab is
// throttled (no animation frames, ~1 timer per second), so it reports
// visibility and the panel warns.
test("the projector opens as a named popup window", () => {
  const calls = [];
  const source = operator.slice(operator.indexOf("function openProjector()"), operator.indexOf("function waitForNeutral()"));
  const context = vm.createContext({ window: { open: (...args) => {
    calls.push(args);
    return { closed: false, focus() {} };
  } }, notice() {} });
  vm.runInContext(`const PROJECTOR_URL = "/projector"; let popup = null, popupReady = false; ${source}; openProjector();`, context);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/projector");
  assert.equal(calls[0][1], "postit-wall-projector", "reuse the projector after a panel reload");
  assert.equal(calls[0][2], "popup=yes,width=1280,height=720", "its own window, movable to the projector");
  assert.match(projector, /document\.addEventListener\("visibilitychange", \(\) => \{\s*channel\.postMessage\(\{ type: "visibility", hidden: document\.hidden \}\);/);
  assert.match(operator, /La ventana de proyección está oculta: movela a su propia ventana en el proyector\./);
});

test("an alignment run owns the camera and the projector until it ends (review, 2026-10-21)", () => {
  const keys = operator.slice(operator.indexOf('document.addEventListener("keydown"'));
  assert.match(keys, /if \(aligning\) return; \/\/ S, A, R, Space and the arrows wait for the run to end/);
  assert.match(keys, /if \(!\(aligning \|\| tuning \|\| pickBusy\)\) setProjectorTheme/);
  for (const id of ["launch-button", "reset-button", "aim-left", "aim-right", "angle-range"]) {
    assert.match(operator, new RegExp(`\\$\\("${id}"\\)\\.disabled = aligning \\|\\| `), id);
  }
  assert.match(operator, /\$\("theme-dark"\)\.disabled = lightLocked;/);
  assert.match(operator, /if \(aligning\) cameraSelect\.disabled = true;/);
  assert.match(operator, /async function pickColor\(role, point\) \{\n  if \(aligning\) return;/);
  // Leaving mid-run cancels it; a run left by an earlier page is cancelled on load.
  const leave = operator.slice(operator.indexOf('window.addEventListener("pagehide"'));
  assert.match(leave, /keepalive: true[\s\S]*action: "align_cancel"/);
  assert.match(operator, /next\?\.alignment\?\.status === "collecting" && !aligning[\s\S]{0,120}align_cancel/);
  // The projector keeps its light while the camera captures it.
  assert.match(projector, /if \(!localScene && !blackoutForced\) setTheme/);
});
