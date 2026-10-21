/**
 * Eval: what the projector emits for real detector samples on every screen
 * shape, both themes, unaligned and aligned (docs/alignment-and-color.md 2.1).
 *
 * Expectations are checked geometrically and independently of the renderer:
 * camera px are mapped with this file's own contain and homography math,
 * and each rim (fill) edge must lie exactly RIM_OFFSET_PX outside
 * (FILL_INSET_PX inside) the mapped paper edge.
 *
 * Ghosts: with the paper removed, the camera re-detects the projected fill
 * itself, grown by a blur `bleed` (projector px) and with corner jitter of
 * up to half a camera pixel. Each frame's fill must be strictly smaller and
 * the ghost must disappear.
 *
 * Patterns (1.2): for the contract 1.1 layout of every screen (computed
 * here, independently of any backend), the warm-up and the 14 frames are
 * drawn and decoded back the way 1.3 reads them: a dot's bit is 1 when it is
 * lit in the plain frame and dark in the inverse one.
 *
 * Pass: every case passes (score 1), the largest H mapping error is at most
 * 1e-6 px, every ghost dies within GHOST_FRAME_LIMIT frames, every pattern
 * dot decodes to its own id, and lit dots stay PATTERN_MIN_GAP_RADII radii
 * apart and fully inside the viewport.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { invertHomography, offsetConvexPolygon, polygonArea } from "../ui/ui-core.mjs";
import {
  alignmentCode,
  drawAlignmentPattern,
  drawProjectorScene,
  FILL_ALPHA,
  FILL_INSET_PX,
  HINT_MARGIN_PX,
  MIN_FILL_AREA_PX,
  NEUTRAL,
  PATTERN_BACKGROUND,
  PATTERN_DOT,
  RIM_OFFSET_PX,
  UNALIGNED_HINT,
} from "../ui/live-view.mjs";

const { samples } = JSON.parse(readFileSync(new URL("../contract/preview-samples.json", import.meta.url), "utf8"));
const { phases } = JSON.parse(readFileSync(new URL("../contract/state-matrix.json", import.meta.url), "utf8"));
const roles = ["start", "goal", "bumper", "marker"];
const screens = [[1280, 720], [1920, 1080], [1280, 800], [800, 600], [600, 1000], [2560, 720]];
const themes = ["dark", "light"];
const MAPPING_TOLERANCE_PX = 1e-6;
const GHOST_FRAME_LIMIT = 40;
const GHOST_BLEEDS = [0, 1, 2, 3];
const PATTERN_SCREENS = [...screens, [320, 240], [1024, 768], [1366, 768], [3840, 2160]];
/** Two separate blobs per neighbor pair after blur: at least two radii of black between lit dots. */
const PATTERN_MIN_GAP_RADII = 2;

/** Records every draw with the full state that decides what light it emits. */
function recordingCanvas() {
  const keys = ["fillStyle", "strokeStyle", "globalAlpha", "shadowBlur", "shadowColor", "shadowOffsetX", "shadowOffsetY",
    "lineWidth", "lineJoin", "miterLimit", "filter", "globalCompositeOperation"];
  const stack = [];
  const draws = [];
  let path = [];
  const snapshot = (ctx) => Object.fromEntries(keys.map((key) => [key, ctx[key]]));
  return {
    draws, fillStyle: "#000000", strokeStyle: "#000000", globalAlpha: 1, shadowBlur: 0, shadowColor: "rgba(0, 0, 0, 0)",
    shadowOffsetX: 0, shadowOffsetY: 0, lineWidth: 1, lineJoin: "miter", miterLimit: 10, filter: "none",
    globalCompositeOperation: "source-over",
    save() { stack.push(snapshot(this)); }, restore() { Object.assign(this, stack.pop()); },
    beginPath() { path = []; }, closePath() {},
    moveTo(x, y) { path.push({ x, y }); }, lineTo(x, y) { path.push({ x, y }); },
    arc(x, y, radius) { path.push({ x, y, radius }); },
    fill() { draws.push({ kind: "fill", points: path, ...snapshot(this) }); },
    stroke() { draws.push({ kind: "stroke", points: path, ...snapshot(this) }); },
    fillText(text, x, y) { draws.push({ kind: "text", text, x, y, ...snapshot(this) }); },
    fillRect(...rect) { draws.push({ kind: "rect", rect, ...snapshot(this) }); }, drawImage() { draws.push({ kind: "image" }); },
    rect() {}, clip() {},
  };
}

