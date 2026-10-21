/**
 * How each golden case is answered by the JS port. Shared by the gate test
 * (parity.test.mjs) and the eval (../evals/parity.mjs), so both judge the
 * same thing. Each function returns `{ name, problems }` per case.
 */

import { alignmentCode, alignmentLayout, decodeAlignment, patternDots } from "../alignment.mjs";
import { estimateHomography, offsetConvexPolygon, transformPoint } from "../calibration.mjs";
import { GameController, placeProbes, RequestError } from "../controller.mjs";
import { pyRound } from "../numbers.mjs";
import { buildPalette, DEFAULT_PALETTE, hsvFromHex, paletteJson, ruleFromHex } from "../palette.mjs";
import { launch, step } from "../physics.mjs";
import { detectBoard, findNotes, frameQuality, grayFeature, refineQuad } from "../vision.mjs";
import { boardFromGolden, boardToGolden, diff, frameMat, grayFrame, grayMat, points } from "./golden.mjs";

export function attempt(fn) {
  try {
    return { ok: true, value: fn() };
  } catch (error) {
    if (!(error instanceof Error)) throw error;
    return { ok: false, error: error.message };
  }
}

const expected = (c) => (c.ok ? { ok: true, value: c.value } : { ok: false, error: c.error });
const verdict = (name, got, want, tolerance = 1e-6) => ({ name, problems: diff(got, want, tolerance) });

export function paletteVerdicts(golden) {
  return [
    ...golden.palette.hsv.map((c) => verdict(`hsv ${c.hex}`, attempt(() => hsvFromHex(c.hex)), expected(c), 0)),
    ...golden.palette.rules.map((c) => verdict(`rule ${c.hex}`, attempt(() => ({ ...ruleFromHex(c.hex) })), expected(c), 0)),
    ...golden.palette.builds.map((c, i) => verdict(`build ${i} ${JSON.stringify(c.choices)}`,
      attempt(() => paletteJson(buildPalette(c.choices, c.changed))), expected(c), 0)),
    verdict("default palette", paletteJson(buildPalette({})), golden.default_palette, 0),
  ];
}

export function homographyVerdicts(cv, golden) {
  return golden.homography.flatMap((c, i) => {
    const got = attempt(() => estimateHomography(cv, points(c.camera), points(c.projector)));
    const out = [verdict(`homography ${i}`, got, expected(c), 1e-7)];
    if (c.ok && got.ok) {
      const mapped = c.camera.map(([x, y]) => {
        const p = transformPoint({ x: x + 7.5, y: y - 3.25 }, got.value);
        return [p.x, p.y];
      });
      out.push(verdict(`mapped ${i}`, mapped, c.mapped, 1e-7));
    }
    return out;
  });
}

export function physicsVerdicts(golden) {
  return golden.physics.map((c, i) => {
    const board = boardFromGolden(c.board);
    let state;
    try {
      state = launch(board, c.angle, c.speed, c.gravity ?? 0);
    } catch (error) {
      return verdict(`physics ${i} refusal`, { ok: false, error: error.message }, expected(c));
    }
    if (!c.ok) return { name: `physics ${i}`, problems: [`Python refused (${c.error}) but JS launched`] };
    const trace = [];
    for (let k = 0; k < 900; k += 1) {
      state = step(board, state, c.dt);
      if (k % 15 === 0 || state.status !== "running") {
        trace.push({ i: k, x: state.position.x, y: state.position.y, vx: state.velocity.x, vy: state.velocity.y,
          t: state.elapsed_s, bounces: state.bounce_count, status: state.status });
      }
      if (state.status !== "running") break;
    }
    return verdict(`physics ${i}`, trace, c.trace);
  });
}

const polygons = (notes) => Object.fromEntries(Object.entries(notes).map(([role, list]) =>
  [role, list.map((polygon) => polygon.map((p) => [p.x, p.y]))]));

export function visionVerdicts(cv, golden, c) {
  const name = `${c.frame}/${c.palette}`;
  if (c.palette_error) return [verdict(`${name} palette`, attempt(() => buildPalette(c.choices)).error, c.palette_error)];
  const palette = buildPalette(c.choices);
  const frame = frameMat(cv, golden, c.frame);
  try {
    return [
      verdict(`${name} notes`, attempt(() => polygons(findNotes(cv, frame, palette))), expected(c.notes)),
      verdict(`${name} board`, attempt(() => boardToGolden(detectBoard(cv, frame, palette))), expected(c.board)),
      verdict(`${name} quality`, attempt(() => frameQuality(cv, frame, palette)), expected(c.quality)),
    ];
  } finally {
    frame.delete();
  }
}

/** Contract 2.4: refined corners within 1e-9 px of Python's, outcomes exact. */
export function refineVerdicts(golden) {
  const features = new Map();
  return golden.refine.map((c, i) => {
    if (!features.has(c.feature)) features.set(c.feature, grayFeature(grayFrame(golden, c.feature)));
    const [quad, outcome] = refineQuad(features.get(c.feature), c.coarse);
    return verdict(`refine ${i} ${c.feature}`, { quad, outcome }, { quad: c.quad, outcome: c.outcome }, 1e-9);
  });
}

