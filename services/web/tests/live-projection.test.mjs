/** The camera and projector share one contain transform and one live renderer. */
import assert from "node:assert/strict";
import test from "node:test";

import { drawLiveDetection, drawLiveGame, LIVE_MAX_AGE_MS } from "../ui/live-view.mjs";

function canvas() {
  const draws = [];
  const stack = [];
  let path = [];
  const ctx = {
    draws, globalAlpha: 1, shadowBlur: 0,
    save() { stack.push({ fillStyle: this.fillStyle, strokeStyle: this.strokeStyle,
      globalAlpha: this.globalAlpha, shadowBlur: this.shadowBlur, shadowColor: this.shadowColor }); },
    restore() { Object.assign(this, stack.pop()); },
    beginPath() { path = []; },
    moveTo(x, y) { path.push(["move", x, y]); },
    lineTo(x, y) { path.push(["line", x, y]); },
    closePath() { path.push(["close"]); },
    arc(x, y, radius, start, end) { path.push(["arc", x, y, radius, start, end]); },
    fill() { draws.push({ kind: "fill", path: structuredClone(path), color: this.fillStyle,
      alpha: this.globalAlpha, shadow: this.shadowColor, blur: this.shadowBlur }); },
    stroke() { draws.push({ kind: "stroke", path: structuredClone(path), color: this.strokeStyle,
      alpha: this.globalAlpha, width: this.lineWidth }); },
    fillText(text, x, y) { draws.push({ kind: "text", text, x, y }); },
  };
  return ctx;
}

const roles = ["start", "goal", "bumper", "marker"];
const colors = ["#e0c431", "#ec813c", "#44c76f", "#d839b0"];
const palette = { roles: Object.fromEntries(roles.map((role, i) => [role, { hex: colors[i] }])) };
const rectangle = { points: [{ x: 100, y: 50 }, { x: 140, y: 50 }, { x: 140, y: 90 }, { x: 100, y: 90 }] };
const preview = {
  width: 640, height: 480,
  roles: Object.fromEntries(roles.map((role) => [role, { notes: [rectangle], count: 1 }])),
};

test("live data expires after the same 1.5 seconds in both views", () => {
  assert.equal(LIVE_MAX_AGE_MS, 1500);
});

test("all four note roles use their selected color and the live white rim", () => {
  const ctx = canvas();
  drawLiveDetection(ctx, preview, palette, 640, 480);
  const fills = ctx.draws.filter((draw) => draw.kind === "fill");
  const strokes = ctx.draws.filter((draw) => draw.kind === "stroke");
  assert.equal(fills.length, 4);
  assert.equal(strokes.length, 4);
  assert.deepEqual(fills.map((draw) => draw.color), colors);
  for (const draw of fills) {
    assert.equal(draw.shadow, draw.color);
    assert.ok(draw.blur > 0, "each role has the camera's colored halo");
    assert.ok(draw.alpha > 0 && draw.alpha < 1, "translucent post-it fill");
  }
  for (const draw of strokes) {
    assert.equal(draw.color, "#ffffff");
    assert.equal(draw.alpha, 1);
  }
  assert.equal(ctx.globalAlpha, 1, "renderer restores drawing state for the ball");
  assert.equal(ctx.shadowBlur, 0);
});

test("light background keeps selected fills and gives every rim at least 4:1 contrast", () => {
  const ctx = canvas();
  drawLiveDetection(ctx, preview, palette, 640, 480, "light");
  assert.deepEqual(ctx.draws.filter((draw) => draw.kind === "fill").map((draw) => draw.color), colors);
  const luminance = (hex) => {
    const channels = hex.slice(1).match(/../g).map((value) => parseInt(value, 16) / 255)
      .map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
    return channels.reduce((sum, value, i) => sum + value * [.2126, .7152, .0722][i], 0);
  };
  const background = luminance("#f3f2ec");
  for (const stroke of ctx.draws.filter((draw) => draw.kind === "stroke")) {
    const rim = luminance(stroke.color);
    const contrast = (Math.max(background, rim) + .05) / (Math.min(background, rim) + .05);
    assert.ok(contrast >= 4, `light rim ${stroke.color} has ${contrast.toFixed(2)}:1 contrast`);
    assert.equal(stroke.alpha, 1);
  }
});

test("wide projector keeps geometry and centers camera image without stretching", () => {
  const ctx = canvas();
  drawLiveDetection(ctx, preview, palette, 1280, 720);
  assert.deepEqual(ctx.draws[0].path, [
    ["move", 310, 75], ["line", 370, 75], ["line", 370, 135], ["line", 310, 135], ["close"],
  ]);
});

