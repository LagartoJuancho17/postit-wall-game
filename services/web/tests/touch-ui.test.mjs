/** Gate tests: the touch alignment's projector drawing, panel line and keys (contract 1.6). */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { TOUCH_GRIDS, touchCommand, touchGrid } from "../ui/align-run.mjs";
import { drawTouchGrid, projectorScene, TOUCH_COLORS, touchLines } from "../ui/live-view.mjs";
import { setupGuide, touchMessage } from "../ui/ui-core.mjs";

const matrix = JSON.parse(readFileSync(new URL("../contract/state-matrix.json", import.meta.url), "utf8"));
const read = (name) => readFileSync(new URL(`../ui/${name}`, import.meta.url), "utf8");

/** Records strokes, fills and texts with their colors. */
function canvas() {
  const draws = [];
  let path = [];
  return {
    draws, fillStyle: "#000000", strokeStyle: "#000000", lineWidth: 1, font: "", textAlign: "", textBaseline: "",
    lineCap: "", globalAlpha: 1, shadowBlur: 0, shadowColor: "", shadowOffsetX: 0, shadowOffsetY: 0,
    globalCompositeOperation: "", filter: "",
    save() {}, restore() {}, beginPath() { path = []; },
    moveTo(x, y) { path.push({ x, y }); }, lineTo(x, y) { path.push({ x, y }); },
    arc(x, y, r, start, end) { path.push({ x, y, r, sweep: end - start }); },
    fillRect(x, y, w, h) { draws.push({ kind: "rect", color: this.fillStyle, w, h }); },
    stroke() { draws.push({ kind: "stroke", color: this.strokeStyle, width: this.lineWidth, path }); },
    fill() { draws.push({ kind: "fill", color: this.fillStyle, path }); },
    fillText(text, x, y) { draws.push({ kind: "text", text, color: this.fillStyle, x, y }); },
  };
}

const touching = matrix.phases.touching.state;
const touch = touching.alignment.touch;

test("the backend's touching phase carries the grid the projector draws", () => {
  assert.equal(touching.alignment.status, "touching");
  assert.equal(touching.projection.mode, "touch");
  assert.equal(touch.targets.length, touch.cols * touch.rows);
  assert.deepEqual([touch.step, touch.phase, touch.many, touch.progress], [0, "waiting", false, 0]);
  assert.deepEqual(projectorScene({ mode: "touch" }), { kind: "touch", mode: "touch" });
  assert.equal(projectorScene({ mode: "touch", blackout: true }).kind, "black", "a blackout still wins");
});

test("the projector draws CCV's grid in white and grays only, the active cross long, its ring filling", () => {
  const ctx = canvas();
  const active = drawTouchGrid(ctx, { ...touch, step: 5, phase: "holding", progress: 0.5 }, 1280, 800);
  assert.equal(active, 5);
  assert.equal(ctx.draws[0].kind, "rect");
  assert.equal(ctx.draws[0].color, "#000000", "black behind the crosses, whatever the theme");
  const colors = new Set(ctx.draws.map((d) => d.color));
  for (const color of colors) {
    assert.ok([...Object.values(TOUCH_COLORS), "#000000"].includes(color), `no role-like color: ${color}`);
  }
  const strokes = ctx.draws.filter((d) => d.kind === "stroke");
  const t = touch.targets[5];
  const cross = strokes.find((d) => d.color === TOUCH_COLORS.active && d.path.length === 4);
  assert.ok(cross, "the active cross");
  const reach = Math.max(...cross.path.map((p) => Math.hypot(p.x - t.x, p.y - t.y)));
  assert.equal(reach, 80, "arms of 10% of the short side stick out past a held post-it");
  const ring = strokes.filter((d) => d.path.length === 1 && d.path[0].r === 60);
  assert.equal(ring.length, 2, "the track and its filled part");
  assert.ok(Math.abs(ring[1].path[0].sweep - Math.PI) < 1e-12, "half full at progress 0.5");
  assert.equal(strokes.filter((d) => d.color === TOUCH_COLORS.done).length, 5, "a check on each done cross");
  assert.equal(strokes.filter((d) => d.color === TOUCH_COLORS.next).length, touch.targets.length - 6);
  const texts = ctx.draws.filter((d) => d.kind === "text").map((d) => d.text);
  assert.equal(texts[0], `CALIBRANDO · punto 6 de ${touch.targets.length}`);
});

