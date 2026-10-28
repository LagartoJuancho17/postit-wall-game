/** Gate tests: a level on the wall (contract 3.2) and the mark colors (2.6). */
import assert from "node:assert/strict";
import test from "node:test";

import {
  bannerAlpha, bumperLine, drawLevel, drawLevelProblem, LEVEL_BANNER_MS, levelMapper, levelTitle, nextLine,
  PORTAL_COLOR, resultText, SPIKES_COLOR, spikeTeeth, trackLevel,
} from "../ui/level-view.mjs";
import { drawProjectorScene, FALLBACK_COLORS, markColor, NEUTRAL } from "../ui/live-view.mjs";
import { levelOptions, levelStatus } from "../ui/ui-core.mjs";

function canvas() {
  const draws = [];
  const clips = [];
  const stack = [];
  let path = [];
  const keys = ["fillStyle", "strokeStyle", "globalAlpha", "lineWidth", "font", "textAlign"];
  const snap = (ctx) => Object.fromEntries(keys.map((key) => [key, ctx[key]]));
  return {
    draws, clips, fillStyle: "#000000", strokeStyle: "#000000", globalAlpha: 1, lineWidth: 1, font: "", textAlign: "left",
    dash: [],
    save() { stack.push(snap(this)); },
    restore() { Object.assign(this, stack.pop()); },
    beginPath() { path = []; },
    moveTo(x, y) { path.push([x, y]); },
    lineTo(x, y) { path.push([x, y]); },
    closePath() {},
    arc(x, y) { path.push([x, y]); },
    ellipse(x, y) { path.push([x, y]); },
    rect() {},
    clip(rule) { clips.push(rule); },
    setLineDash(dash) { this.dash = dash; },
    fill() { draws.push({ kind: "fill", path, ...snap(this) }); },
    stroke() { draws.push({ kind: "stroke", path, dash: this.dash, ...snap(this) }); },
    fillText(text, x, y) { draws.push({ kind: "text", text, x, y, ...snap(this) }); },
    fillRect() {}, createRadialGradient() { return { addColorStop() {} }; },
  };
}

const rect = (x0, y0, x1, y1) => ({ points: [{ x: x0, y: y0 }, { x: x1, y: y0 }, { x: x1, y: y1 }, { x: x0, y: y1 }] });

function level(extra = {}) {
  return {
    id: "5", name: "Portal", hint: "Lo que entra por el portal sale por arriba.", bumpers_max: 1, next: "6",
    list: [{ id: "libre", name: "Libre", done: false }, { id: "5", name: "Portal", done: true }, { id: "6", name: "Doble salto", done: false }],
    geometry: {
      space: "projector", width: 1280, height: 800,
      start_zone: rect(100, 50, 250, 200), goal_zone: rect(1000, 580, 1150, 740),
      walls: [rect(600, 300, 620, 800)],
      spikes: [{ ...rect(500, 500, 800, 528), facing: "up" }],
      portals: [{ entry: rect(80, 320, 280, 352), exit: { x: 640, y: 96 } }],
    },
    check: { start: true, goal: false, bumpers: 2, bumpers_max: 1, covered: [] },
    problem: "Poné el post-it de llegada dentro de su recuadro.",
    advance_s: 0,
    ...extra,
  };
}

test("the level's space maps onto the projector by contain-fit, the identity when equal", () => {
  const same = levelMapper({ width: 1280, height: 800 }, 1280, 800);
  assert.deepEqual(same.point({ x: 10, y: 20 }), { x: 10, y: 20 });
  const camera = levelMapper({ width: 960, height: 720 }, 1280, 800);
  assert.equal(camera.scale, 800 / 720);
  assert.deepEqual(camera.point({ x: 0, y: 0 }), { x: (1280 - 960 * 800 / 720) / 2, y: 0 });
  assert.equal(levelMapper(null, 1280, 800), null);
  assert.equal(levelMapper({ width: 0, height: 10 }, 1280, 800), null);
});

