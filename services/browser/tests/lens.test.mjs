/** Gate tests: services/browser/lens.mjs, the lens model of contract 1.3.1 (parity.test.mjs checks it bit for bit). */
import assert from "node:assert/strict";
import test from "node:test";

import { distort, fitLens, LENS_K_LIMIT, lensFor, undistort, validLens } from "../lens.mjs";
import { lensLocalScale, undistortPoint } from "../../web/ui/ui-core.mjs";

test("lensFor centers the model and normalizes by half the diagonal", () => {
  assert.deepEqual(lensFor(640, 480, 0.05), { k: 0.05, cx: 320, cy: 240, r: 400 });
});

test("null is the identity, the center stays and corners stretch by 1 + k", () => {
  const point = { x: 12.5, y: -3 };
  assert.equal(undistort(point, null), point);
  assert.equal(distort(point, null), point);
  const lens = lensFor(640, 480, 0.1);
  assert.deepEqual(undistort({ x: 320, y: 240 }, lens), { x: 320, y: 240 });
  const corner = undistort({ x: 0, y: 0 }, lens);
  assert.ok(Math.abs(corner.x - (320 - 320 * 1.1)) < 1e-9 && Math.abs(corner.y - (240 - 240 * 1.1)) < 1e-9);
});

test("distort inverts undistort over the whole frame for every allowed k", () => {
  for (const k of [-LENS_K_LIMIT, -0.1, 0, 0.03, LENS_K_LIMIT]) {
    const lens = lensFor(1280, 720, k);
    for (let x = 0; x <= 1280; x += 160) {
      for (let y = 0; y <= 720; y += 120) {
        const back = distort(undistort({ x, y }, lens), lens);
        assert.ok(Math.hypot(back.x - x, back.y - y) < 1e-6, `k=${k} (${x}, ${y})`);
      }
    }
  }
});

test("validLens accepts only finite, bounded lenses", () => {
  assert.ok(validLens(lensFor(640, 480, 0.2)));
  for (const bad of [null, {}, { k: 0.1, cx: 1, cy: 1 }, lensFor(640, 480, 0.3), { ...lensFor(640, 480, 0.1), r: 0 },
    { ...lensFor(640, 480, 0.1), k: NaN }, { ...lensFor(640, 480, 0.1), cx: "a" }]) {
    assert.equal(validLens(bad), false, JSON.stringify(bad));
  }
});

test("fitLens finds the minimum, returns 0 when nothing fits, and keeps the earliest tie", () => {
  for (const target of [-0.2, -0.0337, 0, 0.0123, 0.07, 0.249]) {
    assert.ok(Math.abs(fitLens((k) => (k - target) ** 2) - target) < 1e-9, `target ${target}`);
  }
  assert.equal(fitLens(() => Infinity), 0);
  assert.equal(fitLens(() => 1), -LENS_K_LIMIT);
});

test("the projector UI straightens exactly like the backend, and its stretch is the Jacobian's", () => {
  const lens = lensFor(1280, 720, 0.07);
  for (const point of [{ x: 0, y: 0 }, { x: 640, y: 360 }, { x: 1100, y: 90 }, { x: 37.5, y: 700 }]) {
    assert.deepEqual(undistortPoint(point, lens), undistort(point, lens));
    // sqrt|det J| by central differences.
    const h = 1e-3;
    const fx = (dx, dy) => undistort({ x: point.x + dx, y: point.y + dy }, lens);
    const [a, b] = [fx(h, 0), fx(-h, 0)];
    const [c, d] = [fx(0, h), fx(0, -h)];
    const J = [[(a.x - b.x) / (2 * h), (c.x - d.x) / (2 * h)], [(a.y - b.y) / (2 * h), (c.y - d.y) / (2 * h)]];
    const expected = Math.sqrt(Math.abs(J[0][0] * J[1][1] - J[0][1] * J[1][0]));
    assert.ok(Math.abs(lensLocalScale(point, lens) - expected) < 1e-6, `${JSON.stringify(point)}`);
  }
  assert.equal(lensLocalScale({ x: 3, y: 4 }, null), 1);
});