// --- Independent geometry. ---

/** Solves the 8 x 8 system of a 4-point homography (Gaussian elimination, partial pivoting). */
function homographyFromQuads(source, target) {
  const rows = [];
  source.forEach(([x, y], i) => {
    const [u, v] = target[i];
    rows.push([x, y, 1, 0, 0, 0, -u * x, -u * y, u]);
    rows.push([0, 0, 0, x, y, 1, -v * x, -v * y, v]);
  });
  for (let column = 0; column < 8; column++) {
    let pivot = column;
    for (let row = column + 1; row < 8; row++) if (Math.abs(rows[row][column]) > Math.abs(rows[pivot][column])) pivot = row;
    [rows[column], rows[pivot]] = [rows[pivot], rows[column]];
    for (let row = 0; row < 8; row++) {
      if (row === column) continue;
      const factor = rows[row][column] / rows[column][column];
      for (let k = column; k < 9; k++) rows[row][k] -= factor * rows[column][k];
    }
  }
  const h = rows.map((row, i) => row[8] / row[i]);
  return [[h[0], h[1], h[2]], [h[3], h[4], h[5]], [h[6], h[7], 1]];
}
const project = (m, p) => {
  const w = m[2][0] * p.x + m[2][1] * p.y + m[2][2];
  return { x: (m[0][0] * p.x + m[0][1] * p.y + m[0][2]) / w, y: (m[1][0] * p.x + m[1][1] * p.y + m[1][2]) / w };
};
/** Length stretch by finite differences, independent of the closed form under test. */
const stretch = (m, p) => {
  const step = 1e-3;
  const dx = [project(m, { x: p.x + step, y: p.y }), project(m, { x: p.x - step, y: p.y })];
  const dy = [project(m, { x: p.x, y: p.y + step }), project(m, { x: p.x, y: p.y - step })];
  const jx = [(dx[0].x - dx[1].x) / (2 * step), (dx[0].y - dx[1].y) / (2 * step)];
  const jy = [(dy[0].x - dy[1].x) / (2 * step), (dy[0].y - dy[1].y) / (2 * step)];
  return Math.sqrt(Math.abs(jx[0] * jy[1] - jx[1] * jy[0]));
};
const contain = (frameWidth, frameHeight, width, height) => {
  const scale = Math.min(width / frameWidth, height / frameHeight);
  const left = (width - frameWidth * scale) / 2;
  const top = (height - frameHeight * scale) / 2;
  return { scale, map: (p) => ({ x: left + p.x * scale, y: top + p.y * scale }) };
};
const signedArea = (points) => points.reduce((sum, p, i) => {
  const q = points[(i + 1) % points.length];
  return sum + (p.x * q.y - q.x * p.y) / 2;
}, 0);
/** Distance from `point` to the line of edge i of `polygon`, positive outward. */
const outwardDistance = (polygon, i, point) => {
  const a = polygon[i];
  const b = polygon[(i + 1) % polygon.length];
  const cross = (b.y - a.y) * (point.x - a.x) - (b.x - a.x) * (point.y - a.y);
  return Math.sign(signedArea(polygon)) * cross / Math.hypot(b.x - a.x, b.y - a.y);
};
let worstMappingError = 0;
/** Each output edge lies on the paper edge's line moved `distance` outward; returns the worst error. */
function assertOffset(output, paper, distance, label) {
  assert.equal(output.length, paper.length, `${label}: one vertex per paper corner`);
  let worst = 0;
  for (let i = 0; i < paper.length; i++) {
    for (const vertex of [output[i], output[(i + 1) % output.length]]) {
      assert.ok(Number.isFinite(vertex.x) && Number.isFinite(vertex.y), `${label}: finite`);
      worst = Math.max(worst, Math.abs(outwardDistance(paper, i, vertex) - distance));
    }
  }
  assert.ok(worst <= MAPPING_TOLERANCE_PX, `${label}: edge off by ${worst} px`);
  worstMappingError = Math.max(worstMappingError, worst);
}
const plain = (draw) => draw.shadowBlur === 0 && draw.shadowOffsetX === 0 && draw.shadowOffsetY === 0
  && draw.filter === "none" && draw.globalCompositeOperation === "source-over";

