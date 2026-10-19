/** Browser interaction contract without opening a real camera or projector. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

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
  assert.ok(operatorIds.length > 20 && projectorIds.length > 5);
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
  assert.match(operator, /\$\(["']resume-button["']\)\.disabled\s*=\s*!controls\.resume;/);
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

test("projector reports its size and acknowledges black before frame upload", () => {
  assert.match(projector, /action\(["']projection_open["'],\s*viewport\)/);
  assert.match(projector, /data\.type\s*===\s*["']blackout["'][\s\S]*?blackoutForced\s*=\s*true;[\s\S]*?requestAnimationFrame\([\s\S]*?blackout_ack/);
  assert.match(operator, /action\(["']projection_black["']\)[\s\S]*?await waitForBlackout\(\)[\s\S]*?jpegFromVideo\(\)[\s\S]*?api\(["']\/api\/frame["']/);
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
