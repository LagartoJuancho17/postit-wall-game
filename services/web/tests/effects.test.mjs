/** Gate tests: the attempt's colored effects (effects.mjs) and when the projector may draw them. */
import assert from "node:assert/strict";
import test from "node:test";

import {
  drawGoalEffect, drawLaunchEffect, effectAges, GOAL_EFFECT_MS, GOAL_PARTICLES, LAUNCH_EFFECT_MS, noise,
  PARTY_COLORS, TRAIL_LENGTH, trackEffects,
} from "../ui/effects.mjs";
import { drawProjectorScene, NEUTRAL } from "../ui/live-view.mjs";

/** Records every draw with its color and alpha. */
function canvas() {
  const draws = [];
  let path = [];
  return {
    draws, fillStyle: "#000000", strokeStyle: "#000000", globalAlpha: 1, lineWidth: 1,
    save() {}, restore() {}, beginPath() { path = []; }, closePath() {},
    moveTo(x, y) { path.push({ x, y }); }, lineTo(x, y) { path.push({ x, y }); },
    arc(x, y, r) { path.push({ x, y, r }); },
    fill() { draws.push({ kind: "fill", color: this.fillStyle, alpha: this.globalAlpha, path }); },
    stroke() { draws.push({ kind: "stroke", color: this.strokeStyle, alpha: this.globalAlpha, path }); },
    fillRect() {}, fillText(text) { draws.push({ kind: "text", text, color: this.fillStyle }); },
    clip() {}, rect() {}, drawImage() {},
    createRadialGradient() { return { addColorStop() {} }; },
  };
}

const square = (x, y, s) => [{ x, y }, { x: x + s, y }, { x: x + s, y: y + s }, { x, y: y + s }];
const palette = { roles: { start: { hex: "#e0c431" }, goal: { hex: "#ec813c" }, bumper: { hex: "#44c76f" }, marker: { hex: "#d839b0" } } };
const preview = { width: 1280, height: 800, roles: {
  start: { notes: [{ points: square(100, 100, 60) }] }, goal: { notes: [{ points: square(100, 500, 60) }] },
} };
const scene = (game, effects) => {
  const ctx = canvas();
  drawProjectorScene(ctx, { state: { game: { space: "projector", width: 1280, height: 800, ...game } },
    preview, palette, width: 1280, height: 800, effects });
  return ctx;
};
const isNeutral = (color) => typeof color !== "string" || Object.values(NEUTRAL.dark).includes(color)
  || ["#ffdf53", "#ffa659"].includes(color);   // unaligned rims use only neutrals; these are fallbacks, never used here
const colored = (ctx) => ctx.draws.filter((d) => typeof d.color === "string" && !Object.values(NEUTRAL.dark).includes(d.color));

test("the projector draws colored effects only while the backend says the camera is not read", () => {
  const effects = { launchAgeMs: 100, endAgeMs: null, trail: [{ x: 130, y: 172 }, { x: 130, y: 180 }, { x: 130, y: 200 }],
    launchOrigin: { x: 130, y: 172 }, endOrigin: null };
  const live = scene({ status: "running", position: { x: 130, y: 200 }, camera_paused: true }, effects);
  assert.ok(colored(live).length > 10, "launch rings, sparks and trail in color");
  for (const paused of [false, undefined]) {
    const read = scene({ status: "running", position: { x: 130, y: 200 }, camera_paused: paused }, effects);
    assert.equal(colored(read).length, 0, `camera_paused ${paused}: nothing colored on the wall`);
  }
  const won = scene({ status: "won", position: { x: 130, y: 500 }, camera_paused: true },
    { launchAgeMs: null, endAgeMs: 300, trail: [], launchOrigin: null, endOrigin: { x: 130, y: 500 } });
  assert.ok(colored(won).length > GOAL_PARTICLES / 2, "the goal explodes in color");
  const text = won.draws.find((d) => d.kind === "text");
  assert.ok(PARTY_COLORS.includes(text.color), "the result text cycles party colors");
  const after = scene({ status: "won", position: { x: 130, y: 500 }, camera_paused: false },
    { launchAgeMs: null, endAgeMs: 300, trail: [], launchOrigin: null, endOrigin: { x: 130, y: 500 } });
  assert.equal(colored(after).length, 0, "once the camera reads again, neutral only");
  assert.equal(after.draws.find((d) => d.kind === "text").color, NEUTRAL.dark.ball);
  assert.ok(isNeutral(after.draws[0]?.color));
});

