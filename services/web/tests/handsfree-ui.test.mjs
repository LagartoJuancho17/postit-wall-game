/** Gate tests: package 1 on the wall and in the panel (contract 3.1). */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  APPEAR_EFFECT_MS,
  APPEAR_FORGET_MS,
  appearAges,
  countdownBeat,
  drawCountdown,
  drawHitEffect,
  effectAges,
  HIT_EFFECT_MS,
  trackAppearances,
  trackEffects,
} from "../ui/effects.mjs";
import { drawProjectorScene, NEUTRAL, RIM_OFFSET_PX } from "../ui/live-view.mjs";
import { countdownBeatOf, playSound, soundEvents } from "../ui/sounds.mjs";
import {
  attemptPhase,
  controlsFor,
  handsFreeLine,
  launchLabel,
  remoteCommand,
  remoteKeyAllowed,
} from "../ui/ui-core.mjs";

const matrix = JSON.parse(readFileSync(new URL("../contract/state-matrix.json", import.meta.url), "utf8"));
const read = (name) => readFileSync(new URL(`../ui/${name}`, import.meta.url), "utf8");

/** Records strokes and fills with their paths, colors and the clips in force. */
function canvas() {
  const draws = [];
  const stack = [];
  let path = [];
  let clips = 0;
  return {
    draws, fillStyle: "#000000", strokeStyle: "#000000", globalAlpha: 1, lineWidth: 1, font: "", textAlign: "",
    textBaseline: "", lineJoin: "", lineCap: "", miterLimit: 10, shadowBlur: 0, shadowColor: "", shadowOffsetX: 0,
    shadowOffsetY: 0, globalCompositeOperation: "", filter: "",
    save() { stack.push({ clips, fillStyle: this.fillStyle, strokeStyle: this.strokeStyle, globalAlpha: this.globalAlpha }); },
    restore() { const top = stack.pop(); clips = top.clips; Object.assign(this, top, { clips: undefined }); },
    beginPath() { path = []; }, closePath() {}, rect() {},
    moveTo(x, y) { path.push({ x, y }); }, lineTo(x, y) { path.push({ x, y }); },
    arc(x, y, r) { path.push({ x, y, r }); },
    clip() { clips += 1; },
    fillRect(x, y, w, h) { draws.push({ kind: "rect", color: this.fillStyle, w, h }); },
    stroke() { draws.push({ kind: "stroke", color: this.strokeStyle, alpha: this.globalAlpha, path, clips }); },
    fill() { draws.push({ kind: "fill", color: this.fillStyle, alpha: this.globalAlpha, path, clips }); },
    fillText(text, x, y) { draws.push({ kind: "text", text, color: this.fillStyle, x, y, font: this.font }); },
    createRadialGradient() { return { addColorStop() {} }; },
  };
}

const square = (cx, cy, half) => ({ points: [{ x: cx - half, y: cy - half }, { x: cx + half, y: cy - half },
  { x: cx + half, y: cy + half }, { x: cx - half, y: cy + half }] });
const DETECTION = {
  width: 960, height: 720,
  roles: { start: { notes: [square(180, 350, 30)] }, goal: { notes: [square(180, 560, 30)] },
    bumper: { notes: [square(430, 235, 30)] }, marker: { notes: [] } },
};
const inside = (p, { points }) => p.x > points[0].x && p.x < points[1].x && p.y > points[0].y && p.y < points[2].y;

test("the countdown is its own phase; after an attempt the button plays again", () => {
  assert.equal(attemptPhase({ status: "ready", countdown_s: 1.2 }), "countdown");
  assert.equal(attemptPhase({ status: "ready", countdown_s: 0 }), "ready");
  assert.deepEqual(["ready", "won", "lost", "countdown"].map(launchLabel),
    ["Soltar pelota", "Jugar otra vez", "Jugar otra vez", "Saliendo…"]);
  const state = matrix.phases.countdown.state;
  assert.equal(attemptPhase(state.game), "countdown");
  assert.deepEqual(Object.entries(controlsFor(state, true, true)).filter(([, on]) => on).map(([name]) => name),
    ["reset"], "R cancels; nothing else may touch the camera or the wall");
});

test("the remote's one button: never a panel shortcut, play or resume by phase", () => {
  for (const code of ["Space", "KeyR", "KeyA", "KeyT", "Escape", "", null]) assert.equal(remoteKeyAllowed(code), false, code);
  for (const code of ["PageDown", "AudioVolumeUp", "ArrowRight", "MediaPlayPause", "KeyK"]) assert.equal(remoteKeyAllowed(code), true);
  assert.deepEqual(["ready", "won", "lost", "paused", "running", "countdown", "none"].map(remoteCommand),
    ["play", "play", "play", "resume", null, null, null]);
});