test("portrait projector centers the image vertically without stretching", () => {
  const ctx = canvas();
  drawLiveDetection(ctx, preview, palette, 480, 900);
  assert.deepEqual(ctx.draws[0].path, [
    ["move", 75, 307.5], ["line", 105, 307.5], ["line", 105, 337.5], ["line", 75, 337.5], ["close"],
  ]);
});

test("live preview works before a complete board exists", () => {
  const ctx = canvas();
  drawLiveDetection(ctx, { width: 640, height: 480, roles: { bumper: { notes: [rectangle] } } }, palette, 640, 480);
  assert.equal(ctx.draws.filter((draw) => draw.kind === "fill").length, 1);
  assert.equal(ctx.draws[0].color, colors[2]);
});

test("missing detections render nothing, without recovering stale scanned outlines", () => {
  for (const missing of [null, undefined, { width: 640, height: 480 },
    { ...preview, roles: Object.fromEntries(roles.map((role) => [role, { notes: [], count: 0 }])) }]) {
    const ctx = canvas();
    assert.doesNotThrow(() => drawLiveDetection(ctx, missing, palette, 640, 480));
    assert.equal(ctx.draws.length, 0);
  }
});

test("unusable frame and viewport sizes never emit non-finite drawing coordinates", () => {
  for (const value of [0, -1, NaN, Infinity, undefined]) {
    for (const axis of ["width", "height"]) {
      const ctx = canvas();
      drawLiveDetection(ctx, { ...preview, [axis]: value }, palette, 1280, 720);
      assert.equal(ctx.draws.length, 0, `frame ${axis}=${value}`);
    }
    for (const [width, height] of [[value, 720], [1280, value]]) {
      const ctx = canvas();
      drawLiveDetection(ctx, preview, palette, width, height);
      assert.equal(ctx.draws.length, 0, `viewport ${width}×${height}`);
    }
  }
});

test("invalid polygons do not hide valid notes or send NaN to canvas", () => {
  const bad = [null, {}, { points: [] }, { points: [{ x: NaN, y: 0 }, ...rectangle.points.slice(1)] },
    { points: [{ x: 0, y: Infinity }, ...rectangle.points.slice(1)] }];
  const ctx = canvas();
  drawLiveDetection(ctx, { ...preview, roles: { start: { notes: [...bad, rectangle] } } }, palette, 640, 480);
  assert.equal(ctx.draws.filter((draw) => draw.kind === "fill").length, 1);
  assert.deepEqual(ctx.draws[0].path[0], ["move", 100, 50]);
});

test("missing palette still renders every note using a visible fallback", () => {
  const ctx = canvas();
  drawLiveDetection(ctx, preview, null, 640, 480);
  assert.equal(ctx.draws.filter((draw) => draw.kind === "fill").length, 4);
  for (const draw of ctx.draws.filter((draw) => draw.kind === "fill")) assert.match(draw.color, /^#[0-9a-f]{6}$/i);
});

test("ball uses camera coordinates once, with the same transform as live notes", () => {
  const ctx = canvas();
  drawLiveGame(ctx, { width: 640, height: 480, position: { x: 100, y: 50 }, status: "running" }, 640, 480, 1280, 720);
  const balls = ctx.draws.filter((draw) => draw.kind === "fill" && draw.path[0]?.[0] === "arc");
  assert.equal(balls.length, 1);
  assert.deepEqual(balls[0].path[0].slice(1, 3), [310, 75]);
  assert.equal(ctx.draws.filter((draw) => draw.kind === "stroke").length, 0,
    "a running ball must not redraw the scanned board");
});

test("game's source dimensions override stale video dimensions", () => {
  const ctx = canvas();
  drawLiveGame(ctx, { width: 640, height: 480, position: { x: 100, y: 50 }, status: "running" }, 1280, 720, 1280, 720);
  const ball = ctx.draws.find((draw) => draw.kind === "fill");
  assert.deepEqual(ball.path[0].slice(1, 3), [310, 75]);
});

test("ready aim and final result remain available without drawing a frozen board", () => {
  const ready = canvas();
  drawLiveGame(ready, { position: { x: 100, y: 50 }, status: "ready", aim_degrees: 0 }, 640, 480, 1280, 720);
  const aim = ready.draws.find((draw) => draw.kind === "stroke");
  assert.deepEqual(aim.path[0], ["move", 310, 75]);
  assert.equal(aim.path[1][2], 75);
  assert.ok(aim.path[1][1] > 310);
  for (const [status, label] of [["won", "¡LLEGASTE!"], ["lost", "FUERA DEL CAMPO"]]) {
    const ended = canvas();
    drawLiveGame(ended, { position: { x: 100, y: 50 }, status }, 640, 480, 1280, 720);
    assert.equal(ended.draws.find((draw) => draw.kind === "text").text, label);
    assert.equal(ended.draws.filter((draw) => draw.kind === "fill").length, 1);
  }
});
