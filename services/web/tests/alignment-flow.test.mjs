/**
 * Gate test: the panel's alignment run and overlay check (align-run.mjs,
 * docs/alignment-and-color.md sections 2.3 and 3), driven with fakes for the
 * backend, the projector window, the camera and the clock.
 */

import assert from "node:assert/strict";
import test from "node:test";

import { alignTiming, autoAlignKey, runAlignment, shouldAutoAlign } from "../ui/align-run.mjs";

const FRAMES = Array.from({ length: 7 }, (_, bit) => [{ bit, inverse: false }, { bit, inverse: true }]).flat();
const LAYOUT = { frames: FRAMES, dots: [] };

/** A backend whose align_solve answers from a script; records everything. */
function rig({ solves = [{ status: "ready", code: null }], failAt = null, check = { fill: true } } = {}) {
  const log = [];
  let solve = 0;
  const state = (alignment) => ({ alignment: { overlay: { fill: false, detail: "" }, ...alignment } });
  const deps = {
    action: async (name) => {
      log.push(`action:${name}`);
      if (name === "align_start") return state({ status: "collecting", layout: LAYOUT });
      if (name === "align_solve") return state(solves[Math.min(solve++, solves.length - 1)]);
      if (name === "check_solve") return state({ status: "ready", overlay: { ...check, detail: "x" } });
      return state({ status: "none" });
    },
    show: async (message) => {
      const frame = message.frame === "warmup" ? "warmup"
        : message.frame ? `${message.frame.bit}:${message.frame.inverse ? 1 : 0}` : "";
      log.push(`show:${message.type}${frame ? `:${frame}` : ""}`);
      if (failAt && log.length >= failAt) throw new Error("La ventana de proyección no respondió.");
      return { at: 0 };
    },
    resume: () => log.push("resume"),
    settle: async (ack, ms) => log.push(`settle:${ms}`),
    sleep: async (ms) => log.push(`sleep:${ms}`),
    capture: async () => "jpeg",
    upload: async (path, blob, headers) => log.push(`upload:${path}:${Object.values(headers)[0]}`),
  };
  return { deps, log };
}

test("a run shows the warm-up, then each of the 14 frames, capturing after each settles", async () => {
  const { deps, log } = rig();
  const result = await runAlignment(deps, { cameraFixed: false });
  assert.equal(result.alignment.status, "ready");
  const timing = alignTiming(false);
  assert.deepEqual(log.slice(0, 3), ["action:align_start", "show:align_pattern:warmup", `sleep:${timing.warmup}`]);
  const uploads = log.filter((entry) => entry.startsWith("upload:/api/align_frame"));
  assert.deepEqual(uploads, FRAMES.map((f) => `upload:/api/align_frame:${f.bit}:${f.inverse ? 1 : 0}`));
  // Each capture follows its own pattern's settle: show, settle, upload.
  for (const frame of FRAMES) {
    const key = `${frame.bit}:${frame.inverse ? 1 : 0}`;
    const shown = log.indexOf(`show:align_pattern:${key}`);
    assert.equal(log[shown + 1], `settle:${timing.settle}`, key);
    assert.equal(log[shown + 2], `upload:/api/align_frame:${key}`, key);
  }
  // Resume before solving; the overlay check follows a success.
  const solve = log.indexOf("action:align_solve");
  assert.equal(log[solve - 1], "resume");
  assert.deepEqual(log.slice(solve + 1), [
    "show:neutral", `settle:${timing.settle}`, "upload:/api/check_frame:neutral",
    "show:overlay_test", `settle:${timing.settle}`, "upload:/api/check_frame:overlay",
    "resume", "action:check_solve",
  ]);
});

test("a fixed camera settles faster than an automatic one", () => {
  assert.deepEqual(alignTiming(true), { warmup: 300, settle: 150 });
  assert.deepEqual(alignTiming(false), { warmup: 1000, settle: 350 });
});