test("the wall says how to play without hands on the keyboard, and when to lift the hand", () => {
  const on = { enabled: true, armed: false };
  assert.equal(handsFreeLine({ status: "ready", hands_free: on }), "Tapá el inicio con la mano un segundo y sacala para soltar");
  assert.equal(handsFreeLine({ status: "none", hands_free: { enabled: true, armed: true } }), "Sacá la mano: ¡la pelota sale!");
  assert.equal(handsFreeLine({ status: "won", camera_paused: true, hands_free: on }), null, "not during the end effect");
  assert.equal(handsFreeLine({ status: "won", camera_paused: false, hands_free: on }), "Tapá el inicio con la mano un segundo y sacala para soltar");
  assert.equal(handsFreeLine({ status: "running", hands_free: on }), null);
  assert.equal(handsFreeLine({ status: "ready", hands_free: { enabled: false, armed: false } }), null);
});

test("a post-it ripples once when stuck, never for a flicker, and not for what was already there", () => {
  let tracked = trackAppearances(null, null, 0);
  assert.equal(tracked, null, "no detection yet: nothing known");
  tracked = trackAppearances(tracked, DETECTION, 100);
  assert.deepEqual(appearAges(tracked, 100), [], "the first detection is what was already on the wall");
  const added = { ...DETECTION, roles: { ...DETECTION.roles, bumper: { notes: [square(430, 235, 30), square(700, 400, 30)] } } };
  tracked = trackAppearances(tracked, added, 400);
  assert.deepEqual(appearAges(tracked, 500), [{ role: "bumper", x: 700, y: 400, ageMs: 100 }]);
  // It flickers out for a frame and comes back a bit moved: the same note.
  tracked = trackAppearances(tracked, DETECTION, 700);
  tracked = trackAppearances(tracked, { ...added, roles: { ...added.roles, bumper: { notes: [square(430, 235, 30), square(706, 403, 30)] } } }, 900);
  assert.equal(appearAges(tracked, 900).length, 1, "still the first ripple, no second one");
  assert.equal(appearAges(tracked, 400 + APPEAR_EFFECT_MS).length, 0, "it ends");
  // Away longer than APPEAR_FORGET_MS (the others still seen): new again when it comes back.
  let now = 1000;
  for (; now < 1000 + APPEAR_FORGET_MS + 300; now += 250) tracked = trackAppearances(tracked, DETECTION, now);
  tracked = trackAppearances(tracked, added, now);
  assert.deepEqual(appearAges(tracked, now).map((a) => [a.role, a.x]), [["bumper", 700]]);
});

test("the ripple is neutral, starts outside the paper and is clipped off every paper", () => {
  const ctx = canvas();
  const state = { game: { status: "ready", hands_free: { enabled: false } }, camera: { width: 960, height: 720 } };
  drawProjectorScene(ctx, { state, preview: DETECTION, width: 960, height: 720,
    appears: [{ role: "bumper", x: 430, y: 235, ageMs: 200 }] });
  const ripples = ctx.draws.filter((d) => d.kind === "stroke" && d.clips === 3);
  assert.equal(ripples.length, 2, "two rings, each drawn with the 3 papers clipped out");
  for (const ring of ripples) {
    assert.equal(ring.color, NEUTRAL.dark.rim);
    for (const point of ring.path) {
      for (const note of Object.values(DETECTION.roles).flatMap((r) => r.notes)) assert.equal(inside(point, note), false);
      assert.ok(Math.max(Math.abs(point.x - 430), Math.abs(point.y - 235)) >= 30 + RIM_OFFSET_PX - 1e-9);
    }
  }
  const late = canvas();
  drawProjectorScene(late, { state, preview: DETECTION, width: 960, height: 720,
    appears: [{ role: "bumper", x: 430, y: 235, ageMs: APPEAR_EFFECT_MS + 1 }] });
  assert.equal(late.draws.filter((d) => d.kind === "stroke" && d.clips === 3).length, 0);
});

test("a bounce is tracked with its speed and bursts in the bumper color, harder hits bigger", () => {
  let tracked = trackEffects(null, { status: "running", attempts: 1, elapsed_s: 0.1, position: { x: 100, y: 100 }, bounces: 0 }, 1000);
  tracked = trackEffects(tracked, { status: "running", attempts: 1, elapsed_s: 0.2, position: { x: 100, y: 130 }, bounces: 1 }, 1100);
  assert.equal(tracked.hits.length, 1);
  assert.equal(tracked.hits[0].speed, 300, "30 px in 0.1 s");
  const ages = effectAges(tracked, 1200).hits;
  assert.deepEqual(ages, [{ ageMs: 100, origin: { x: 100, y: 130 }, speed: 300 }]);
  assert.equal(effectAges(tracked, 1100 + HIT_EFFECT_MS).hits.length, 0);
  const soft = canvas();
  const hard = canvas();
  const paper = square(100, 150, 20).points;
  drawHitEffect(soft, { ageMs: 50, origin: { x: 100, y: 130 }, paper, color: "#77ec8f", intensity: 0.1 });
  drawHitEffect(hard, { ageMs: 50, origin: { x: 100, y: 130 }, paper, color: "#77ec8f", intensity: 1 });
  const sparks = (ctx) => ctx.draws.filter((d) => d.kind === "fill" && d.path.length === 1).length;
  assert.ok(sparks(hard) > sparks(soft));
  assert.equal(soft.draws[0].color, "#ffffff", "the bumper paper flashes first");
  assert.ok(hard.draws.some((d) => d.color === "#77ec8f"));
});