/** Mild and strong keystone views of the camera frame, partly beyond the screen like a real camera. */
function matricesFor(frameWidth, frameHeight, width, height) {
  const corners = [[0, 0], [frameWidth, 0], [frameWidth, frameHeight], [0, frameHeight]];
  const mild = [[-0.04, -0.03], [1.03, -0.05], [1.05, 1.02], [-0.02, 1.04]];
  const keystone = [[0.06, -0.12], [0.95, 0.02], [1.08, 1.1], [-0.07, 0.93]];
  return Object.fromEntries([["mild", mild], ["keystone", keystone]].map(([name, quad]) =>
    [name, homographyFromQuads(corners, quad.map(([u, v]) => [u * width, v * height]))]));
}

const results = [];
function check(name, body) {
  try { body(); results.push({ name, passed: true }); }
  catch (error) { results.push({ name, passed: false, error: error.message }); }
}

check("solver:matches_opencv_known_mapping", () => {
  const golden = [[1.4302055525851982, 0.039997657951908544, 40.0],
    [-0.017906657822151072, 1.2721345739142345, 22.0], [-4.809443052232611e-05, 5.273835729423269e-05, 1.0]];
  const solved = homographyFromQuads([[0, 0], [799, 0], [799, 599], [0, 599]], [[40, 22], [1230, 8], [1215, 775], [62, 760]]);
  solved.forEach((row, r) => row.forEach((value, c) =>
    assert.ok(Math.abs(value - golden[r][c]) <= 1e-9 * Math.max(1, Math.abs(golden[r][c])), `H[${r}][${c}]`)));
});

