/**
 * Gate tests: the projector's geometry helpers and the coded alignment
 * pattern (docs/alignment-and-color.md 1.1, 1.2 and 2.1).
 *
 * Golden values come from OpenCV 4.12 / numpy 2.2 in .venv:
 * H = cv2.getPerspectiveTransform of the runtime fixture's known_mapping
 * corners, normalized to h22 == 1; mapped points from
 * cv2.perspectiveTransform (float64); the inverse from np.linalg.inv.
 */
import assert from "node:assert/strict";
import test from "node:test";

import {
  applyHomography,
  convexHull,
  homographyLocalScale,
  invertHomography,
  isHomography,
  offsetConvexPolygon,
  polygonArea,
  PROJECTOR_THEMES,
} from "../ui/ui-core.mjs";
import {
  ALIGNMENT_BITS,
  alignmentCode,
  drawAlignmentPattern,
  PATTERN_BACKGROUND,
  PATTERN_DOT,
  WARMUP_FRAME,
} from "../ui/live-view.mjs";

const H = [
  [1.4302055525851982, 0.039997657951908544, 40.0],
  [-0.017906657822151072, 1.2721345739142345, 22.0],
  [-4.809443052232611e-05, 5.273835729423269e-05, 1.0],
];
const H_INVERSE = [
  [0.6982876065910595, -0.020816161714096632, -27.473548705932256],
  [0.009256799739631089, 0.7868279778686426, -17.680487502695378],
  [3.3095556367725275e-05, -4.24971564692352e-05, 1.0],
];
const GOLDEN = [
  [{ x: 0, y: 0 }, { x: 40.0, y: 22.0 }],
  [{ x: 799, y: 599 }, { x: 1214.9999999999998, y: 774.9999999999999 }],
  [{ x: 405, y: 190 }, { x: 632.8179686120018, y: 258.90205701240984 }],
  [{ x: 155.5, y: 315.25 }, { x: 272.51352124687855, y: 416.44666682910696 }],
  [{ x: 650, y: 310 }, { x: 996.8991305418423, y: 410.8491741513754 }],
];
const near = (actual, expected, tolerance, label = "") =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${label} ${actual} != ${expected} (±${tolerance})`);
const nearPoint = (actual, expected, tolerance, label = "") => {
  near(actual.x, expected.x, tolerance, `${label} x`);
  near(actual.y, expected.y, tolerance, `${label} y`);
};
const rect = (x, y, w, h) => [{ x, y }, { x: x + w, y }, { x: x + w, y: y + h }, { x, y: y + h }];
const signedArea = (points) => points.reduce((sum, p, i) => {
  const q = points[(i + 1) % points.length];
  return sum + (p.x * q.y - q.x * p.y) / 2;
}, 0);
/** Signed distance from a point to the line a->b: positive outward when the signed area is positive. */
const lineDistance = (point, a, b) =>
  ((b.y - a.y) * (point.x - a.x) - (b.x - a.x) * (point.y - a.y)) / Math.hypot(b.x - a.x, b.y - a.y);

test("applyHomography matches cv2.perspectiveTransform", () => {
  for (const [camera, projector] of GOLDEN) nearPoint(applyHomography(H, camera), projector, 1e-9, JSON.stringify(camera));
  const identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
  assert.deepEqual(applyHomography(identity, { x: 3.5, y: -2 }), { x: 3.5, y: -2 });
});

test("applyHomography refuses points at infinity and malformed input", () => {
  const horizon = [[1, 0, 0], [0, 1, 0], [0.01, 0, 1]]; // w = 0.01 x + 1 vanishes at x = -100
  assert.equal(applyHomography(horizon, { x: -100, y: 5 }), null);
  for (const bad of [null, [], [[1, 0, 0], [0, 1, 0]], [[1, 0, 0], [0, 1, 0], [0, 0, NaN]], [[1, 2, 3], [2, 4, 6], [0, 0, 1]]]) {
    assert.equal(applyHomography(bad, { x: 1, y: 1 }), null, JSON.stringify(bad));
    assert.equal(isHomography(bad), false);
  }
  assert.equal(applyHomography(H, { x: NaN, y: 1 }), null);
  assert.equal(applyHomography(H, null), null);
  assert.equal(isHomography(H), true);
});

test("invertHomography is the exact inverse, normalized to [2][2] == 1", () => {
  const inverse = invertHomography(H);
  assert.equal(inverse[2][2], 1);
  for (let row = 0; row < 3; row++) {
    for (let column = 0; column < 3; column++) {
      const expected = H_INVERSE[row][column];
      near(inverse[row][column], expected, 1e-12 * Math.max(1, Math.abs(expected)), `inv[${row}][${column}]`);
    }
  }
  for (const [camera, projector] of GOLDEN) {
    nearPoint(applyHomography(inverse, projector), camera, 1e-9, "round trip projector -> camera");
    nearPoint(applyHomography(inverse, applyHomography(H, camera)), camera, 1e-9, "round trip");
  }
  // Any scale of H has the same normalized inverse.
  const scaled = H.map((row) => row.map((value) => value * -3.5));
  invertHomography(scaled).forEach((row, r) => row.forEach((value, c) => near(value, inverse[r][c], 1e-12 * Math.max(1, Math.abs(value)))));
});

test("invertHomography returns null for singular or unnormalizable matrices", () => {
  assert.equal(invertHomography([[1, 2, 3], [2, 4, 6], [7, 8, 9]]), null, "rank 2");
  assert.equal(invertHomography([[0, 1, 0], [1, 0, 0], [0, 0, 1]])?.[2][2], 1, "a swap is invertible");
  // The inverse of this matrix has [2][2] == 0: the projector origin is at infinity in the camera.
  assert.equal(invertHomography([[0, 0, 1], [0, 1, 0], [1, 0, 0]]), null);
  assert.equal(invertHomography("H"), null);
});

test("polygonArea is the same for both orientations", () => {
  const box = rect(10, 20, 60, 40);
  assert.equal(polygonArea(box), 2400);
  assert.equal(polygonArea([...box].reverse()), 2400);
  assert.equal(polygonArea([{ x: 0, y: 0 }, { x: 4, y: 0 }, { x: 0, y: 3 }]), 6);
  for (const bad of [null, [], [{ x: 0, y: 0 }, { x: 1, y: 1 }], [{ x: 0, y: 0 }, { x: 1, y: NaN }, { x: 2, y: 0 }]]) {
    assert.equal(polygonArea(bad), 0);
  }
});

test("offsetConvexPolygon moves every edge outward for clockwise and counter-clockwise quads", () => {
  const box = rect(100, 50, 40, 40);
  for (const points of [box, [...box].reverse()]) {
    const out = offsetConvexPolygon(points, 5);
    assert.equal(out.length, 4);
    assert.equal(Math.sign(signedArea(out)), Math.sign(signedArea(points)), "orientation kept");
    near(polygonArea(out), 50 * 50, 1e-9, "outward area");
    for (let i = 0; i < 4; i++) {
      // Vertex i moved diagonally away from the center by 5 px on each axis.
      near(Math.abs(out[i].x - 120), 25, 1e-9);
      near(Math.abs(out[i].y - 70), 25, 1e-9);
      assert.equal(Math.sign(out[i].x - 120), Math.sign(points[i].x - 120));
      assert.equal(Math.sign(out[i].y - 70), Math.sign(points[i].y - 70));
    }
    const inward = offsetConvexPolygon(points, -5);
    near(polygonArea(inward), 30 * 30, 1e-9, "inward area");
    assert.equal(Math.sign(signedArea(inward)), Math.sign(signedArea(points)));
  }
});

test("each offset edge stays parallel to its source at exactly the distance (rotated, skewed quad)", () => {
  const quad = [{ x: 150, y: 320 }, { x: 150, y: 380 }, { x: 209, y: 381 }, { x: 211, y: 321 }]; // a real detection
  for (const points of [quad, [...quad].reverse()]) {
    const orientation = Math.sign(signedArea(points));
    for (const distance of [5, -5, 2.5, -12]) {
      const moved = offsetConvexPolygon(points, distance);
      assert.ok(moved, `offset ${distance}`);
      for (let i = 0; i < 4; i++) {
        const a = points[i];
        const b = points[(i + 1) % 4];
        // Outward is the right-hand side of a->b for a positive signed area.
        const expected = distance * orientation;
        near(lineDistance(moved[i], a, b), expected, 1e-9, `edge ${i} start`);
        near(lineDistance(moved[(i + 1) % 4], a, b), expected, 1e-9, `edge ${i} end`);
      }
    }
  }
});

test("an inset past the paper's half width degenerates to null instead of flipping", () => {
  const square = rect(0, 0, 10, 10);
  assert.ok(offsetConvexPolygon(square, -4.9));
  assert.equal(offsetConvexPolygon(square, -5), null, "zero area");
  assert.equal(offsetConvexPolygon(square, -6), null, "would flip orientation");
  const sliver = rect(0, 0, 80, 8);
  assert.equal(offsetConvexPolygon(sliver, -5), null, "the thin side collapses first");
  // A cut corner: its short edge collapses before the paper does.
  const cut = [{ x: 0, y: 0 }, { x: 40, y: 0 }, { x: 40, y: 38 }, { x: 38, y: 40 }, { x: 0, y: 40 }];
  assert.ok(offsetConvexPolygon(cut, -1));
  assert.equal(offsetConvexPolygon(cut, -5), null, "the 2.8 px edge would turn around");
});

test("offsetConvexPolygon refuses non-convex or degenerate input and ignores repeated or collinear points", () => {
  const arrow = [{ x: 0, y: 0 }, { x: 10, y: 5 }, { x: 0, y: 10 }, { x: 4, y: 5 }];
  assert.equal(offsetConvexPolygon(arrow, 5), null, "non-convex");
  const star = [0, 2, 4, 1, 3].map((k) => ({ x: Math.cos(k * 2 * Math.PI / 5), y: Math.sin(k * 2 * Math.PI / 5) }));
  assert.equal(offsetConvexPolygon(star, 1), null, "a pentagram turns the same way twice around");
  assert.equal(offsetConvexPolygon([{ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 2, y: 2 }], 1), null, "collinear");
  assert.equal(offsetConvexPolygon([{ x: 0, y: 0 }, { x: 1, y: 0 }], 1), null);
  assert.equal(offsetConvexPolygon(rect(0, 0, 10, 10), NaN), null);
  assert.equal(offsetConvexPolygon([{ x: 0, y: 0 }, { x: 1, y: NaN }, { x: 2, y: 0 }], 1), null);
  const messy = [{ x: 0, y: 0 }, { x: 5, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 10 }, { x: 0, y: 10 }, { x: 0, y: 0 }];
  const clean = offsetConvexPolygon(messy, 1);
  assert.equal(clean.length, 4, "midpoint, duplicate and closing point dropped");
  near(polygonArea(clean), 144, 1e-9);
});

test("a sharp corner keeps its pure miter: the stroke's miter limit is the stroke's business", () => {
  // Apex 30 degrees: the moved edges meet 5 / sin(15 deg) = 19.3 px from it.
  const half = Math.tan(15 * Math.PI / 180) * 100;
  const triangle = [{ x: 0, y: 0 }, { x: 100, y: -half }, { x: 100, y: half }];
  for (const points of [triangle, [...triangle].reverse()]) {
    const out = offsetConvexPolygon(points, 5);
    assert.equal(out.length, 3, "no bevel vertex");
    const apex = out[points.findIndex((p) => p.x === 0)];
    near(Math.hypot(apex.x, apex.y), 5 / Math.sin(15 * Math.PI / 180), 1e-9);
    near(apex.y, 0, 1e-9, "on the bisector");
  }
});

test("convexHull keeps the extreme points of any vertex order and drops the rest", () => {
  const box = rect(0, 0, 40, 30);
  const messy = [{ x: 20, y: 15 }, box[2], { x: 20, y: 0 }, box[0], box[0], box[3], { x: 40, y: 10 }, box[1]];
  const hull = convexHull(messy);
  assert.equal(hull.length, 4, "interior, duplicate and collinear points dropped");
  near(polygonArea(hull), 1200, 1e-12);
  for (const corner of box) assert.ok(hull.some((p) => p.x === corner.x && p.y === corner.y));
  const arrow = [{ x: 0, y: 0 }, { x: 10, y: 5 }, { x: 0, y: 10 }, { x: 4, y: 5 }];
  assert.deepEqual(convexHull(arrow).length, 3, "a concave vertex is not on the hull");
  assert.ok(offsetConvexPolygon(convexHull(arrow), 5), "the hull is a valid convex polygon to offset");
  for (const bad of [null, [], [{ x: 0, y: 0 }, { x: 1, y: 1 }], [{ x: 0, y: 0 }, { x: 1, y: 1 }, { x: 2, y: 2 }],
    [{ x: 1, y: 1 }, { x: 1, y: 1 }, { x: 1, y: 1 }], [{ x: 0, y: 0 }, { x: 1, y: NaN }, { x: 2, y: 0 }]]) {
    assert.equal(convexHull(bad), null, JSON.stringify(bad));
  }
});

test("homographyLocalScale is the length stretch of H around a point", () => {
  const scale = [[2.5, 0, 7], [0, 2.5, -3], [0, 0, 1]];
  near(homographyLocalScale(scale, { x: 13, y: 40 }), 2.5, 1e-12, "pure scale");
  const angle = 0.6;
  const rotation = [[Math.cos(angle), -Math.sin(angle), 0], [Math.sin(angle), Math.cos(angle), 0], [0, 0, 1]];
  near(homographyLocalScale(rotation, { x: -50, y: 9 }), 1, 1e-12, "rotation");
  const both = rotation.map((row, r) => row.map((value, c) => (r < 2 && c < 2 ? 2 * value : value)));
  near(homographyLocalScale(both, { x: 3, y: 4 }), 2, 1e-12, "rotation and scale");
  // Perspective: equals sqrt(|det H| / w^3) and the finite-difference Jacobian.
  const point = { x: 405, y: 190 };
  const w = H[2][0] * point.x + H[2][1] * point.y + H[2][2];
  const det = H[0][0] * (H[1][1] * H[2][2] - H[1][2] * H[2][1]) - H[0][1] * (H[1][0] * H[2][2] - H[1][2] * H[2][0])
    + H[0][2] * (H[1][0] * H[2][1] - H[1][1] * H[2][0]);
  near(homographyLocalScale(H, point), Math.sqrt(Math.abs(det / w ** 3)), 1e-12, "closed form");
  near(homographyLocalScale(H, point), 1.3687567710182134, 1e-7, "OpenCV finite difference");
  const step = 1e-3;
  const at = (x, y) => applyHomography(H, { x, y });
  const dx = [(at(point.x + step, point.y).x - at(point.x - step, point.y).x) / (2 * step),
    (at(point.x + step, point.y).y - at(point.x - step, point.y).y) / (2 * step)];
  const dy = [(at(point.x, point.y + step).x - at(point.x, point.y - step).x) / (2 * step),
    (at(point.x, point.y + step).y - at(point.x, point.y - step).y) / (2 * step)];
  near(homographyLocalScale(H, point), Math.sqrt(Math.abs(dx[0] * dy[1] - dx[1] * dy[0])), 1e-8, "numeric Jacobian");
  assert.equal(homographyLocalScale([[1, 0, 0], [0, 1, 0], [0.01, 0, 1]], { x: -100, y: 0 }), null, "at infinity");
});

test("alignmentCode is the id plus an even parity bit 6 for every id 1..63", () => {
  const codes = new Set();
  for (let id = 1; id <= 63; id++) {
    const ones = id.toString(2).split("").filter((bit) => bit === "1").length;
    const code = alignmentCode(id);
    assert.equal(code, id | ((ones % 2) << 6), `id ${id}`);
    assert.equal(code & 63, id);
    assert.equal(code.toString(2).split("").filter((bit) => bit === "1").length % 2, 0, `even parity for ${id}`);
    assert.ok(code < 2 ** ALIGNMENT_BITS);
    codes.add(code);
  }
  assert.equal(codes.size, 63);
  assert.equal(alignmentCode(1), 65);
  assert.equal(alignmentCode(3), 3);
  for (const bad of [0, 64, -1, 1.5, "7", NaN]) assert.throws(() => alignmentCode(bad), RangeError);
});

/** Records pattern draws; the context starts painted with a theme background. */
function patternCanvas(background) {
  const draws = [{ kind: "rect", color: background, rect: [0, 0, 320, 240] }];
  const stack = [];
  let path = [];
  return {
    draws, fillStyle: background, globalAlpha: 0.4, shadowBlur: 12, shadowColor: "#ff0000", filter: "blur(3px)",
    save() { stack.push({ fillStyle: this.fillStyle, globalAlpha: this.globalAlpha, shadowBlur: this.shadowBlur,
      shadowColor: this.shadowColor, filter: this.filter }); },
    restore() { Object.assign(this, stack.pop()); },
    beginPath() { path = []; },
    arc(x, y, radius) { path.push({ x, y, radius }); },
    fill() { draws.push({ kind: "fill", path, color: this.fillStyle, alpha: this.globalAlpha, blur: this.shadowBlur, filter: this.filter }); },
    fillRect(...rect) { draws.push({ kind: "rect", rect, color: this.fillStyle, alpha: this.globalAlpha, blur: this.shadowBlur, filter: this.filter }); },
  };
}

// 9 x 7 = 63 dots on a 320 x 240 projector: every id and code is exercised.
const layout = {
  version: 1, width: 320, height: 240, cols: 9, rows: 7, radius: 4, bits: 7,
  dots: Array.from({ length: 63 }, (_, k) => ({ id: k + 1, x: 20 + (k % 9) * 35, y: 20 + Math.floor(k / 9) * 33 })),
  frames: Array.from({ length: 14 }, (_, k) => ({ bit: k >> 1, inverse: Boolean(k & 1) })),
};

test("each pattern frame lights exactly the dots whose code bit matches, on pure black in both themes", () => {
  for (const theme of ["dark", "light"]) {
    const litBy = new Map(layout.dots.map((dot) => [dot.id, []]));
    for (const frame of layout.frames) {
      const ctx = patternCanvas(PROJECTOR_THEMES[theme].background);
      const lit = drawAlignmentPattern(ctx, layout, frame, 320, 240);
      const [background, ...dots] = ctx.draws.slice(1);
      assert.deepEqual(background.rect, [0, 0, 320, 240], "covers the whole viewport first");
      assert.equal(background.color, PATTERN_BACKGROUND);
      assert.equal(PATTERN_BACKGROUND, "#000000");
      const expected = layout.dots.filter((dot) => ((alignmentCode(dot.id) >> frame.bit) & 1) === (frame.inverse ? 0 : 1));
      assert.equal(lit, expected.length);
      assert.deepEqual(dots.map((draw) => draw.path), expected.map((dot) => [{ x: dot.x, y: dot.y, radius: 4 }]));
      for (const draw of [background, ...dots]) {
        assert.equal(draw.alpha, 1);
        assert.equal(draw.blur, 0);
        assert.equal(draw.filter, "none");
      }
      for (const draw of dots) assert.equal(draw.color, PATTERN_DOT);
      for (const dot of expected) litBy.get(dot.id).push(frame);
      assert.equal(ctx.globalAlpha, 0.4, "the caller's drawing state is restored");
    }
    for (const [id, frames] of litBy) {
      for (let bit = 0; bit < ALIGNMENT_BITS; bit++) {
        assert.equal(frames.filter((frame) => frame.bit === bit).length, 1, `dot ${id} lit in exactly one frame of pair ${bit}`);
      }
    }
  }
});

test("the warm-up frame is the {bit: 0, inverse: false} frame", () => {
  const warm = patternCanvas("#f3f2ec");
  const first = patternCanvas("#f3f2ec");
  assert.deepEqual(WARMUP_FRAME, { bit: 0, inverse: false });
  assert.equal(drawAlignmentPattern(warm, layout, "warmup", 320, 240), drawAlignmentPattern(first, layout, { bit: 0, inverse: false }, 320, 240));
  assert.deepEqual(warm.draws, first.draws);
});

test("a malformed frame or layout leaves the projector pure black", () => {
  for (const [badLayout, frame] of [[layout, { bit: 7, inverse: false }], [layout, { bit: -1, inverse: true }],
    [layout, { bit: 1 }], [layout, null], [layout, "cold"], [null, { bit: 0, inverse: false }],
    [{ ...layout, radius: 0 }, { bit: 0, inverse: false }],
    [{ ...layout, dots: [{ id: 0, x: 5, y: 5 }, { id: 64, x: 5, y: 5 }, { id: 1, x: NaN, y: 5 }] }, { bit: 0, inverse: false }]]) {
    const ctx = patternCanvas("#f3f2ec");
    assert.equal(drawAlignmentPattern(ctx, badLayout, frame, 320, 240), 0);
    assert.deepEqual(ctx.draws.slice(1).map((draw) => [draw.kind, draw.color]), [["rect", "#000000"]]);
  }
  const ctx = patternCanvas("#000000");
  assert.equal(drawAlignmentPattern(ctx, layout, { bit: 0, inverse: false }, 0, 240), 0);
  assert.equal(ctx.draws.length, 1, "no drawing into an unusable viewport");
});