test("the instructions move away from an active cross in the upper half", () => {
  const top = canvas();
  drawTouchGrid(top, { ...touch, step: 0 }, 1280, 800);
  const bottom = canvas();
  drawTouchGrid(bottom, { ...touch, step: touch.targets.length - 1 }, 1280, 800);
  const y = (ctx) => ctx.draws.find((d) => d.kind === "text").y;
  assert.ok(y(top) > 400, "first cross is at the top: text at the bottom");
  assert.ok(y(bottom) < 100, "last cross is at the bottom: text at the top");
});

test("a full ring after the capture, and nothing drawn without targets or size", () => {
  const ctx = canvas();
  drawTouchGrid(ctx, { ...touch, step: 2, phase: "lift", progress: 0 }, 1280, 800);
  const ring = ctx.draws.filter((d) => d.kind === "stroke" && d.path.length === 1 && d.path[0].r === 60);
  assert.ok(Math.abs(ring[1].path[0].sweep - 2 * Math.PI) < 1e-12);
  assert.equal(drawTouchGrid(canvas(), null, 1280, 800), -1);
  assert.equal(drawTouchGrid(canvas(), touch, 0, 800), -1);
});

test("projector and panel say the same thing in their words", () => {
  assert.deepEqual(touchLines({ step: 15, phase: "lift" }, 16), ["CALIBRANDO · punto 16 de 16",
    "Listo. Sacá el post-it y llevalo a la cruz siguiente.",
    "B: tomar el fondo de nuevo · R: volver un punto · Esc: cancelar"]);
  assert.equal(touchLines({ step: 0, many: true }, 16)[1], "Hay más de un papel nuevo: dejá uno solo.");
  assert.equal(touchMessage(touch), "Toques: punto 1 de 16. Poné un post-it centrado en la cruz del círculo. " +
    "B retoma el fondo, R vuelve un punto, Esc cancela.");
  assert.match(touchMessage({ ...touch, phase: "holding" }), /Quieto…/);
  const guide = setupGuide(touching, true, true);
  assert.equal(guide.step, "align");
  assert.equal(guide.message, touchMessage(touch));
});

test("B, R and Esc are CCV's keys only during a touch run", () => {
  assert.equal(touchCommand(touching, "b"), "touch_background");
  assert.equal(touchCommand(touching, "R"), "touch_back");
  assert.equal(touchCommand(touching, "Escape"), "align_cancel");
  assert.equal(touchCommand(touching, " "), null);
  assert.equal(touchCommand(matrix.phases.ready.state, "r"), null, "R is reset outside a touch run");
  assert.deepEqual(touchGrid("6x5"), { cols: 6, rows: 5 });
  assert.deepEqual(touchGrid("9x9"), { cols: 4, rows: 4 }, "anything else is CCV's default 4 x 4");
  for (const value of TOUCH_GRIDS) assert.match(read("index.html"), new RegExp(`<option value="${value}"`));
});

test("panel and projector wire the touch run", () => {
  const panel = read("app.js");
  assert.match(panel, /const command = touchCommand\(state, event\.key\);/);
  assert.match(panel, /key === "t" && !\$\("touch-align"\)\.disabled/);
  assert.match(panel, /sendAction\("touch_start", touchGrid\(\$\("touch-grid"\)\.value\)\)/);
  // The backend ends the run on a camera frame; the panel then checks the overlay, as after the dots.
  assert.match(panel, /before === "touching" && next\?\.alignment\?\.status === "ready"\) setTimeout\(checkAfterTouch, 0\)/);
  assert.match(panel, /runOverlayCheck\(alignDeps\(\), alignTiming\(cameraFixed\)\)/);
  assert.match(read("projector.js"), /if \(scene\.kind === "touch"\) \{\s*\/\/[^\n]*\n[^\n]*\n\s*drawTouchGrid\(ctx, state\?\.alignment\?\.touch, viewport\.width, viewport\.height\);/);
  assert.match(read("index.html"), /id="touch-align"/);
});
