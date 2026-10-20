/**
 * Gate tests for what the projector may emit (docs/alignment-and-color.md
 * 2.1) and for the panel's own camera overlay, which is never projected.
 *
 * The fake canvas records the full drawing state of every fill, stroke and
 * text, shadow included: the old projector put a colored halo on the rim
 * stroke too, and a recorder that only looked at fills missed it.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  applyHomography,
  convexHull,
  homographyLocalScale,
  invertHomography,
  offsetConvexPolygon,
  polygonArea,
  projectorColors,
} from "../ui/ui-core.mjs";
import * as liveView from "../ui/live-view.mjs";
import {
  AIM_WIDTH_PX,
  alignedMatrix,
  drawLiveDetection,
  drawProjectorScene,
  FILL_ALPHA,
  FILL_INSET_PX,
  fillPolygon,
  HINT_FONT_PX,
  HINT_MARGIN_PX,
  LIVE_MAX_AGE_MS,
  MIN_FILL_AREA_PX,
  NEUTRAL,
  projectorScene,
  RESULT_TEXTS,
  RIM_MITER_LIMIT,
  RIM_OFFSET_PX,
  RIM_WIDTH_PX,
  rimPolygon,
  UNALIGNED_HINT,
} from "../ui/live-view.mjs";

const STATE_KEYS = ["fillStyle", "strokeStyle", "globalAlpha", "shadowBlur", "shadowColor", "shadowOffsetX",
  "shadowOffsetY", "lineWidth", "lineJoin", "miterLimit", "filter", "globalCompositeOperation", "font",
  "textAlign", "textBaseline"];
const DEFAULTS = { fillStyle: "#000000", strokeStyle: "#000000", globalAlpha: 1, shadowBlur: 0,
  shadowColor: "rgba(0, 0, 0, 0)", shadowOffsetX: 0, shadowOffsetY: 0, lineWidth: 1, lineJoin: "miter",
  miterLimit: 10, filter: "none", globalCompositeOperation: "source-over", font: "10px sans-serif",
  textAlign: "start", textBaseline: "alphabetic" };

/** A 2D context stand-in. `dirty` starts it with a colored blurred shadow, as leftovers from earlier code. */
function canvas({ dirty = false } = {}) {
  const draws = [];
  const stack = [];
  let path = [];
  const snapshot = (ctx) => Object.fromEntries(STATE_KEYS.map((key) => [key, ctx[key]]));
  const ctx = {
    draws, ...DEFAULTS,
    ...(dirty ? { shadowColor: "#ff9b49", shadowBlur: 18, shadowOffsetX: 2, globalAlpha: 0.5, filter: "blur(4px)" } : {}),
    save() { stack.push(snapshot(this)); },
    restore() { Object.assign(this, stack.pop()); },
    beginPath() { path = []; },
    moveTo(x, y) { path.push(["move", x, y]); },
    lineTo(x, y) { path.push(["line", x, y]); },
    closePath() { path.push(["close"]); },
    arc(x, y, radius, start, end) { path.push(["arc", x, y, radius, start, end]); },
    fill() { draws.push({ kind: "fill", path: structuredClone(path), ...snapshot(this) }); },
    stroke() { draws.push({ kind: "stroke", path: structuredClone(path), ...snapshot(this) }); },
    fillRect(...rect) { draws.push({ kind: "rect", rect, ...snapshot(this) }); },
    fillText(text, x, y) { draws.push({ kind: "text", text, x, y, ...snapshot(this) }); },
    strokeText(text, x, y) { draws.push({ kind: "strokeText", text, x, y, ...snapshot(this) }); },
    drawImage() { draws.push({ kind: "image" }); },
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
// A skewed camera -> projector mapping (the runtime fixture's known_mapping).
const H = [
  [1.4302055525851982, 0.039997657951908544, 40.0],
  [-0.017906657822151072, 1.2721345739142345, 22.0],
  [-4.809443052232611e-05, 5.273835729423269e-05, 1.0],
];
const aligned = (overrides = {}) => ({
  alignment: { status: "ready", matrix: H, projector: { width: 1280, height: 800 },
    camera: { label: "Cámara de prueba", width: 640, height: 480 },
    overlay: { status: "unchecked", fill: false }, ...overrides },
});
const points = (path) => path.filter((step) => step[0] === "move" || step[0] === "line").map(([, x, y]) => ({ x, y }));
const near = (actual, expected, tolerance, label = "") =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label} ${actual} != ${expected}`);
const samePolygon = (actual, expected, tolerance, label = "") => {
  assert.equal(actual.length, expected.length, `${label} vertex count`);
  actual.forEach((point, i) => {
    near(point.x, expected[i].x, tolerance, `${label} x${i}`);
    near(point.y, expected[i].y, tolerance, `${label} y${i}`);
  });
};
const insideConvex = (point, polygon, margin) => polygon.every((a, i) => {
  const b = polygon[(i + 1) % polygon.length];
  const area = polygon.reduce((sum, p, k) => sum + p.x * polygon[(k + 1) % polygon.length].y - polygon[(k + 1) % polygon.length].x * p.y, 0);
  const inward = Math.sign(area) * ((b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)) / Math.hypot(b.x - a.x, b.y - a.y);
  return inward >= margin;
});
const noEffects = (draw) => draw.shadowBlur === 0 && draw.shadowOffsetX === 0 && draw.shadowOffsetY === 0
  && draw.filter === "none" && draw.globalCompositeOperation === "source-over";
const isNeutral = (color) => Object.values(NEUTRAL).some((set) => Object.values(set).includes(color));

test("live data expires after the same 1.5 seconds in both views", () => {
  assert.equal(LIVE_MAX_AGE_MS, 1500);
});

test("contract constants and neutral colors", () => {
  assert.equal(RIM_OFFSET_PX, 6);
  assert.equal(RIM_WIDTH_PX, 3);
  assert.equal(RIM_MITER_LIMIT, 2);
  assert.equal(AIM_WIDTH_PX, 3);
  assert.deepEqual(RESULT_TEXTS, { won: "¡LLEGASTE!", lost: "FUERA DEL CAMPO" });
  assert.equal(FILL_INSET_PX, 5);
  assert.equal(FILL_ALPHA, 0.35);
  assert.equal(MIN_FILL_AREA_PX, 25);
  assert.deepEqual(NEUTRAL, { dark: { rim: "#ffffff", ball: "#f6fbfc" }, light: { rim: "#2b2f2e", ball: "#17211f" } });
  assert.equal(UNALIGNED_HINT, "Sin alinear · pulsá A en el panel");
  for (const theme of ["dark", "light"]) {
    const themeColors = projectorColors(theme);
    assert.equal(themeColors.ball, NEUTRAL[theme].ball);
    assert.equal(themeColors.won, NEUTRAL[theme].ball, "won text has no role hue");
    assert.equal(themeColors.lost, NEUTRAL[theme].ball, "lost text has no role hue");
  }
});

// --- Panel overlay: on the camera video only, never projected. ---

test("panel overlay keeps each role color with its colored halo and a white rim", () => {
  const ctx = canvas();
  drawLiveDetection(ctx, preview, palette, 640, 480);
  const fills = ctx.draws.filter((draw) => draw.kind === "fill");
  const strokes = ctx.draws.filter((draw) => draw.kind === "stroke");
  assert.equal(fills.length, 4);
  assert.equal(strokes.length, 4);
  assert.deepEqual(fills.map((draw) => draw.fillStyle), colors);
  for (const draw of fills) {
    assert.equal(draw.shadowColor, draw.fillStyle);
    assert.ok(draw.shadowBlur > 0, "each role has the camera's colored halo");
    assert.ok(draw.globalAlpha > 0 && draw.globalAlpha < 1, "translucent post-it fill");
  }
  for (const draw of strokes) assert.equal(draw.strokeStyle, "#ffffff");
  assert.equal(ctx.globalAlpha, 1, "renderer restores drawing state");
  assert.equal(ctx.shadowBlur, 0);
});

test("panel overlay centers the camera image without stretching", () => {
  const wide = canvas();
  drawLiveDetection(wide, preview, palette, 1280, 720);
  assert.deepEqual(wide.draws[0].path, [
    ["move", 310, 75], ["line", 370, 75], ["line", 370, 135], ["line", 310, 135], ["close"],
  ]);
  const portrait = canvas();
  drawLiveDetection(portrait, preview, palette, 480, 900);
  assert.deepEqual(portrait.draws[0].path, [
    ["move", 75, 307.5], ["line", 105, 307.5], ["line", 105, 337.5], ["line", 75, 337.5], ["close"],
  ]);
});

test("panel overlay skips invalid polygons and missing data, and falls back to visible colors", () => {
  const bad = [null, {}, { points: [] }, { points: [{ x: NaN, y: 0 }, ...rectangle.points.slice(1)] }];
  const ctx = canvas();
  drawLiveDetection(ctx, { ...preview, roles: { start: { notes: [...bad, rectangle] } } }, palette, 640, 480);
  assert.equal(ctx.draws.filter((draw) => draw.kind === "fill").length, 1);
  for (const missing of [null, undefined, { width: 640, height: 480 }]) {
    const empty = canvas();
    drawLiveDetection(empty, missing, palette, 640, 480);
    assert.equal(empty.draws.length, 0);
  }
  const fallback = canvas();
  drawLiveDetection(fallback, preview, null, 640, 480);
  for (const draw of fallback.draws.filter((d) => d.kind === "fill")) assert.match(draw.fillStyle, /^#[0-9a-f]{6}$/i);
});

// --- Projector: unaligned. ---

test("unaligned projector draws neutral rims only, through the contain transform, in both themes", () => {
  for (const theme of ["dark", "light"]) {
    const ctx = canvas({ dirty: true });
    drawProjectorScene(ctx, { preview, palette, theme, width: 1280, height: 720 });
    const strokes = ctx.draws.filter((draw) => draw.kind === "stroke");
    assert.equal(ctx.draws.filter((draw) => draw.kind === "fill").length, 0, "never a fill without alignment");
    assert.equal(strokes.length, 4, "one rim per note");
    const contained = [{ x: 310, y: 75 }, { x: 370, y: 75 }, { x: 370, y: 135 }, { x: 310, y: 135 }];
    for (const stroke of strokes) {
      assert.equal(stroke.strokeStyle, NEUTRAL[theme].rim);
      assert.equal(stroke.lineWidth, 3);
      assert.equal(stroke.lineJoin, "miter");
      assert.equal(stroke.miterLimit, 2);
      assert.equal(stroke.globalAlpha, 1);
      assert.ok(noEffects(stroke), "no shadow or blur, even on a dirty context");
      samePolygon(points(stroke.path), offsetConvexPolygon(contained, RIM_OFFSET_PX), 1e-9, "rim");
      samePolygon(offsetConvexPolygon(points(stroke.path), -RIM_OFFSET_PX), contained, 1e-9, "contain");
    }
    assert.equal(ctx.shadowBlur, 18, "the caller's context state is restored");
  }
});

test("unaligned projector keeps the contain mapping for portrait screens", () => {
  const ctx = canvas();
  drawProjectorScene(ctx, { preview, palette, width: 480, height: 900 });
  const contained = [{ x: 75, y: 307.5 }, { x: 105, y: 307.5 }, { x: 105, y: 337.5 }, { x: 75, y: 337.5 }];
  samePolygon(offsetConvexPolygon(points(ctx.draws.find((d) => d.kind === "stroke").path), -RIM_OFFSET_PX), contained, 1e-9);
});

test("unaligned projector shows the neutral hint bottom-left in the rim color", () => {
  for (const theme of ["dark", "light"]) {
    const ctx = canvas();
    drawProjectorScene(ctx, { state: { game: { status: "none" } }, theme, width: 1280, height: 720 });
    const [hint] = ctx.draws;
    assert.equal(ctx.draws.length, 1);
    assert.equal(hint.kind, "text");
    assert.equal(hint.text, "Sin alinear · pulsá A en el panel");
    assert.equal(hint.fillStyle, NEUTRAL[theme].rim);
    assert.deepEqual([hint.x, hint.y], [HINT_MARGIN_PX, 720 - HINT_MARGIN_PX]);
    assert.equal(hint.textAlign, "left");
    assert.equal(hint.textBaseline, "bottom");
    assert.match(hint.font, new RegExp(`(^|\\s)${HINT_FONT_PX}px\\s`));
    assert.ok(noEffects(hint));
  }
});

test("alignment that is not ready, malformed or for another viewport draws the unaligned scene", () => {
  const cases = [
    { ...aligned(), alignment: { ...aligned().alignment, status: "stale" } },
    { ...aligned(), alignment: { ...aligned().alignment, status: "failed" } },
    { ...aligned(), alignment: { ...aligned().alignment, status: "collecting" } },
    { ...aligned(), alignment: { ...aligned().alignment, matrix: null } },
    { ...aligned(), alignment: { ...aligned().alignment, matrix: [[1, 0], [0, 1]] } },
    { ...aligned(), alignment: { ...aligned().alignment, matrix: [[1, 2, 3], [2, 4, 6], [0, 0, 1]] } },
    aligned({ projector: { width: 1920, height: 1080 } }),
    aligned({ projector: null }),
    aligned({ camera: { label: "x", width: 1280, height: 720 } }), // H was measured at another camera size
    {},
  ];
  for (const state of cases) {
    const ctx = canvas();
    const filled = { ...state, alignment: state.alignment && { ...state.alignment, overlay: { status: "ok", fill: true } } };
    drawProjectorScene(ctx, { state: filled, preview, palette, width: 1280, height: 800, forceFill: true });
    assert.equal(ctx.draws.filter((d) => d.kind === "fill").length, 0, JSON.stringify(state.alignment));
    assert.ok(ctx.draws.some((d) => d.text === UNALIGNED_HINT), "hint shown");
    const fit = Math.min(1280 / 640, 800 / 480);
    const contained = rectangle.points.map((p) => ({ x: (1280 - 640 * fit) / 2 + p.x * fit, y: (800 - 480 * fit) / 2 + p.y * fit }));
    samePolygon(offsetConvexPolygon(points(ctx.draws.find((d) => d.kind === "stroke").path), -RIM_OFFSET_PX), contained, 1e-9);
  }
  assert.equal(alignedMatrix(aligned(), 1280, 800), H);
  assert.equal(alignedMatrix(aligned(), 1280, 801), null);
});

// --- Projector: aligned. ---

test("aligned projector maps each note through H and rims it 6 px outside; neutral light inside until checked", () => {
  for (const theme of ["dark", "light"]) {
    const ctx = canvas({ dirty: true });
    drawProjectorScene(ctx, { state: aligned(), preview, palette, theme, width: 1280, height: 800 });
    // overlay.fill is false: no role color. The dark theme lights each paper
    // with neutral white (a dark room still sees the whole paper; white can
    // never be taken for a post-it); the light theme's flood already lights it.
    const fills = ctx.draws.filter((d) => d.kind === "fill");
    if (theme === "dark") {
      assert.equal(fills.length, 4);
      for (const fill of fills) assert.deepEqual([fill.fillStyle, fill.globalAlpha], ["#ffffff", 0.35]);
    } else {
      assert.equal(fills.length, 0);
    }
    assert.equal(ctx.draws.filter((d) => d.kind === "text").length, 0, "no hint once aligned");
    const strokes = ctx.draws.filter((d) => d.kind === "stroke");
    assert.equal(strokes.length, 4);
    const mapped = rectangle.points.map((p) => applyHomography(H, p));
    for (const stroke of strokes) {
      assert.equal(stroke.strokeStyle, NEUTRAL[theme].rim);
      assert.ok(noEffects(stroke));
      samePolygon(offsetConvexPolygon(points(stroke.path), -RIM_OFFSET_PX), mapped, 1e-6, "H mapping");
      for (const vertex of points(stroke.path)) assert.ok(!insideConvex(vertex, mapped, 0), "rim lies outside the paper");
    }
  }
});

test("checked overlay fills each note 5 px inside the mapped paper, in its palette color, without halo", () => {
  const ctx = canvas({ dirty: true });
  drawProjectorScene(ctx, { state: aligned({ overlay: { status: "ok", fill: true } }), preview, palette, width: 1280, height: 800 });
  const fills = ctx.draws.filter((d) => d.kind === "fill");
  assert.equal(fills.length, 4);
  assert.deepEqual(fills.map((d) => d.fillStyle), colors);
  const mapped = rectangle.points.map((p) => applyHomography(H, p));
  for (const fill of fills) {
    assert.equal(fill.globalAlpha, FILL_ALPHA);
    assert.ok(noEffects(fill), "no colored shadow or blur around a fill");
    const polygon = points(fill.path);
    samePolygon(polygon, offsetConvexPolygon(mapped, -FILL_INSET_PX), 1e-9, "inset");
    for (const vertex of polygon) assert.ok(insideConvex(vertex, mapped, FILL_INSET_PX - 1e-6), "fill strictly inside the paper");
    assert.ok(polygonArea(polygon) < polygonArea(mapped));
  }
  // Every stroke after a fill is opaque and neutral: alpha does not leak into the rim.
  for (const stroke of ctx.draws.filter((d) => d.kind === "stroke")) {
    assert.equal(stroke.globalAlpha, 1);
    assert.ok(isNeutral(stroke.strokeStyle));
  }
});

test("the overlay check's forced fill works only when aligned", () => {
  const forced = canvas();
  drawProjectorScene(forced, { state: aligned(), preview, palette, width: 1280, height: 800, forceFill: true });
  assert.equal(forced.draws.filter((d) => d.kind === "fill").length, 4);
  const unaligned = canvas();
  drawProjectorScene(unaligned, { preview, palette, width: 1280, height: 800, forceFill: true });
  assert.equal(unaligned.draws.filter((d) => d.kind === "fill").length, 0);
});

test("a fill too small after the inset is skipped; the rim stays", () => {
  const tiny = { points: [{ x: 100, y: 100 }, { x: 106, y: 100 }, { x: 106, y: 106 }, { x: 100, y: 106 }] };
  const small = { width: 640, height: 480, roles: { goal: { notes: [tiny] } } };
  const ctx = canvas();
  drawProjectorScene(ctx, { state: aligned({ overlay: { fill: true } }), preview: small, palette, width: 1280, height: 800 });
  assert.equal(ctx.draws.filter((d) => d.kind === "fill").length, 0);
  assert.equal(ctx.draws.filter((d) => d.kind === "stroke").length, 1);
  // The inset survives as a valid 4 x 0.5 px sliver, but under 25 px² it is noise, not a note.
  const thin = { points: [{ x: 100, y: 100 }, { x: 114, y: 100 }, { x: 114, y: 110.5 }, { x: 100, y: 110.5 }] };
  const identity = aligned({ matrix: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], overlay: { fill: true } });
  const sliver = offsetConvexPolygon(thin.points, -FILL_INSET_PX);
  assert.ok(sliver && polygonArea(sliver) > 0 && polygonArea(sliver) < MIN_FILL_AREA_PX);
  const thinCtx = canvas();
  drawProjectorScene(thinCtx, { state: identity, preview: { ...small, roles: { goal: { notes: [thin] } } }, palette, width: 1280, height: 800 });
  assert.equal(thinCtx.draws.filter((d) => d.kind === "fill").length, 0);
  assert.equal(thinCtx.draws.filter((d) => d.kind === "stroke").length, 1);
});

test("a ghost fill seen on bare wall is re-detected strictly smaller every frame and dies", () => {
  const inverse = invertHomography(H);
  let note = { points: [{ x: 150, y: 320 }, { x: 150, y: 380 }, { x: 209, y: 381 }, { x: 211, y: 321 }] };
  let previous = Infinity;
  let frames = 0;
  for (; frames < 40; frames++) {
    const ctx = canvas();
    drawProjectorScene(ctx, { state: aligned({ overlay: { fill: true } }), preview: { width: 640, height: 480, roles: { goal: { notes: [note] } } },
      palette, width: 1280, height: 800 });
    const fill = ctx.draws.find((d) => d.kind === "fill");
    if (!fill) break;
    const area = polygonArea(points(fill.path));
    assert.ok(area < previous, `frame ${frames}: ${area} < ${previous}`);
    previous = area;
    // Paper removed: the camera now sees only the fill, exactly where it landed.
    note = { points: points(fill.path).map((p) => applyHomography(inverse, p)) };
  }
  assert.ok(frames >= 1 && frames < 40, `the ghost disappeared after ${frames} frames`);
});

test("a note behind the homography's horizon is not drawn", () => {
  const horizon = [[1, 0, 0], [0, 1, 0], [0.01, 0, 1]]; // w <= 0 for x <= -100
  const behind = { points: [{ x: -200, y: 10 }, { x: -150, y: 10 }, { x: -150, y: 60 }, { x: -200, y: 60 }] };
  const ctx = canvas();
  drawProjectorScene(ctx, { state: aligned({ matrix: horizon, overlay: { fill: true } }),
    preview: { width: 640, height: 480, roles: { start: { notes: [behind, rectangle] } } }, palette, width: 1280, height: 800 });
  assert.equal(ctx.draws.filter((d) => d.kind === "stroke").length, 1, "only the note in front is drawn");
});

test("stale detections draw no notes, and nothing projected is non-finite", () => {
  const ctx = canvas();
  drawProjectorScene(ctx, { state: aligned(), preview, palette, width: 1280, height: 800, fresh: false });
  assert.equal(ctx.draws.length, 0);
  for (const value of [0, -1, NaN, Infinity, undefined]) {
    for (const [w, h] of [[value, 720], [1280, value]]) {
      const bad = canvas();
      drawProjectorScene(bad, { preview, palette, width: w, height: h });
      assert.equal(bad.draws.length, 0, `viewport ${w}×${h}`);
    }
    const frame = canvas();
    drawProjectorScene(frame, { preview: { ...preview, width: value }, palette, width: 1280, height: 720 });
    assert.equal(frame.draws.filter((d) => d.kind !== "text").length, 0, `frame width ${value}`);
  }
  const broken = canvas();
  const invalid = [null, {}, { points: [] }, { points: [{ x: 0, y: Infinity }, ...rectangle.points.slice(1)] }];
  drawProjectorScene(broken, { preview: { ...preview, roles: { start: { notes: [...invalid, rectangle] } } }, palette, width: 640, height: 480 });
  assert.equal(broken.draws.filter((d) => d.kind === "stroke").length, 1);
  for (const draw of broken.draws) for (const step of draw.path ?? []) assert.ok(step.slice(1).every(Number.isFinite));
  assert.doesNotThrow(() => drawProjectorScene(canvas(), { width: 1280, height: 720 }));
  assert.doesNotThrow(() => drawProjectorScene(canvas()));
});

// --- Ball, aim and result text. ---

const ball = (ctx) => ctx.draws.filter((d) => d.kind === "fill" && d.path[0]?.[0] === "arc");

test("camera-space ball uses the contain transform when unaligned, game size first", () => {
  const ctx = canvas();
  drawProjectorScene(ctx, { state: { game: { width: 640, height: 480, position: { x: 100, y: 50 }, status: "running" },
    camera: { width: 1280, height: 720 } }, width: 1280, height: 720 });
  const [only] = ball(ctx);
  assert.equal(ball(ctx).length, 1);
  assert.deepEqual(only.path[0].slice(1, 4), [310, 75, 15]);
  assert.equal(ctx.draws.filter((d) => d.kind === "stroke").length, 0, "a running ball redraws no board");
  const fallback = canvas();
  drawProjectorScene(fallback, { state: { game: { position: { x: 100, y: 50 }, status: "running" }, camera: { width: 640, height: 480 } },
    width: 1280, height: 720 });
  assert.deepEqual(ball(fallback)[0].path[0].slice(1, 3), [310, 75]);
});

test("projector-space ball is drawn as is, radius 10 px", () => {
  for (const alignment of [aligned(), {}]) {
    const ctx = canvas();
    drawProjectorScene(ctx, { state: { ...alignment, game: { space: "projector", width: 1280, height: 800,
      position: { x: 640.5, y: 400.25 }, status: "running" } }, width: 1280, height: 800 });
    assert.deepEqual(ball(ctx)[0].path[0].slice(1, 4), [640.5, 400.25, 10]);
  }
});

test("camera-space ball maps through H when aligned, radius scaled by H's local stretch", () => {
  const position = { x: 405, y: 190 };
  const ctx = canvas();
  drawProjectorScene(ctx, { state: { ...aligned(), game: { space: "camera", width: 640, height: 480, position,
    status: "ready", aim_degrees: 0 } }, width: 1280, height: 800 });
  const [x, y, radius] = ball(ctx)[0].path[0].slice(1, 4);
  const center = applyHomography(H, position);
  near(x, center.x, 1e-6);
  near(y, center.y, 1e-6);
  near(radius, 10 * homographyLocalScale(H, position), 1e-9);
  const aim = ctx.draws.find((d) => d.kind === "stroke");
  const tip = applyHomography(H, { x: position.x + 70, y: position.y });
  near(aim.path[1][1], tip.x, 1e-6);
  near(aim.path[1][2], tip.y, 1e-6);
});

test("ball, aim line and result texts are neutral in both themes", () => {
  for (const theme of ["dark", "light"]) {
    const ready = canvas({ dirty: true });
    drawProjectorScene(ready, { state: { game: { position: { x: 100, y: 50 }, status: "ready", aim_degrees: 0, width: 640, height: 480 } },
      theme, width: 1280, height: 720 });
    const aim = ready.draws.find((d) => d.kind === "stroke");
    assert.deepEqual(aim.path[0], ["move", 310, 75]);
    near(aim.path[1][1], 310 + 70 * 1.5, 1e-9);
    assert.equal(aim.strokeStyle, NEUTRAL[theme].ball);
    assert.ok(noEffects(aim));
    assert.equal(ball(ready)[0].fillStyle, NEUTRAL[theme].ball);
    for (const [status, label] of [["won", "¡LLEGASTE!"], ["lost", "FUERA DEL CAMPO"]]) {
      const ended = canvas();
      drawProjectorScene(ended, { state: { game: { position: { x: 100, y: 50 }, status, width: 640, height: 480 } }, theme, width: 1280, height: 720 });
      const text = ended.draws.find((d) => d.text === label);
      assert.equal(text.fillStyle, NEUTRAL[theme].ball, `${label} is neutral`);
      assert.deepEqual([text.x, text.y, text.textAlign], [640, 75, "center"]);
      assert.equal(ball(ended).length, 1);
    }
  }
});

test("nothing the projector draws outside a paper carries a role color", () => {
  const roleHues = new Set([...colors, "#ffdf53", "#ffa659", "#77ec8f", "#ff77d3", "#ff8680", "#946f00", "#c2540c", "#1f7a43", "#b0392e"]);
  for (const theme of ["dark", "light"]) {
    for (const state of [{}, aligned(), aligned({ overlay: { fill: true } })]) {
      const ctx = canvas({ dirty: true });
      drawProjectorScene(ctx, { state: { ...state, game: { status: "won", position: { x: 100, y: 50 }, width: 640, height: 480 } },
        preview, palette, theme, width: 1280, height: 800 });
      const mapped = state.alignment ? rectangle.points.map((p) => applyHomography(H, p)) : null;
      for (const draw of ctx.draws) {
        assert.notEqual(draw.kind, "image");
        assert.ok(noEffects(draw), `${draw.kind} has no shadow or blur`);
        const color = draw.kind === "stroke" ? draw.strokeStyle : draw.fillStyle;
        if (isNeutral(color)) continue;
        assert.ok(roleHues.has(color) && draw.kind === "fill" && mapped, `${draw.kind} ${color} only as an aligned fill`);
        for (const vertex of points(draw.path)) assert.ok(insideConvex(vertex, mapped, FILL_INSET_PX - 1e-6));
      }
    }
  }
});

// --- Robustness beyond what the detector reports. ---

test("a note that is not convex still gets a rim around its hull, never a fill", () => {
  // The detector reports convex quads only; a malformed one must not vanish from the wall.
  const notch = { points: [{ x: 100, y: 50 }, { x: 140, y: 50 }, { x: 125, y: 70 }, { x: 140, y: 90 }, { x: 100, y: 90 }] };
  const identity = aligned({ matrix: [[1, 0, 0], [0, 1, 0], [0, 0, 1]], overlay: { status: "ok", fill: true } });
  const ctx = canvas();
  drawProjectorScene(ctx, { state: identity, preview: { width: 640, height: 480, roles: { start: { notes: [notch] } } },
    palette, width: 1280, height: 800 });
  assert.equal(ctx.draws.filter((d) => d.kind === "fill").length, 0, "fill needs a convex inset");
  const [rim] = ctx.draws.filter((d) => d.kind === "stroke");
  samePolygon(points(rim.path), offsetConvexPolygon(convexHull(notch.points), RIM_OFFSET_PX), 1e-12, "hull rim");
  for (const vertex of notch.points) assert.ok(insideConvex(vertex, points(rim.path), RIM_OFFSET_PX - 1e-9), "rim outside the paper");
  assert.equal(fillPolygon(notch.points), null);
  assert.deepEqual(rimPolygon(rectangle.points), offsetConvexPolygon(rectangle.points, RIM_OFFSET_PX), "convex notes use their own polygon");
});

test("H scaled by any factor, even a negative one, draws the same notes and ball", () => {
  // The horizon side comes from the camera frame's center, not from the sign convention of h22.
  const draw = (matrix) => {
    const ctx = canvas();
    drawProjectorScene(ctx, { state: { ...aligned({ matrix, overlay: { fill: true } }),
      game: { space: "camera", position: { x: 405, y: 190 }, status: "ready", aim_degrees: 30, width: 640, height: 480 } },
    preview, palette, width: 1280, height: 800 });
    return ctx.draws.map((d) => ({ kind: d.kind, path: d.path, color: d.fillStyle + d.strokeStyle }));
  };
  const expected = draw(H);
  assert.equal(expected.filter((d) => d.kind === "stroke").length, 5, "four rims and the aim line");
  for (const factor of [-1, 2.5, -0.01]) {
    const scaled = draw(H.map((row) => row.map((value) => value * factor)));
    assert.equal(scaled.length, expected.length, `factor ${factor}`);
    scaled.forEach((d, i) => {
      assert.equal(d.kind, expected[i].kind);
      d.path.forEach((step, k) => step.slice(1).forEach((value, c) => near(value, expected[i].path[k][c + 1], 1e-6)));
    });
  }
  // Behind the horizon is decided relative to the camera center, for -H as for H.
  const horizon = [[1, 0, 0], [0, 1, 0], [0.01, 0, 1]];
  const behind = { points: [{ x: -200, y: 10 }, { x: -150, y: 10 }, { x: -150, y: 60 }, { x: -200, y: 60 }] };
  for (const matrix of [horizon, horizon.map((row) => row.map((value) => -value))]) {
    const ctx = canvas();
    drawProjectorScene(ctx, { state: aligned({ matrix }), preview: { width: 640, height: 480, roles: { start: { notes: [behind, rectangle] } } },
      palette, width: 1280, height: 800 });
    assert.equal(ctx.draws.filter((d) => d.kind === "stroke").length, 1);
  }
});

test("a projector-space game from another viewport is contain-fitted, and drawn as is on its own", () => {
  const game = { space: "projector", width: 1280, height: 800, position: { x: 640, y: 400 }, status: "running" };
  const same = canvas();
  drawProjectorScene(same, { state: { game }, width: 1280, height: 800 });
  assert.deepEqual(ball(same)[0].path[0].slice(1, 4), [640, 400, 10]);
  const larger = canvas();
  drawProjectorScene(larger, { state: { game }, width: 1920, height: 1200 });
  assert.deepEqual(ball(larger)[0].path[0].slice(1, 4), [960, 600, 15], "until the backend re-measures the game");
  const old = canvas();
  drawProjectorScene(old, { state: { game: { ...game, width: undefined, height: undefined } }, width: 1920, height: 1200 });
  assert.deepEqual(ball(old)[0].path[0].slice(1, 4), [640, 400, 10], "no size: projector px as is");
});

test("projectorScene: scan blackout, then the panel's local scene, then the backend mode", () => {
  const layout = { dots: [], radius: 4 };
  const pattern = { kind: "pattern", layout, frame: { bit: 3, inverse: true } };
  assert.deepEqual(projectorScene(), { kind: "live", mode: "live", forceFill: false });
  assert.deepEqual(projectorScene({ mode: "live" }), { kind: "live", mode: "live", forceFill: false });
  assert.deepEqual(projectorScene({ mode: "black" }), { kind: "black", mode: "black" });
  assert.deepEqual(projectorScene({ mode: "align" }), { kind: "black", mode: "align" }, "black until a pattern arrives");
  assert.deepEqual(projectorScene({ mode: "neutral" }), { kind: "neutral", mode: "neutral" });
  assert.deepEqual(projectorScene({ mode: "align", local: pattern }), { kind: "pattern", mode: "align", layout, frame: pattern.frame });
  assert.deepEqual(projectorScene({ mode: "live", local: pattern }).kind, "pattern", "the pattern can arrive before the state");
  assert.deepEqual(projectorScene({ mode: "live", local: { kind: "neutral" } }), { kind: "neutral", mode: "neutral" });
  assert.deepEqual(projectorScene({ mode: "live", local: { kind: "overlay_test" } }), { kind: "live", mode: "live", forceFill: true });
  for (const local of [null, pattern, { kind: "neutral" }, { kind: "overlay_test" }]) {
    for (const mode of ["live", "neutral", "align", "black", undefined]) {
      assert.deepEqual(projectorScene({ blackout: true, local, mode }), { kind: "black", mode: "black" }, "a scan always sees black");
    }
  }
  assert.equal(projectorScene({ mode: "something new" }).kind, "live", "unknown modes keep the live drawing");
  assert.equal(projectorScene({ local: { kind: "unknown" }, mode: "neutral" }).kind, "neutral", "unknown local scenes fall through");
});

test("projector.js imports only what live-view exports, and uses all of it", () => {
  const source = readFileSync(new URL("../ui/projector.js", import.meta.url), "utf8");
  const names = source.match(/import\s*\{([^}]*)\}\s*from\s*"\.\/live-view\.mjs";/)?.[1]
    .split(",").map((name) => name.trim()).filter(Boolean);
  assert.ok(names?.length, "projector.js imports the live renderer");
  const body = source.slice(source.indexOf('from "./live-view.mjs";'));
  for (const name of names) {
    assert.ok(name in liveView, `${name} is not exported by live-view.mjs`);
    assert.match(body, new RegExp(`(?<![\\w.$])${name}\\b`), `${name} is imported but unused`);
  }
  for (const name of ["drawProjectorScene", "drawAlignmentPattern", "projectorScene"]) assert.ok(names.includes(name), name);
  assert.doesNotMatch(source, /drawLiveDetection|drawLiveGame/, "the projector never draws the panel's colored overlay");
});
