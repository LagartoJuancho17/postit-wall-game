/**
 * Gate test: the JS port answers exactly what the Python services answer.
 *
 * Every case comes from services/browser/parity/export.py, which runs the
 * real Python code. Numbers must agree to 1e-6 (relative above 1), messages
 * and statuses exactly. A failure names the case and the first differences.
 * The gate checks five representative frames; the eval checks all of them.
 */

import assert from "node:assert/strict";
import test from "node:test";

import {
  controllerVerdicts, homographyVerdicts, paletteVerdicts, physicsVerdicts, visionVerdicts,
} from "./cases.mjs";
import { loadCv, loadGolden } from "./golden.mjs";

const golden = loadGolden();
const cv = await loadCv();

/** One frame per kind of answer: playable, refused, picked colors, skew + noise, too small. */
const GATE_FRAMES = new Set(["wall_430/default", "recolored/default", "recolored/picked",
  "scene_skew_dim_speckles/picked", "tiny/default"]);

function allAgree(verdicts) {
  const failing = verdicts.filter((v) => v.problems.length);
  assert.deepEqual(failing.slice(0, 3).map((v) => `${v.name}: ${v.problems.slice(0, 4).join("; ")}`), []);
  assert.ok(verdicts.length > 0, "no cases");
}

test("hex → HSV, picked-color rules and palettes match Python, refusals included", () => {
  assert.ok(golden.palette.builds.some((c) => !c.ok), "the golden set covers refusals");
  allAgree(paletteVerdicts(golden));
});

test("homographies and mapped points match OpenCV in Python", () => {
  allAgree(homographyVerdicts(cv, golden));
});

test("ball trajectories match the Python engine step by step", () => {
  allAgree(physicsVerdicts(golden));
});

test("note detection, board and frame quality match Python (gate frames)", () => {
  const cases = golden.vision.filter((c) => GATE_FRAMES.has(`${c.frame}/${c.palette}`));
  assert.equal(cases.length, GATE_FRAMES.size, "every gate frame exists in the golden set");
  allAgree(cases.flatMap((c) => visionVerdicts(cv, golden, c)));
});

for (const name of Object.keys(golden.controller)) {
  test(`controller script "${name}" matches the Python GameController step by step`, () => {
    allAgree(controllerVerdicts(cv, golden, name));
  });
}

test("the golden set reaches every phase and both endings", () => {
  const phases = new Set();
  for (const script of Object.values(golden.controller)) {
    for (const result of script.results) phases.add((result.value ?? result.state)?.phase);
  }
  for (const phase of ["waiting_camera", "ready", "running", "paused", "won", "lost"]) {
    assert.ok(phases.has(phase), `no golden step reaches ${phase}`);
  }
});
