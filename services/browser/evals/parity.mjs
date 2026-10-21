/** Evaluate every synthetic frame, refinement, alignment decode and state transition against Python, threshold 100%. */
import {
  alignmentVerdicts, bridgeVerdicts, controllerVerdicts, homographyVerdicts, offsetVerdicts, paletteVerdicts,
  physicsVerdicts, probeVerdicts, refineVerdicts, visionVerdicts,
} from "../tests/cases.mjs";
import { loadCv, loadGolden } from "../tests/golden.mjs";

const golden = loadGolden();
const cv = await loadCv();
const cases = [
  ...paletteVerdicts(golden),
  ...homographyVerdicts(cv, golden),
  ...physicsVerdicts(golden),
  ...golden.vision.flatMap((frame) => visionVerdicts(cv, golden, frame)),
  ...refineVerdicts(golden),
  ...bridgeVerdicts(cv, golden),
  ...offsetVerdicts(golden),
  ...alignmentVerdicts(cv, golden),
  ...probeVerdicts(golden),
  ...Object.keys(golden.controller).flatMap((name) => controllerVerdicts(cv, golden, name)),
];
const failures = cases.filter((result) => result.problems.length);
console.log(JSON.stringify({ event: "browser_parity_eval", cases: cases.length,
  passed: cases.length - failures.length, failed: failures.length,
  pass_rate: (cases.length - failures.length) / cases.length, threshold: 1,
  failures: failures.map(({ name, problems }) => ({ name, problems: problems.slice(0, 5) })) }));
if (failures.length) process.exitCode = 1;