for (const [name, { preview, palette }] of Object.entries(samples)) {
  const notes = roles.flatMap((role) => (preview.roles[role]?.notes ?? []).map((shape) => ({ role, shape })));
  for (const theme of themes) {
    const ink = NEUTRAL[theme];
    for (const [width, height] of screens) {
      const screen = `${theme}:${name}:${width}x${height}`;
      let unalignedDraws = null;
      check(`unaligned:${screen}`, () => {
        const ctx = recordingCanvas();
        drawProjectorScene(ctx, { state: { live_detection: preview, palette }, preview, palette, theme, width, height });
        unalignedDraws = ctx.draws;
        const strokes = ctx.draws.filter((d) => d.kind === "stroke");
        assert.equal(ctx.draws.filter((d) => d.kind === "fill").length, 0, "no fill without alignment");
        assert.equal(strokes.length, notes.length, "one rim per detected note");
        const fit = contain(preview.width, preview.height, width, height);
        strokes.forEach((stroke, i) => {
          assert.equal(stroke.strokeStyle, ink.rim);
          assert.ok(plain(stroke) && stroke.globalAlpha === 1 && stroke.lineWidth === 3, "plain 3 px rim");
          assert.deepEqual([stroke.lineJoin, stroke.miterLimit], ["miter", 2]);
          assertOffset(stroke.points, notes[i].shape.points.map(fit.map), RIM_OFFSET_PX, "contain rim");
        });
        const hint = ctx.draws.find((d) => d.kind === "text");
        assert.equal(hint?.text, UNALIGNED_HINT);
        assert.deepEqual([hint.x, hint.y, hint.fillStyle], [HINT_MARGIN_PX, height - HINT_MARGIN_PX, ink.rim]);
      });
      for (const [kind, matrix] of Object.entries(matricesFor(preview.width, preview.height, width, height))) {
        const alignment = (fill) => ({ status: "ready", matrix, projector: { width, height },
          camera: { label: "Cámara", width: preview.width, height: preview.height }, overlay: { status: fill ? "ok" : "unchecked", fill } });
        check(`aligned:${kind}:${screen}`, () => {
          const ctx = recordingCanvas();
          drawProjectorScene(ctx, { state: { alignment: alignment(false) }, preview, palette, theme, width, height });
          assert.equal(ctx.draws.filter((d) => d.kind === "text").length, 0, "no hint once aligned");
          // Unchecked: never a role color. Neutral white light inside each paper
          // in the dark theme (contract 2.1); nothing under the light flood.
          const fills = ctx.draws.filter((d) => d.kind === "fill");
          const neutralFill = theme === "dark" ? "#ffffff" : null;
          assert.ok(fills.every((d) => d.fillStyle === neutralFill && d.globalAlpha === 0.35), "only neutral fill");
          if (!neutralFill) assert.equal(fills.length, 0, "no fill under the light flood");
          const strokes = ctx.draws.filter((d) => d.kind === "stroke");
          assert.equal(strokes.length, notes.length);
          strokes.forEach((stroke, i) => {
            assert.ok(stroke.strokeStyle === ink.rim && plain(stroke) && stroke.globalAlpha === 1, "neutral plain rim");
            assertOffset(stroke.points, notes[i].shape.points.map((p) => project(matrix, p)), RIM_OFFSET_PX, "H rim");
          });
        });
        check(`aligned_fill:${kind}:${screen}`, () => {
          const ctx = recordingCanvas();
          drawProjectorScene(ctx, { state: { alignment: alignment(true) }, preview, palette, theme, width, height });
          const fills = ctx.draws.filter((d) => d.kind === "fill");
          assert.equal(fills.length, notes.length, "every real note is big enough for a fill");
          fills.forEach((fill, i) => {
            const paper = notes[i].shape.points.map((p) => project(matrix, p));
            assert.equal(fill.fillStyle, palette.roles[notes[i].role].hex);
            assert.ok(fill.globalAlpha === FILL_ALPHA && plain(fill), "translucent fill without halo");
            assertOffset(fill.points, paper, -FILL_INSET_PX, "fill inset");
            assert.ok(polygonArea(fill.points) >= MIN_FILL_AREA_PX);
          });
          for (const stroke of ctx.draws.filter((d) => d.kind === "stroke")) {
            assert.ok(stroke.strokeStyle === ink.rim && stroke.globalAlpha === 1 && plain(stroke));
          }
        });
        check(`resized:${kind}:${screen}`, () => {
          const ctx = recordingCanvas();
          const stale = { ...alignment(true), projector: { width: width + 1, height } };
          drawProjectorScene(ctx, { state: { live_detection: preview, palette, alignment: stale }, preview, palette, theme, width, height });
          assert.deepEqual(ctx.draws, unalignedDraws, "H of another viewport is never used");
        });
      }
    }
  }
}