test("the launch effect plays for LAUNCH_EFFECT_MS from the ball's first position, over the start paper", () => {
  const ctx = canvas();
  drawLaunchEffect(ctx, { ageMs: 0, origin: { x: 50, y: 60 }, startPaper: square(20, 0, 60), color: "#e0c431" });
  const flash = ctx.draws[0];
  assert.equal(flash.color, "#ffffff");
  assert.equal(flash.path.length, 4, "the flash covers the start paper");
  assert.ok(ctx.draws.some((d) => d.kind === "stroke" && d.color === "#e0c431" && d.path[0].x === 50 && d.path[0].y === 60));
  for (const age of [-1, LAUNCH_EFFECT_MS, LAUNCH_EFFECT_MS + 1]) {
    const late = canvas();
    drawLaunchEffect(late, { ageMs: age, origin: { x: 50, y: 60 } });
    assert.equal(late.draws.length, 0, `age ${age}`);
  }
});

test("the goal explosion is the same at the same instant, bursts outward, falls and ends", () => {
  const draw = (ageMs) => {
    const ctx = canvas();
    drawGoalEffect(ctx, { ageMs, goalPaper: square(100, 100, 60), colors: ["#e0c431", "#ec813c"] });
    return ctx.draws.filter((d) => d.kind === "fill" && d.path.length === 1);
  };
  assert.deepEqual(draw(400), draw(400), "deterministic");
  const spread = (dots) => Math.max(...dots.map((d) => Math.hypot(d.path[0].x - 130, d.path[0].y - 130)));
  assert.ok(spread(draw(600)) > spread(draw(100)), "outward");
  const meanY = (dots) => dots.reduce((sum, d) => sum + d.path[0].y, 0) / dots.length;
  assert.ok(meanY(draw(1500)) > meanY(draw(300)), "it falls");
  assert.equal(canvas().draws.length, 0);
  const done = canvas();
  drawGoalEffect(done, { ageMs: GOAL_EFFECT_MS, goalPaper: square(100, 100, 60) });
  assert.equal(done.draws.length, 0);
  assert.ok(GOAL_EFFECT_MS < 2500, "it ends before the backend reads the camera again (END_EFFECT_S)");
  for (let i = 0; i < 50; i += 1) assert.ok(noise(i) >= 0 && noise(i) < 1);
});

test("trackEffects times the drop and the end once per attempt and keeps a bounded trail", () => {
  let tracked = trackEffects(null, { status: "running", attempts: 1, elapsed_s: 0.1, position: { x: 1, y: 1 } }, 1000);
  assert.equal(tracked.launchAt, 900, "first seen 0.1 s into the drop");
  for (let k = 0; k < 40; k += 1) {
    tracked = trackEffects(tracked, { status: "running", attempts: 1, elapsed_s: 0.1 + k / 30, position: { x: 1, y: 2 + k } }, 1033 + k);
  }
  assert.equal(tracked.launchAt, 900, "kept");
  assert.equal(tracked.trail.length, TRAIL_LENGTH);
  tracked = trackEffects(tracked, { status: "won", attempts: 1, elapsed_s: 2, position: { x: 1, y: 50 } }, 5000);
  assert.equal(tracked.endAt, 5000);
  tracked = trackEffects(tracked, { status: "won", attempts: 1, elapsed_s: 2, position: { x: 1, y: 50 } }, 5200);
  assert.equal(tracked.endAt, 5000, "the end is timed once");
  const ages = effectAges(tracked, 5300);
  assert.deepEqual([ages.launchAgeMs, ages.endAgeMs, ages.trail.length], [null, 300, 0]);
  assert.deepEqual(ages.endOrigin, tracked.trail.at(-1));
  const next = trackEffects(tracked, { status: "running", attempts: 2, elapsed_s: 0, position: { x: 9, y: 9 } }, 9000);
  assert.deepEqual([next.launchAt, next.endAt, next.trail.length], [9000, null, 1], "a new attempt starts over");
  assert.equal(effectAges(next, 9100).launchAgeMs, 100);
  assert.equal(trackEffects(next, { status: "ready", attempts: 2, position: { x: 9, y: 9 } }, 9500).attempt, null);
  assert.equal(effectAges(null, 0), null);
});