test("texts: title, bumper count, why it ended, what follows", () => {
  assert.equal(levelTitle(level()), "NIVEL 5 · PORTAL");
  assert.equal(levelTitle({ id: "libre" }), null);
  assert.equal(bumperLine(level()), "Rebotes 2 / 1");
  assert.equal(bumperLine(level({ bumpers_max: 0 })), "Sin rebotes");
  assert.equal(bumperLine(level({ bumpers_max: null })), null);
  assert.equal(resultText({ status: "lost", ended_by: "spikes" }), "¡PINCHOS!");
  assert.equal(resultText({ status: "lost", ended_by: "exit" }), "FUERA DEL CAMPO");
  assert.equal(resultText({ status: "lost", ended_by: "stall" }), "SE QUEDÓ QUIETA");
  assert.equal(resultText({ status: "won" }, level()), "¡LLEGASTE!");
  assert.equal(resultText({ status: "won" }, level({ next: null })), "¡COMPLETASTE TODOS LOS NIVELES!");
  assert.equal(resultText({ status: "won" }, { id: "libre", next: null }), "¡LLEGASTE!");
  assert.equal(resultText({ status: "ready" }), null);
  assert.equal(nextLine({ status: "won" }, level({ advance_s: 2 })), "Sigue el nivel 6: Doble salto");
  assert.equal(nextLine({ status: "won" }, level({ advance_s: 0 })), null);
});

test("the banner shows once per level change, never for the level the window opened on", () => {
  let seen = trackLevel(null, { id: "1" }, 100);
  assert.equal(bannerAlpha(1000 - seen.at), 0, "the first level seen is no change");
  seen = trackLevel(seen, { id: "1" }, 500);
  seen = trackLevel(seen, { id: "2" }, 1000);
  assert.deepEqual(seen, { id: "2", at: 1000 });
  assert.equal(trackLevel(seen, null, 2000), seen);
  assert.equal(bannerAlpha(0), 0);
  assert.equal(bannerAlpha(1000), 1);
  assert.equal(bannerAlpha(LEVEL_BANNER_MS), 0);
  assert.ok(bannerAlpha(LEVEL_BANNER_MS - 300) < 1);
});

test("spike teeth fill their strip and point where the strip faces", () => {
  const strip = rect(0, 0, 300, 30).points;
  const up = spikeTeeth(strip, "up");
  assert.equal(up.length, Math.round(300 / 33));
  assert.ok(up.every(([a, apex, b]) => a.y === 30 && b.y === 30 && apex.y === 0));
  assert.equal(up.at(-1)[2].x, 300);
  const down = spikeTeeth(strip, "down");
  assert.ok(down.every(([, apex]) => apex.y === 30));
  const left = spikeTeeth(rect(0, 0, 30, 300).points, "left");
  assert.ok(left.every(([, apex]) => apex.x === 0));
  assert.deepEqual(spikeTeeth(rect(0, 0, 0, 10).points), []);
});

test("while the camera reads the wall, the level is neutral light clipped off every paper", () => {
  for (const theme of ["dark", "light"]) {
    const color = NEUTRAL[theme].rim;
    const ctx = canvas();
    const papers = [rect(120, 80, 180, 140).points, rect(1050, 620, 1110, 680).points];
    const shapes = drawLevel(ctx, { state: { level: level(), game: { camera_paused: false } }, width: 1280, height: 800,
      color, keepClear: papers });
    assert.equal(shapes, 5, "two boxes, a wall, a spike strip, a portal");
    assert.equal(ctx.clips.filter((rule) => rule === "evenodd").length, 2, "one even-odd clip per paper");
    for (const draw of ctx.draws) {
      const used = draw.kind === "stroke" ? draw.strokeStyle : draw.fillStyle;
      assert.equal(used, color, `${draw.kind} ${draw.text ?? ""} is neutral`);
    }
    const texts = ctx.draws.filter((d) => d.kind === "text").map((d) => d.text);
    assert.deepEqual(texts, ["INICIO ✓", "LLEGADA", "NIVEL 5 · PORTAL", "Rebotes 2 / 1"]);
    const [start, goal] = ctx.draws.filter((d) => d.kind === "stroke").slice(0, 2);
    assert.deepEqual([start.dash, goal.dash.length > 0], [[], true], "a met box is solid, an unmet one dashed");
  }
});