test("an inaccurate or inconsistent decode is retried once, slower; then reported", async () => {
  for (const code of ["inaccurate", "inconsistent"]) {
    const { deps, log } = rig({ solves: [{ status: "failed", code }, { status: "failed", code }] });
    const result = await runAlignment(deps, { cameraFixed: true });
    assert.equal(result.alignment.status, "failed", code);
    assert.equal(log.filter((entry) => entry === "action:align_start").length, 2, code);
    assert.ok(log.includes("settle:300"), `${code}: the retry doubles the settle`);
    assert.ok(log.includes("sleep:600"), `${code}: the retry doubles the warm-up`);
    assert.ok(!log.includes("action:check_solve"), code);
  }
  const { deps, log } = rig({ solves: [{ status: "failed", code: "inaccurate" }, { status: "ready", code: null }] });
  assert.equal((await runAlignment(deps, { cameraFixed: true })).alignment.status, "ready");
  assert.ok(log.includes("action:check_solve"), "a successful retry still runs the overlay check");
});

test("other failures are not retried: the camera cannot see enough", async () => {
  for (const code of ["few_points", "low_coverage", "frames"]) {
    const { deps, log } = rig({ solves: [{ status: "failed", code }] });
    const result = await runAlignment(deps);
    assert.equal(result.alignment.code, code);
    assert.equal(log.filter((entry) => entry === "action:align_start").length, 1, code);
  }
});

test("a projector that stops answering cancels the run and still resumes", async () => {
  const { deps, log } = rig({ failAt: 6 });
  await assert.rejects(runAlignment(deps), /no respondió/);
  const failed = log.findIndex((entry) => entry.startsWith("show:")) + 0;
  assert.ok(failed >= 0);
  assert.deepEqual(log.slice(-2), ["resume", "action:align_cancel"]);
  assert.ok(!log.includes("action:align_solve"));
});

test("a failing overlay capture still resumes the projector", async () => {
  const { deps, log } = rig();
  deps.upload = async (path) => {
    log.push(`upload:${path}`);
    if (path === "/api/check_frame") throw new Error("Primero alineá el proyector.");
  };
  await assert.rejects(runAlignment(deps), /Primero alineá/);
  assert.equal(log.at(-1), "resume");
});

const READY = {
  alignment: { status: "none" }, game: { status: "ready", paused: false },
  camera: { width: 1280, height: 720, label: "Cámara de iPhone de Tobias" },
  projection: { width: 1920, height: 1080 },
};

test("automatic alignment starts once per projector size and camera, never after a failure", () => {
  const tried = new Set();
  const ask = (overrides = {}) => shouldAutoAlign({ state: READY, cameraReady: true, projectorReady: true,
    fullscreen: true, busy: false, tried, ...overrides });
  assert.equal(ask(), true);
  for (const blocker of [{ cameraReady: false }, { projectorReady: false }, { fullscreen: false }, { busy: true },
    { state: { ...READY, alignment: { status: "failed" } } },
    { state: { ...READY, alignment: { status: "ready" } } },
    { state: { ...READY, alignment: { status: "collecting" } } },
    { state: { ...READY, game: { status: "running", paused: false } } },
    { state: { ...READY, camera: { width: 0 } } },
    { state: { ...READY, alignment: undefined } }]) {
    assert.equal(ask(blocker), false, JSON.stringify(blocker));
  }
  assert.equal(ask({ state: { ...READY, game: { status: "running", paused: true } } }), true, "a paused attempt can align");
  assert.equal(ask({ state: { ...READY, alignment: { status: "stale" } } }), true);
  tried.add(autoAlignKey(READY));
  assert.equal(ask(), false, "already tried under these exact conditions");
  assert.equal(ask({ state: { ...READY, projection: { width: 1280, height: 720 } } }), true, "a new projector size is new");
  assert.equal(ask({ state: { ...READY, camera: { ...READY.camera, label: "C920" } } }), true, "another camera is new");
});