for (const [name, { state }] of Object.entries(phases)) {
  if (!state.game?.position) continue;
  for (const theme of themes) {
    const ink = NEUTRAL[theme];
    for (const [width, height] of screens) {
      const frameWidth = state.game.width || state.camera.width;
      const frameHeight = state.game.height || state.camera.height;
      const matrix = matricesFor(frameWidth, frameHeight, width, height).keystone;
      const expectations = {
        camera_unaligned: { state, center: contain(frameWidth, frameHeight, width, height).map(state.game.position),
          radius: 10 * contain(frameWidth, frameHeight, width, height).scale },
        camera_aligned: { state: { ...state, alignment: { status: "ready", matrix, projector: { width, height } } },
          center: project(matrix, state.game.position), radius: 10 * stretch(matrix, state.game.position) },
        projector: { state: { ...state, game: { ...state.game, space: "projector", width, height } },
          center: state.game.position, radius: 10 },
      };
      // A phase whose game already runs in projector px (aligned_ready) has no
      // camera-space ball to re-map: only the projector expectation applies.
      const spaces = state.game.space === "projector"
        ? Object.entries(expectations).filter(([space]) => space === "projector") : Object.entries(expectations);
      for (const [space, expected] of spaces) {
        check(`ball:${space}:${theme}:${name}:${width}x${height}`, () => {
          const ctx = recordingCanvas();
          drawProjectorScene(ctx, { state: expected.state, theme, width, height });
          // The ball is the one arc fill; aligned dark scenes also light each
          // live paper with a neutral polygon fill (contract 2.1).
          const balls = ctx.draws.filter((d) => d.kind === "fill" && Number.isFinite(d.points?.[0]?.radius));
          assert.equal(balls.length, 1, "one ball, no frozen board polygons");
          assert.ok(ctx.draws.filter((d) => d.kind === "fill" && !Number.isFinite(d.points?.[0]?.radius))
            .every((d) => d.fillStyle === "#ffffff"), "any other fill is neutral paper light");
          const [{ x, y, radius }] = balls[0].points;
          assert.ok(Math.abs(x - expected.center.x) <= MAPPING_TOLERANCE_PX && Math.abs(y - expected.center.y) <= MAPPING_TOLERANCE_PX,
            `center ${x},${y} vs ${expected.center.x},${expected.center.y}`);
          assert.ok(Math.abs(radius - expected.radius) <= 1e-5 * expected.radius, `radius ${radius} vs ${expected.radius}`);
          for (const draw of ctx.draws) {
            assert.notEqual(draw.kind, "image");
            const color = draw.kind === "stroke" ? draw.strokeStyle : draw.fillStyle;
            assert.ok([ink.ball, ink.rim].includes(color) && plain(draw), `${draw.kind} ${color} is neutral and plain`);
          }
          const texts = ctx.draws.filter((d) => d.kind === "text").map((d) => d.text);
          if (state.game.status === "won") assert.ok(texts.includes("¡LLEGASTE!"));
          if (state.game.status === "lost") assert.ok(texts.includes("FUERA DEL CAMPO"));
        });
      }
    }
  }
}

/** Deterministic jitter in [-0.5, 0.5) camera px: corner precision of a re-detection. */
function jitter(seed) {
  let value = seed >>> 0 || 1;
  return () => {
    value ^= value << 13; value >>>= 0;
    value ^= value >>> 17;
    value ^= value << 5; value >>>= 0;
    return value / 2 ** 32 - 0.5;
  };
}