/** Contract 2.5: notes with and without our own rim's bridge. */
export function bridgeVerdicts(cv, golden) {
  return golden.bridge.flatMap((c) => {
    const frame = frameMat(cv, golden, c.frame);
    const bridge = grayMat(cv, golden, c.bridge);
    try {
      return [
        verdict(`${c.frame} with bridge`, attempt(() => polygons(findNotes(cv, frame, DEFAULT_PALETTE, bridge))),
          expected(c.with)),
        verdict(`${c.frame} without`, attempt(() => polygons(findNotes(cv, frame, DEFAULT_PALETTE))),
          expected(c.without)),
      ];
    } finally {
      frame.delete();
      bridge.delete();
    }
  });
}

export function offsetVerdicts(golden) {
  return golden.offset.map((c, i) => {
    const got = offsetConvexPolygon(c.points.map(([x, y]) => ({ x, y })), c.distance);
    return verdict(`offset ${i}`, got && got.map((p) => [p.x, p.y]), c.result, 1e-9);
  });
}

/** Contract 1.1-1.3: codes, layouts, pattern frames and full decodes. */
export function alignmentVerdicts(cv, golden) {
  const { codes, layouts, decodes } = golden.alignment;
  const out = [verdict("codes", codes.map((_, id) => alignmentCode(id)), codes, 0)];
  for (const c of layouts) {
    const layout = alignmentLayout(...c.size);
    out.push(verdict(`layout ${c.size.join("x")}`, layout, c.layout, 0));
    const dots = Object.fromEntries(Object.keys(c.dots).map((key) => {
      const [bit, inverse] = key.split(":").map(Number);
      return [key, patternDots(layout, bit, inverse === 1)];
    }));
    out.push(verdict(`pattern dots ${c.size.join("x")}`, dots, c.dots, 0));
  }
  for (const c of decodes) {
    const frames = new Map(Object.entries(c.frames).map(([key, name]) => [key, grayFrame(golden, name)]));
    const result = decodeAlignment(cv, frames, alignmentLayout(...c.size));
    out.push(verdict(`decode ${c.case}`, result, c.result, 1e-6));
  }
  return out;
}

export function probeVerdicts(golden) {
  return golden.probes.map((c, i) => verdict(`probes ${i} ${c.frame} ${c.projector.join("x")}`,
    placeProbes(c.detection, c.matrix, c.projector, c.camera), c.probes, 1e-9));
}

/** The eyedropper of services/browser/parity/export.py `_sample`. */
const FRAME_CENTERS = { start: [180, 350], goal: [780, 350], bumper: [430, 235], marker: [90, 90] };
export function sampleHex(frame, role) {
  const [x, y] = FRAME_CENTERS[role];
  const sums = [0, 0, 0];
  for (let row = y - 2; row <= y + 2; row += 1) {
    for (let col = x - 2; col <= x + 2; col += 1) {
      for (let channel = 0; channel < 3; channel += 1) sums[channel] += frame.data[(row * frame.cols + col) * 3 + channel];
    }
  }
  const [blue, green, red] = sums.map((sum) => pyRound(sum / 25));
  return `#${[red, green, blue].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function snapshotOf(value) {
  const clean = JSON.parse(JSON.stringify(value));
  for (const entry of Object.values(clean.camera_presets ?? {})) delete entry.saved_at;
  return clean;
}

/** Run a golden controller script on a fresh JS controller with a fake clock. */
export function replay(cv, golden, steps) {
  let now = 0;
  const controller = new GameController({ clock: () => now, storage: null });
  controller.cv = cv;
  const frames = new Map();
  const frame = (name) => {
    if (!frames.has(name)) frames.set(name, frameMat(cv, golden, name));
    return frames.get(name);
  };
  const results = [];
  try {
    for (const entry of steps) {
      try {
        let value;
        if (entry.op === "action") value = controller.action(entry.payload);
        else if (entry.op === "scan") value = controller.scan(frame(entry.frame), entry.camera);
        else if (entry.op === "preview") value = controller.preview(frame(entry.frame), entry.camera ?? null);
        else if (entry.op === "align_frame") {
          value = controller.alignFrame(frame(entry.frame), entry.align, entry.camera ?? null);
        } else if (entry.op === "check_frame") value = controller.checkFrame(frame(entry.frame), entry.kind);
        else if (entry.op === "tick") {
          now += entry.advance;
          value = controller.tick();
        } else if (entry.op === "state") value = controller.snapshot();
        else if (entry.op === "pick") {
          value = controller.action({ action: "palette_set", role: entry.role,
            hex: sampleHex(frame(entry.frame), entry.role) });
        } else throw new Error(`unknown op ${entry.op}`);
        results.push({ ok: true, value: "revision" in value ? snapshotOf(value) : value });
      } catch (error) {
        if (!(error instanceof RequestError)) throw error;
        results.push({ ok: false, status: error.status, error: error.message,
          state: snapshotOf(controller.snapshot()) });
      }
    }
  } finally {
    frames.forEach((mat) => mat.delete());
  }
  return results;
}

export function controllerVerdicts(cv, golden, name) {
  const script = golden.controller[name];
  const results = replay(cv, golden, script.steps);
  return script.results.map((want, i) => verdict(
    `${name} step ${i} ${JSON.stringify(script.steps[i]).slice(0, 90)}`, results[i], want));
}