test("3, 2, 1: beats from the seconds left, drawn at the drop point in the start color", () => {
  assert.deepEqual(countdownBeat(2.4, 2.4), { beat: 3, within: 0 });
  assert.equal(countdownBeat(1.6, 2.4).beat, 2, "not 3: 1.6 / 0.8 is 2.0000000000000004");
  assert.equal(countdownBeat(1.61, 2.4).beat, 3);
  assert.equal(countdownBeatOf({ countdown_s: 1.6, countdown_total_s: 2.4 }), 2);
  const half = countdownBeat(0.4, 2.4);
  assert.equal(half.beat, 1);
  assert.ok(Math.abs(half.within - 0.5) < 1e-12);
  assert.deepEqual(countdownBeat(0, 2.4), { beat: 0, within: 1 });
  const ctx = canvas();
  assert.equal(drawCountdown(ctx, { remainingS: 1.0, totalS: 2.4, origin: { x: 200, y: 400 }, color: "#ffdf53" }), 2);
  const text = ctx.draws.find((d) => d.kind === "text");
  assert.deepEqual([text.text, text.color, text.x], ["2", "#ffdf53", 200]);
  // In the scene: only while the backend says the camera is paused.
  const state = { game: { ...matrix.phases.countdown.state.game }, camera: matrix.phases.countdown.state.camera,
    palette: matrix.phases.countdown.state.palette };
  const scene = canvas();
  drawProjectorScene(scene, { state, preview: null, width: 1280, height: 800, countdownS: 2.0 });
  assert.ok(scene.draws.some((d) => d.kind === "text" && d.text === "3"));
  const frozen = canvas();
  drawProjectorScene(frozen, { state: { ...state, game: { ...state.game, camera_paused: false } }, preview: null,
    width: 1280, height: 800, countdownS: 2.0 });
  assert.equal(frozen.draws.some((d) => d.kind === "text" && d.text === "3"), false);
});

test("each beat ticks once, and the bounce sound follows the impact", () => {
  const at = (left) => ({ status: "ready", countdown_s: left, countdown_total_s: 2.4 });
  assert.deepEqual(soundEvents(at(2.4), at(2.3)), []);
  assert.deepEqual(soundEvents(at(1.7), at(1.5)), ["tick"]);
  assert.deepEqual(soundEvents({ status: "ready" }, at(2.4)), ["tick"]);
  assert.equal(countdownBeatOf(at(0.5)), 1);
  const calls = [];
  const node = () => ({ connect: (next) => next ?? node(), start() {}, stop() {}, type: "", frequency: param(), gain: param(), Q: { value: 0 } });
  function param() { return { setValueAtTime: (v) => calls.push(v), exponentialRampToValueAtTime() {} }; }
  const audio = { currentTime: 0, sampleRate: 8000, destination: {}, createOscillator: node, createGain: node,
    createBiquadFilter: node, createBufferSource: node, createBuffer: () => ({ getChannelData: () => new Float32Array(400) }) };
  assert.equal(playSound(audio, "tick", 3), true);
  calls.length = 0;
  playSound(audio, "bounce", 0, 0.15);
  const soft = calls[0];
  calls.length = 0;
  playSound(audio, "bounce", 0, 1);
  assert.ok(calls[0] > soft, "a harder bounce is higher");
});

test("panel wiring: Space and the remote play or resume, the toggle and the button are on the page", () => {
  const panel = read("app.js");
  const page = read("index.html");
  assert.match(panel, /\$\("launch-button"\)\.addEventListener\("click", \(\) => sendAction\("play", \{ source: "panel" \}\)\);/);
  assert.match(panel, /if \(remoteCode && event\.code === remoteCode\) \{\s*event\.preventDefault\(\);\s*playOrResume\("button"\);/);
  assert.match(panel, /sendAction\("hands_free", \{ enabled: event\.target\.checked \}\)/);
  for (const id of ["launch-label", "hands-free", "remote-assign", "remote-key", "remote-clear"]) {
    assert.match(page, new RegExp(`id="${id}"`));
  }
  const projector = read("projector.js");
  assert.match(projector, /appears: appearAges\(appearances, now\)/);
  assert.match(projector, /if \(next\?\.game\?\.camera_paused !== true\) appearances = trackAppearances/);
});