const ghostFrames = [];
const ghostByBleed = Object.fromEntries(GHOST_BLEEDS.map((bleed) => [bleed, 0]));
let ghostSeed = 1;
for (const [name, { preview, palette }] of Object.entries(samples)) {
  for (const role of roles) {
    (preview.roles[role]?.notes ?? []).forEach((shape, index) => {
      for (const [width, height] of screens) {
        for (const [kind, matrix] of Object.entries(matricesFor(preview.width, preview.height, width, height))) {
          const inverse = invertHomography(matrix);
          const state = { alignment: { status: "ready", matrix, projector: { width, height },
            camera: { width: preview.width, height: preview.height }, overlay: { status: "ok", fill: true } } };
          for (const bleed of GHOST_BLEEDS) {
            check(`ghost:${name}:${role}${index}:${kind}:${width}x${height}:bleed${bleed}`, () => {
              const random = jitter(ghostSeed++);
              let note = shape;
              let previous = Infinity;
              for (let frame = 0; frame <= GHOST_FRAME_LIMIT; frame++) {
                const ctx = recordingCanvas();
                drawProjectorScene(ctx, { state, preview: { width: preview.width, height: preview.height, roles: { [role]: { notes: [note] } } },
                  palette, width, height });
                const fill = ctx.draws.find((d) => d.kind === "fill");
                if (!fill) {
                  ghostFrames.push(frame);
                  ghostByBleed[bleed] = Math.max(ghostByBleed[bleed], frame);
                  return;
                }
                const area = polygonArea(fill.points);
                assert.ok(area < previous, `frame ${frame}: area ${area.toFixed(2)} did not shrink from ${previous.toFixed(2)}`);
                previous = area;
                // The paper is gone: the camera sees the fill, blurred `bleed` px wider, corners jittered.
                const seen = bleed ? offsetConvexPolygon(fill.points, bleed) : fill.points;
                assert.ok(seen, "a blurred fill is still a polygon");
                note = { points: seen.map((p) => {
                  const camera = project(inverse, p);
                  return { x: camera.x + random(), y: camera.y + random() };
                }) };
              }
              assert.fail(`the ghost survived ${GHOST_FRAME_LIMIT} frames`);
            });
          }
        }
      }
    });
  }
}

// --- Alignment patterns (1.1 layout, 1.2 drawing). ---

/** Python's round(): half to even. */
const pyRound = (value) => {
  const floor = Math.floor(value);
  const rest = value - floor;
  if (rest !== 0.5) return rest < 0.5 ? floor : floor + 1;
  return floor % 2 === 0 ? floor : floor + 1;
};
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
function contractLayout(width, height) {
  const cols = clamp(pyRound(Math.sqrt(60 * width / height)), 4, 12);
  let rows = clamp(pyRound(60 / cols), 3, 9);
  while (cols * rows > 63) rows -= 1;
  const margin = 0.06;
  const dots = [];
  for (let j = 0; j < rows; j++) {
    for (let i = 0; i < cols; i++) {
      dots.push({ id: j * cols + i + 1, x: width * (margin + (1 - 2 * margin) * i / (cols - 1)),
        y: height * (margin + (1 - 2 * margin) * j / (rows - 1)) });
    }
  }
  const frames = Array.from({ length: 14 }, (_, k) => ({ bit: k >> 1, inverse: Boolean(k & 1) }));
  return { version: 1, width, height, cols, rows, radius: Math.max(4.0, 0.012 * Math.min(width, height)), bits: 7, dots, frames };
}

