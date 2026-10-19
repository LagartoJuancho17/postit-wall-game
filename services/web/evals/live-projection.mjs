/** Real detector samples must survive every projector aspect ratio unchanged. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { drawLiveDetection, drawLiveGame } from "../ui/live-view.mjs";

const { samples } = JSON.parse(readFileSync(new URL("../contract/preview-samples.json", import.meta.url), "utf8"));
const { phases } = JSON.parse(readFileSync(new URL("../contract/state-matrix.json", import.meta.url), "utf8"));
const roles = ["start", "goal", "bumper", "marker"];
const screens = [[1280, 720], [1920, 1080], [1280, 800], [800, 600], [600, 1000], [2560, 720]];

function recordingCanvas() {
  let path = [];
  const draws = [];
  const strokes = [];
  const ctx = {
    draws, strokes, save() {}, restore() {},
    beginPath() { path = []; }, closePath() {},
    moveTo(x, y) { path.push([x, y]); }, lineTo(x, y) { path.push([x, y]); },
    arc(x, y, radius) { path.push([x, y, radius]); },
    fill() { draws.push({ points: path, color: this.fillStyle }); },
    stroke() { strokes.push({ points: path, color: this.strokeStyle }); }, fillText() {},
  };
  return ctx;
}

const results = [];
function contrastRatio(a, b) {
  const luminance = (hex) => hex.slice(1).match(/../g)
    .map((value) => parseInt(value, 16) / 255)
    .map((value) => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4)
    .reduce((sum, value, i) => sum + value * [.2126, .7152, .0722][i], 0);
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + .05) / (Math.min(x, y) + .05);
}
function check(name, body) {
  try { body(); results.push({ name, passed: true }); }
  catch (error) { results.push({ name, passed: false, error: error.message }); }
}

for (const [name, { preview, palette }] of Object.entries(samples)) {
  for (const theme of ["dark", "light"]) {
  for (const [width, height] of screens) {
    check(`${theme}:${name}:${width}x${height}`, () => {
      const ctx = recordingCanvas();
      drawLiveDetection(ctx, preview, palette, width, height, theme);
      const notes = roles.flatMap((role) => (preview.roles[role]?.notes ?? []).map((shape) => ({ role, shape })));
      assert.equal(ctx.draws.length, notes.length, "every detected note gets exactly one halo");
      assert.equal(ctx.strokes.length, notes.length, "every note has one visible rim");
      for (const stroke of ctx.strokes) {
        assert.ok(contrastRatio(stroke.color, theme === "light" ? "#f3f2ec" : "#000000") >= 4,
          "rim remains visible against the projector background");
      }
      for (let i = 0; i < notes.length; i++) {
        const { role, shape } = notes[i];
        const draw = ctx.draws[i];
        assert.equal(draw.color, palette.roles[role].hex, `${role} keeps its palette color`);
        assert.equal(draw.points.length, shape.points.length);
        const scale = Math.min(width / preview.width, height / preview.height);
        const left = (width - preview.width * scale) / 2;
        const top = (height - preview.height * scale) / 2;
        for (let j = 0; j < shape.points.length; j++) {
          const [x, y] = draw.points[j];
          assert.ok(Number.isFinite(x) && Number.isFinite(y));
          assert.ok(x >= 0 && x <= width && y >= 0 && y <= height, "no crop");
          assert.ok(Math.abs((x - left) / scale - shape.points[j].x) < 1e-6, "horizontal camera coordinate preserved");
          assert.ok(Math.abs((y - top) / scale - shape.points[j].y) < 1e-6, "vertical camera coordinate preserved");
        }
      }
    });
  }
  }
}

for (const [name, { state }] of Object.entries(phases)) {
  if (!state.game?.position) continue;
  for (const [width, height] of screens) {
    check(`ball:${name}:${width}x${height}`, () => {
      const ctx = recordingCanvas();
      drawLiveGame(ctx, state.game, state.camera.width, state.camera.height, width, height);
      assert.equal(ctx.draws.length, 1, "one ball, no frozen board polygons");
      const frameWidth = state.game.width || state.camera.width;
      const frameHeight = state.game.height || state.camera.height;
      const scale = Math.min(width / frameWidth, height / frameHeight);
      const [x, y, radius] = ctx.draws[0].points[0];
      assert.ok(Math.abs(x - ((width - frameWidth * scale) / 2 + state.game.position.x * scale)) < 1e-6);
      assert.ok(Math.abs(y - ((height - frameHeight * scale) / 2 + state.game.position.y * scale)) < 1e-6);
      assert.ok(radius > 0 && Number.isFinite(radius));
    });
  }
}

const failed = results.filter((result) => !result.passed);
const score = (results.length - failed.length) / results.length;
console.log(JSON.stringify({ evaluation: "live_projection", score, threshold: 1, cases: results.length,
  passed: results.length - failed.length, failed }, null, 2));
if (score !== 1) process.exitCode = 1;