test("in the air, spikes and portals take their own color; paused, they go neutral again", () => {
  const colors = (game) => {
    const ctx = canvas();
    drawLevel(ctx, { state: { level: level(), game }, width: 1280, height: 800, color: "#ffffff" });
    return new Set(ctx.draws.map((d) => (d.kind === "stroke" ? d.strokeStyle : d.fillStyle)));
  };
  assert.deepEqual(colors({ camera_paused: true, paused: false }), new Set(["#ffffff", SPIKES_COLOR, PORTAL_COLOR]));
  assert.deepEqual(colors({ camera_paused: true, paused: true }), new Set(["#ffffff"]));
});

test("the banner names the new level in the middle of the wall", () => {
  const ctx = canvas();
  drawLevel(ctx, { state: { level: level() }, width: 1280, height: 800, color: "#ffffff", bannerAgeMs: 1000 });
  const texts = ctx.draws.filter((d) => d.kind === "text").map((d) => d.text);
  assert.ok(texts.includes("NIVEL 5") && texts.includes("Portal") && texts.includes(level().hint));
});

test("what to fix replaces the play hint at the bottom, never during the fill check", () => {
  const state = { level: level(), game: { status: "ready", hands_free: { enabled: true, armed: false } } };
  const ctx = canvas();
  drawProjectorScene(ctx, { state, width: 1280, height: 800 });
  const texts = ctx.draws.filter((d) => d.kind === "text").map((d) => d.text);
  assert.ok(texts.includes(level().problem));
  assert.ok(!texts.some((t) => t.startsWith("Tapá el inicio")));
  const solved = canvas();
  drawProjectorScene(solved, { state: { ...state, level: level({ problem: null }) }, width: 1280, height: 800 });
  assert.ok(solved.draws.some((d) => d.kind === "text" && d.text.startsWith("Tapá el inicio")));
  const check = canvas();
  drawProjectorScene(check, { state, width: 1280, height: 800, forceFill: true });
  assert.equal(check.draws.filter((d) => d.kind === "text" && d.text.startsWith("INICIO")).length, 0);
  assert.equal(drawLevelProblem(canvas(), { problem: null }, 10, 10, "#fff"), false);
});

test("the panel lists the levels with a check on the won ones and says how the wall stands", () => {
  assert.deepEqual(levelOptions(level()), [
    { value: "libre", label: "Libre (sin recuadros)" }, { value: "5", label: "5 · Portal ✓" },
    { value: "6", label: "6 · Doble salto" }]);
  assert.equal(levelStatus(level()),
    "Inicio ✓ · Llegada ✗ · Rebotes 2 de 1. Poné el post-it de llegada dentro de su recuadro.");
  assert.equal(levelStatus(level({ check: null })), `${level().hint} La cámara todavía no ve la pared.`);
  assert.equal(levelStatus({ id: "libre" }), "Modo libre: sin recuadros ni límite de rebotes.");
  assert.deepEqual(levelOptions(null), []);
});

test("a note is marked in its mark color, else its detection color, else the fallback", () => {
  const palette = { roles: { goal: { hex: "#ff9b49", mark: "#00ffaa" }, start: { hex: "#f7d84b" }, bumper: { hex: "nope" } } };
  assert.equal(markColor(palette, "goal"), "#00ffaa");
  assert.equal(markColor(palette, "start"), "#f7d84b");
  assert.equal(markColor(palette, "bumper"), FALLBACK_COLORS.bumper);
  assert.equal(markColor(null, "marker"), FALLBACK_COLORS.marker);
});

test("aligned, a paper glows in its mark color", () => {
  const H = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  const state = { alignment: { status: "ready", matrix: H, projector: { width: 1280, height: 800 },
    camera: { width: 1280, height: 800 } } };
  const preview = { width: 1280, height: 800, roles: { goal: { notes: [rect(300, 300, 400, 400)] } } };
  const palette = { roles: { goal: { hex: "#ff9b49", mark: "#00ffaa" } } };
  const ctx = canvas();
  drawProjectorScene(ctx, { state, preview, palette, width: 1280, height: 800 });
  assert.ok(ctx.draws.some((d) => d.kind === "fill" && d.fillStyle === "#00ffaa"));
  assert.ok(!ctx.draws.some((d) => d.fillStyle === "#ff9b49" || d.strokeStyle === "#ff9b49"));
});