const patternStats = { layouts: 0, frames: 0, dots: 0, decoded: 0, min_gap_radii: Infinity, lit_fraction: [Infinity, 0] };
for (const [width, height] of PATTERN_SCREENS) {
  for (const theme of themes) {
    check(`pattern:${theme}:${width}x${height}`, () => {
      const layout = contractLayout(width, height);
      const lit = new Map(layout.dots.map((dot) => [dot.id, new Set()]));
      const render = (frame) => {
        const ctx = recordingCanvas();
        ctx.fillStyle = theme === "light" ? "#f3f2ec" : "#000000"; // whatever the theme painted before
        ctx.shadowBlur = 12;
        ctx.globalAlpha = 0.5;
        const count = drawAlignmentPattern(ctx, layout, frame, width, height);
        const [cover, ...rest] = ctx.draws;
        assert.ok(cover.kind === "rect" && cover.fillStyle === PATTERN_BACKGROUND && plain(cover) && cover.globalAlpha === 1,
          "pure black over the whole viewport first");
        assert.deepEqual(cover.rect, [0, 0, width, height]);
        assert.equal(rest.length, count);
        return rest;
      };
      const warm = render("warmup");
      for (const frame of layout.frames) {
        const dots = render(frame);
        if (frame.bit === 0 && !frame.inverse) assert.deepEqual(warm, dots, "warm-up is the {bit 0, plain} frame");
        patternStats.frames += 1;
        const fraction = dots.length / layout.dots.length;
        patternStats.lit_fraction = [Math.min(patternStats.lit_fraction[0], fraction), Math.max(patternStats.lit_fraction[1], fraction)];
        for (const dot of dots) {
          assert.ok(dot.kind === "fill" && dot.fillStyle === PATTERN_DOT && dot.globalAlpha === 1 && plain(dot), "plain white dot");
          const [{ x, y, radius }] = dot.points;
          assert.equal(radius, layout.radius);
          assert.ok(x - radius >= 0 && y - radius >= 0 && x + radius <= width && y + radius <= height, "fully inside the viewport");
          const source = layout.dots.find((candidate) => candidate.x === x && candidate.y === y);
          assert.ok(source, "drawn exactly at a layout dot");
          lit.get(source.id).add(`${frame.bit}:${frame.inverse}`);
        }
        for (let a = 0; a < dots.length; a++) {
          for (let b = a + 1; b < dots.length; b++) {
            const gap = Math.hypot(dots[a].points[0].x - dots[b].points[0].x, dots[a].points[0].y - dots[b].points[0].y) - 2 * layout.radius;
            patternStats.min_gap_radii = Math.min(patternStats.min_gap_radii, gap / layout.radius);
            assert.ok(gap >= PATTERN_MIN_GAP_RADII * layout.radius, `lit dots ${gap.toFixed(1)} px apart`);
          }
        }
      }
      // 1.3 steps 6-7 on ideal frames: bit b = lit in plain and dark in inverse; parity; id.
      for (const dot of layout.dots) {
        const frames = lit.get(dot.id);
        let code = 0;
        for (let bit = 0; bit < 7; bit++) {
          const plainLit = frames.has(`${bit}:false`);
          assert.notEqual(plainLit, frames.has(`${bit}:true`), `dot ${dot.id} lit in exactly one frame of pair ${bit}`);
          if (plainLit) code |= 1 << bit;
        }
        const ones = code.toString(2).split("").filter((digit) => digit === "1").length;
        assert.equal(ones % 2, 0, "even parity");
        assert.equal(code, alignmentCode(dot.id));
        assert.equal(code & 63, dot.id, "decodes to its own id");
        patternStats.decoded += 1;
      }
      patternStats.dots += layout.dots.length;
      patternStats.layouts += 1;
    });
  }
}

const failed = results.filter((result) => !result.passed);
const score = (results.length - failed.length) / results.length;
const ghosts = {
  cases: ghostFrames.length,
  max_frames: Math.max(...ghostFrames),
  mean_frames: Number((ghostFrames.reduce((sum, n) => sum + n, 0) / ghostFrames.length).toFixed(2)),
  max_frames_by_bleed_px: ghostByBleed,
  frame_limit: GHOST_FRAME_LIMIT,
};
const patterns = {
  ...patternStats,
  min_gap_radii: Number(patternStats.min_gap_radii.toFixed(2)),
  lit_fraction: patternStats.lit_fraction.map((value) => Number(value.toFixed(3))),
  min_gap_threshold_radii: PATTERN_MIN_GAP_RADII,
};
const passed = score === 1 && worstMappingError <= MAPPING_TOLERANCE_PX && ghostFrames.every((n) => n <= GHOST_FRAME_LIMIT)
  && patternStats.decoded === patternStats.dots && patternStats.dots > 0 && patternStats.min_gap_radii >= PATTERN_MIN_GAP_RADII;
console.log(JSON.stringify({
  evaluation: "live_projection", score, threshold: 1, cases: results.length, passed: results.length - failed.length,
  max_mapping_error_px: worstMappingError, mapping_tolerance_px: MAPPING_TOLERANCE_PX, ghosts, patterns,
  failed: failed.slice(0, 20), failed_total: failed.length,
}, null, 2));
if (!passed) process.exitCode = 1;
