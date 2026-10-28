/**
 * Live renderers, no saved outlines.
 *
 * drawLiveDetection is the panel's overlay on the camera video. It is never
 * projected, so it keeps colored halos that help the operator.
 *
 * drawProjectorScene is everything the projector emits while live, under
 * docs/alignment-and-color.md section 2.1: the camera sees the projected
 * light, and a role hue on bare wall is re-detected as a post-it. So rims
 * are neutral and sit outside the paper, a role color only ever lands
 * strictly inside it (and only after the overlay check allowed it), and
 * the ball and texts are neutral too.
 *
 * drawAlignmentPattern draws the coded dot frames of section 1.2,
 * drawTouchGrid the crosses of the touch alignment (section 1.6), and
 * projectorScene picks which of these the projector window shows.
 */
import {
  alignmentLens,
  applyHomography,
  containTransform,
  convexHull,
  homographyLocalScale,
  isHomography,
  lensLocalScale,
  offsetConvexPolygon,
  PALETTE_ROLES,
  polygonArea,
  handsFreeLine,
  projectorTheme,
  undistortPoint,
} from "./ui-core.mjs";
import {
  APPEAR_EFFECT_MS,
  APPEAR_NEAR_SHARE,
  drawCountdown,
  drawGoalEffect,
  drawHitEffect,
  drawLaunchEffect,
  drawLostEffect,
  drawTrail,
  PARTY_COLORS,
} from "./effects.mjs";
import { drawLevel, drawLevelProblem, nextLine, resultText } from "./level-view.mjs";

export const LIVE_MAX_AGE_MS = 1500;
export const FALLBACK_COLORS = { start: "#ffdf53", goal: "#ffa659", bumper: "#77ec8f", marker: "#ff77d3" };

/** Projector px between a paper's edge and its rim (unaligned, outward). */
export const RIM_OFFSET_PX = 6;
/**
 * Aligned (contract 2.1): the paper glows in its role color up to
 * FILL_INSET_PX from its edge, with a border of the same color just inside
 * it, and no rim outside. Measured in the closed-loop eval: less inset
 * spills color on the wall and the outline grows; a brighter border washes
 * the paper's saturation edge out in a dark room.
 */
export const FILL_INSET_PX = 3;
export const BORDER_WIDTH_PX = 4;
export const BORDER_ALPHA = 0.35;
export const RIM_WIDTH_PX = 3;
/** The rim stroke's canvas miter limit; the offset path itself is a pure miter. */
export const RIM_MITER_LIMIT = 2;
export const FILL_ALPHA = 0.35;
/**
 * Aligned: the paper's last FILL_INSET_PX (left dark by the inset) are lit
 * white at EDGE_ALPHA (Tobias, 2026-10-26: "el reborde negro, que sea con un
 * fill blanco", accepting less precise outlines). Never past the paper's
 * edge. Measured in the closed loop: 80% keeps every lit-room case within
 * the original limits (corners <= 4 px, no flips, no color on the wall);
 * 100% puts corners 5-11 px off. A dark room fails at any strength (a known
 * limit of feedback_eval): keep some room light.
 */
export const EDGE_ALPHA = 0.8;
/** A thinner inset fill is noise on the wall, not a note. */
export const MIN_FILL_AREA_PX = 25;
export const BALL_RADIUS_PX = 10;
/**
 * The ball is still neutral (a hue on the wall would be read as a post-it)
 * but round: a soft glow BALL_GLOW_PX wide at BALL_GLOW_ALPHA, then a
 * radial gradient from a highlight up and left of center to a shade at the
 * edge (NEUTRAL[theme].ballHighlight -> ball -> ballShade).
 */
export const BALL_GLOW_PX = 4;
export const BALL_GLOW_ALPHA = 0.22;
export const RESULT_TEXTS = Object.freeze({ won: "¡LLEGASTE!", lost: "FUERA DEL CAMPO" });
export const UNALIGNED_HINT = "Sin alinear · pulsá A en el panel";
export const HINT_FONT_PX = 16;
export const HINT_MARGIN_PX = 16;
/** The only colors the projector uses outside a paper, per theme. */
export const NEUTRAL = {
  dark: { rim: "#ffffff", ball: "#f6fbfc", ballHighlight: "#ffffff", ballShade: "#a3afb3", edge: "#ffffff" },
  light: { rim: "#2b2f2e", ball: "#17211f", ballHighlight: "#5d6b68", ballShade: "#050807", edge: "#ffffff" },
};
export const PATTERN_BACKGROUND = "#000000";
export const PATTERN_DOT = "#ffffff";
export const ALIGNMENT_BITS = 7;
/** The warm-up frame is the first pattern frame, so exposure settles at its light level. */
export const WARMUP_FRAME = Object.freeze({ bit: 0, inverse: false });

const positive = (n) => Number.isFinite(n) && n > 0;
const validPoint = (p) => p && Number.isFinite(p.x) && Number.isFinite(p.y);
const validShape = (shape) => Array.isArray(shape?.points) && shape.points.length >= 3 && shape.points.every(validPoint);
const HEX = /^#[0-9a-f]{6}$/i;
const TEXT_FONT = "-apple-system, BlinkMacSystemFont, sans-serif";

/**
 * The color a detected note of `role` is marked with (contract 2.6): the
 * operator's mark color, else its detection color, else the fallback.
 */
export function markColor(palette, role) {
  const entry = palette?.roles?.[role];
  for (const color of [entry?.mark, entry?.hex]) if (HEX.test(color ?? "")) return color;
  return FALLBACK_COLORS[role] ?? "#ffffff";
}

function fitFrame(frameWidth, frameHeight, width, height) {
  return [frameWidth, frameHeight, width, height].every(positive)
    ? containTransform(width, height, frameWidth, frameHeight) : null;
}

/** Panel overlay on the camera video: role fill, colored halo, white rim. */
export function drawLiveDetection(ctx, preview, palette, width, height, theme = "dark") {
  const fit = fitFrame(preview?.width, preview?.height, width, height);
  if (!fit) return;
  for (const role of PALETTE_ROLES) {
    const notes = preview.roles?.[role]?.notes;
    if (!Array.isArray(notes)) continue;
    const color = markColor(palette, role);
    for (const shape of notes) {
      if (!validShape(shape)) continue;
      ctx.save();
      ctx.beginPath();
      shape.points.forEach((point, index) => {
        const x = fit.offsetX + point.x * fit.scale;
        const y = fit.offsetY + point.y * fit.scale;
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.closePath();
      ctx.shadowColor = color;
      ctx.shadowBlur = 18;
      ctx.globalAlpha = .3;
      ctx.fillStyle = color;
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#ffffff";
      ctx.stroke();
      ctx.restore();
    }
  }
}

/** Neutral colors of a projector theme; unknown themes fall back to dark. */
export function neutralColors(theme) {
  return NEUTRAL[projectorTheme(theme)];
}

/**
 * The camera -> projector homography when the projector may use it: the
 * alignment is ready and was measured for this exact viewport. Else null,
 * and the projector draws the unaligned scene.
 */
export function alignedMatrix(state, width, height) {
  const alignment = state?.alignment;
  if (alignment?.status !== "ready" || !isHomography(alignment.matrix)) return null;
  const projector = alignment.projector;
  return projector?.width === width && projector?.height === height ? alignment.matrix : null;
}

/** H was measured with one camera frame size; a preview of another size does not fit it. */
function previewFitsAlignment(alignment, preview) {
  const camera = alignment?.camera;
  if (!preview || !positive(camera?.width) || !positive(camera?.height)) return true;
  return preview.width === camera.width && preview.height === camera.height;
}

/**
 * Camera px -> projector px through H, only on the side of H's horizon line
 * (w = 0) that the camera looks at: the side of the camera frame's center
 * (of the camera origin, h22, without a frame size). A point across the
 * horizon has a finite image that no projector pixel can light. The camera
 * center rather than a fixed w > 0 keeps this right for any scale of H.
 */
function cameraToProjector(matrix, camera, lens = null) {
  const [, , [g, h, i]] = matrix;
  const reference = positive(camera?.width) && positive(camera?.height)
    ? g * camera.width / 2 + h * camera.height / 2 + i : i;
  const side = Math.sign(reference) || 1;
  return (point) => {
    if (!validPoint(point)) return null;
    // The lens (contract 1.3.1) straightens the point first; it keeps the frame center.
    const straight = undistortPoint(point, lens);
    return side * (g * straight.x + h * straight.y + i) > 1e-9 ? applyHomography(matrix, straight) : null;
  };
}

/** Each valid note of a preview in projector px, with its role. */
function projectedNotes(preview, mapper, width, height) {
  const fit = fitFrame(preview?.width, preview?.height, width, height);
  if (!fit || !preview.roles) return [];
  const toProjector = mapper
    ?? ((point) => ({ x: fit.offsetX + point.x * fit.scale, y: fit.offsetY + point.y * fit.scale }));
  const projected = [];
  for (const role of PALETTE_ROLES) {
    const notes = preview.roles[role]?.notes;
    if (!Array.isArray(notes)) continue;
    for (const shape of notes) {
      if (!validShape(shape)) continue;
      const points = shape.points.map(toProjector);
      // `center`: the note's vertex mean in camera px, to find it again (3.1 ripples).
      const center = shape.points.reduce((sum, p) => ({ x: sum.x + p.x / shape.points.length,
        y: sum.y + p.y / shape.points.length }), { x: 0, y: 0 });
      if (points.every(Boolean)) projected.push({ role, points, center });
    }
  }
  return projected;
}

/** No shadow, blur or transparency left over from earlier drawing. */
function plainEffects(ctx) {
  ctx.shadowBlur = 0;
  ctx.shadowColor = "rgba(0, 0, 0, 0)";
  ctx.shadowOffsetX = 0;
  ctx.shadowOffsetY = 0;
  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = "source-over";
  ctx.filter = "none";
}

function tracePolygon(ctx, points) {
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y);
    else ctx.lineTo(point.x, point.y);
  });
  ctx.closePath();
}

/**
 * The rim path of a note: its polygon moved RIM_OFFSET_PX outward. The
 * detector only reports convex quads; anything else is rimmed around its
 * convex hull, which still lies outside the paper, instead of vanishing.
 */
export function rimPolygon(points) {
  return offsetConvexPolygon(points, RIM_OFFSET_PX) ?? offsetConvexPolygon(convexHull(points), RIM_OFFSET_PX);
}

/** The fill path: FILL_INSET_PX inside the paper, or null when that degenerates (2.1). */
export function fillPolygon(points) {
  const inset = offsetConvexPolygon(points, -FILL_INSET_PX);
  return inset && polygonArea(inset) >= MIN_FILL_AREA_PX ? inset : null;
}

/** Contract 2.1: a rim stays this far off every other detected note. */
export const RIM_CLEAR_PX = 3;

/**
 * A note's rim, clipped out of `keepClear` (the other notes, grown by
 * RIM_CLEAR_PX): our white light never lands on another paper, so it never
 * cuts a neighbor or a second piece of the same paper.
 */
function drawRim(ctx, points, color, keepClear = []) {
  const rim = rimPolygon(points);
  if (!rim) return;
  ctx.save();
  for (const region of keepClear) {
    ctx.beginPath();
    ctx.rect(-1e5, -1e5, 2e5, 2e5);
    region.forEach((point, index) => (index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y)));
    ctx.closePath();
    ctx.clip("evenodd");
  }
  tracePolygon(ctx, rim);
  ctx.lineWidth = RIM_WIDTH_PX;
  ctx.lineJoin = "miter";
  ctx.miterLimit = RIM_MITER_LIMIT;
  ctx.strokeStyle = color;
  ctx.stroke();
  ctx.restore();
}

/** Role color strictly inside the paper, so a ghost on bare wall shrinks every frame. */
/** White over the paper's unlit edge strip: from the fill's edge to the paper's. */
function drawEdge(ctx, points, color) {
  // A paper too small for the fill gets no light at all, edge included.
  const middle = fillPolygon(points) && offsetConvexPolygon(points, -FILL_INSET_PX / 2);
  if (!middle) return;
  tracePolygon(ctx, middle);
  ctx.globalAlpha = EDGE_ALPHA;
  ctx.lineWidth = FILL_INSET_PX;
  ctx.lineJoin = "miter";
  ctx.miterLimit = RIM_MITER_LIMIT;
  ctx.strokeStyle = color;
  ctx.stroke();
  ctx.globalAlpha = 1;
}

function drawFill(ctx, points, color) {
  const inset = fillPolygon(points);
  if (!inset) return;
  tracePolygon(ctx, inset);
  ctx.globalAlpha = FILL_ALPHA;
  ctx.fillStyle = color;
  ctx.fill();
  // The border: the same color again along the inside of the fill's edge.
  const band = offsetConvexPolygon(points, -(FILL_INSET_PX + BORDER_WIDTH_PX / 2));
  if (band) {
    tracePolygon(ctx, band);
    ctx.globalAlpha = BORDER_ALPHA;
    ctx.lineWidth = BORDER_WIDTH_PX;
    ctx.lineJoin = "miter";
    ctx.miterLimit = RIM_MITER_LIMIT;
    ctx.strokeStyle = color;
    ctx.stroke();
  }
  ctx.globalAlpha = 1;
}

/**
 * Ball center and radius in projector px, from game.space (1.5).
 * "projector": drawn as is, radius 10 px (a game measured on another
 * viewport size is contain-fitted until the backend catches up). "camera":
 * through H when aligned, radius scaled by H's local stretch, or through
 * the contain transform when not. The ball is dropped, never aimed: no aim.
 */
function placeBall(game, camera, mapping, width, height) {
  const position = game.position;
  if (game.space !== "projector" && mapping) {
    const center = mapping.toProjector(position);
    const stretch = center && homographyLocalScale(mapping.matrix, undistortPoint(position, mapping.lens)) *
      lensLocalScale(position, mapping.lens);
    if (!center || !positive(stretch)) return null;
    return { center, radius: BALL_RADIUS_PX * stretch };
  }
  const fit = game.space === "projector"
    ? fitFrame(game.width || width, game.height || height, width, height)
    : fitFrame(game.width || camera?.width, game.height || camera?.height, width, height);
  if (!fit) return null;
  return { center: { x: fit.offsetX + position.x * fit.scale, y: fit.offsetY + position.y * fit.scale },
    radius: BALL_RADIUS_PX * fit.scale };
}

/** The ball: glow, then a shaded body lit from up and left. */
export function drawBall(ctx, center, radius, colors) {
  ctx.globalAlpha = BALL_GLOW_ALPHA;
  ctx.fillStyle = colors.ball;
  ctx.beginPath();
  ctx.arc(center.x, center.y, radius + BALL_GLOW_PX, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalAlpha = 1;
  const light = { x: center.x - 0.35 * radius, y: center.y - 0.4 * radius };
  const body = ctx.createRadialGradient(light.x, light.y, 0.1 * radius, center.x, center.y, radius);
  body.addColorStop(0, colors.ballHighlight);
  body.addColorStop(0.55, colors.ball);
  body.addColorStop(1, colors.ballShade);
  ctx.fillStyle = body;
  ctx.beginPath();
  ctx.arc(center.x, center.y, radius, 0, Math.PI * 2);
  ctx.fill();
}

/**
 * The colored effects of an attempt (effects.mjs), only while the backend
 * says the camera is not read: they light the wall in any color.
 */
function drawEffects(ctx, { state, mapping, width, height, effects, projected, palette }) {
  const game = state?.game;
  // Paused, the wall may be scanned: no colored light then either.
  if (!effects || game?.camera_paused !== true || game.paused === true) return;
  const place = (point) => point && placeBall({ ...game, position: point }, state.camera, mapping, width, height);
  const hex = (role) => markColor(palette, role);
  const paper = (role) => projected.find((note) => note.role === role)?.points ?? null;
  const ball = place(game.position);
  const scale = ball ? ball.radius / BALL_RADIUS_PX : 1;
  if (game.status === "running" && effects.trail?.length > 1) {
    const points = effects.trail.map(place).filter(Boolean).map((p) => p.center);
    drawTrail(ctx, { points, radius: ball?.radius ?? BALL_RADIUS_PX, from: hex("start"), to: hex("goal") });
  }
  // A bounce: the bumper nearest the ball flashes and sparks in its color.
  const gravity = Number(game.gravity_px_s2);
  const reference = gravity > 0 && game.height > 0 ? Math.sqrt(gravity * game.height) : null;
  for (const hit of effects.hits ?? []) {
    const origin = place(hit.origin)?.center;
    if (!origin) continue;
    const bumper = nearestPaper(projected.filter((note) => note.role === "bumper"), origin);
    drawHitEffect(ctx, { ageMs: hit.ageMs, origin, paper: bumper, color: hex("bumper"), scale,
      intensity: reference ? hit.speed / reference : 0.5 });
  }
  if (Number.isFinite(effects.launchAgeMs)) {
    drawLaunchEffect(ctx, { ageMs: effects.launchAgeMs, origin: place(effects.launchOrigin)?.center,
      startPaper: paper("start"), color: hex("start"), scale });
  }
  if (game.status === "won" && Number.isFinite(effects.endAgeMs)) {
    drawGoalEffect(ctx, { ageMs: effects.endAgeMs, goalPaper: paper("goal"),
      colors: ["start", "goal", "bumper"].map(hex), scale });
  }
  if (game.status === "lost" && Number.isFinite(effects.endAgeMs)) {
    drawLostEffect(ctx, { ageMs: effects.endAgeMs, origin: place(effects.endOrigin)?.center, scale });
  }
  ctx.globalAlpha = 1;
}

/** The countdown (3.1), only while the backend says the camera is not read. */
function drawCountdownScene(ctx, { state, mapping, width, height, projected, palette, countdownS }) {
  const game = state?.game;
  if (!(countdownS > 0) || game?.camera_paused !== true || game.status !== "ready" || !validPoint(game.position)) return;
  const ball = placeBall(game, state.camera, mapping, width, height);
  if (!ball) return;
  drawCountdown(ctx, { remainingS: countdownS, totalS: game.countdown_total_s, origin: ball.center,
    startPaper: projected.find((note) => note.role === "start")?.points ?? null,
    color: markColor(palette, "start"), scale: ball.radius / BALL_RADIUS_PX, font: TEXT_FONT });
}

function nearestPaper(notes, point) {
  let best = null;
  let bestDistance = Infinity;
  for (const { points } of notes) {
    const c = points.reduce((sum, p) => ({ x: sum.x + p.x / points.length, y: sum.y + p.y / points.length }),
      { x: 0, y: 0 });
    const d = Math.hypot(c.x - point.x, c.y - point.y);
    if (d < bestDistance) {
      best = points;
      bestDistance = d;
    }
  }
  return best;
}

/** Ring offset around a projected paper (projector px), or null. */
function rippleOutline(points, distance) {
  const hull = convexHull(points);
  return hull && hull.length >= 3 ? offsetConvexPolygon(hull, distance) : null;
}

/**
 * Contract 3.1: a neutral ripple around each post-it just stuck on the wall,
 * from RIM_OFFSET_PX out, so it never lights the paper itself; every paper
 * (its own too) is clipped out like the rims of 2.1. Neutral light, a few
 * frames: the camera already copes with a white rim at that distance.
 */
function drawAppearRipples(ctx, appears, projected, color, cameraSize) {
  if (!Array.isArray(appears) || !appears.length || !projected.length) return 0;
  const near = APPEAR_NEAR_SHARE * Math.min(cameraSize.width || 0, cameraSize.height || 0);
  let drawn = 0;
  for (const appear of appears) {
    const t = appear.ageMs / APPEAR_EFFECT_MS;
    if (!(t >= 0 && t < 1)) continue;
    const note = projected.find((n) => n.role === appear.role
      && Math.hypot(n.center.x - appear.x, n.center.y - appear.y) <= near);
    if (!note) continue;
    ctx.save();
    for (const other of projected) {
      const region = offsetConvexPolygon(convexHull(other.points) ?? other.points, RIM_CLEAR_PX);
      if (!region) continue;
      ctx.beginPath();
      ctx.rect(-1e5, -1e5, 2e5, 2e5);
      region.forEach((point, index) => (index ? ctx.lineTo(point.x, point.y) : ctx.moveTo(point.x, point.y)));
      ctx.closePath();
      ctx.clip("evenodd");
    }
    ctx.strokeStyle = color;
    ctx.lineJoin = "round";
    for (const [delay, width] of [[0, 4], [0.22, 2]]) {
      const local = (t - delay) / (1 - delay);
      if (local <= 0) continue;
      const outline = rippleOutline(note.points, RIM_OFFSET_PX + 60 * (1 - (1 - local) ** 3));
      if (!outline) continue;
      ctx.globalAlpha = 0.85 * (1 - local);
      ctx.lineWidth = width;
      tracePolygon(ctx, outline);
      ctx.stroke();
    }
    ctx.restore();
    drawn += 1;
  }
  return drawn;
}

/** Contract 3.1: how to play without the keyboard, in neutral text at the bottom. */
function drawHandsFreeHint(ctx, game, width, height, color) {
  const line = handsFreeLine(game);
  if (!line) return;
  const armed = game.hands_free?.armed === true;
  ctx.font = `${armed ? 700 : 500} ${armed ? 30 : HINT_FONT_PX + 2}px ${TEXT_FONT}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.globalAlpha = armed ? 1 : 0.75;
  ctx.fillStyle = color;
  ctx.fillText(line, width / 2, height - HINT_MARGIN_PX);
  ctx.globalAlpha = 1;
}

function drawGame(ctx, state, mapping, width, height, colors, effects = null) {
  const level = state?.level ?? null;
  const game = state?.game;
  if (!game) return;
  // The ball appears when it is released (Tobias, 2026-10-26): not while ready.
  const ball = game.status !== "ready" && validPoint(game.position)
    ? placeBall(game, state.camera, mapping, width, height) : null;
  if (ball) drawBall(ctx, ball.center, ball.radius, colors);
  if (game.status === "won" || game.status === "lost") {
    ctx.font = `700 42px ${TEXT_FONT}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = colors.ball;
    // Won, while the camera is paused: the text cycles through party colors.
    const party = game.status === "won" && game.camera_paused === true && Number.isFinite(effects?.endAgeMs);
    if (party) ctx.fillStyle = PARTY_COLORS[Math.floor(effects.endAgeMs / 120) % PARTY_COLORS.length];
    // Why it ended, and the level that follows a won one (contract 3.2).
    ctx.fillText(resultText(game, level) ?? RESULT_TEXTS[game.status], width / 2, 75);
    const upcoming = nextLine(game, level);
    if (upcoming) {
      ctx.font = `500 24px ${TEXT_FONT}`;
      ctx.fillStyle = colors.ball;
      ctx.fillText(upcoming, width / 2, 110);
    }
  }
}

function drawHint(ctx, height, color) {
  ctx.font = `${HINT_FONT_PX}px ${TEXT_FONT}`;
  ctx.textAlign = "left";
  ctx.textBaseline = "bottom";
  ctx.fillStyle = color;
  ctx.fillText(UNALIGNED_HINT, HINT_MARGIN_PX, height - HINT_MARGIN_PX);
}

/**
 * The live projector image over an already painted background.
 *
 * preview: the live detection (camera px); fresh: whether it is recent
 * enough to show (a stale one draws no notes). forceFill draws the role
 * fill even when the overlay check has not allowed it, for that check's own
 * capture (2.3); it never fills without alignment.
 */
export function drawProjectorScene(ctx, {
  state = null, preview = state?.live_detection, palette = state?.palette, theme = "dark",
  width, height, fresh = true, forceFill = false, effects = null, appears = null, countdownS = null,
  levelBannerAgeMs = null,
} = {}) {
  if (!positive(width) || !positive(height)) return;
  const colors = neutralColors(theme);
  const notes = fresh ? preview : null;
  let matrix = alignedMatrix(state, width, height);
  if (matrix && !previewFitsAlignment(state.alignment, notes)) matrix = null;
  const lens = matrix && alignmentLens(state.alignment);
  const mapping = matrix && { matrix, lens, toProjector: cameraToProjector(matrix, state.alignment.camera, lens) };
  ctx.save();
  plainEffects(ctx);
  const projected = [...projectedNotes(notes, mapping?.toProjector, width, height)];
  const clear = projected.map(({ points }) => offsetConvexPolygon(points, RIM_CLEAR_PX));
  // Aligned: every paper's edge strip white first, then the role fills over them.
  if (matrix) for (const { points } of projected) drawEdge(ctx, points, colors.edge);
  for (const [index, { role, points }] of projected.entries()) {
    if (matrix) {
      // Aligned: the role color strictly inside the paper, nothing outside.
      drawFill(ctx, points, markColor(palette, role));
    } else {
      drawRim(ctx, points, colors.rim, clear.filter((region, k) => k !== index && region));
    }
  }
  if (matrix && forceFill) drawProbes(ctx, state.alignment.overlay?.probes, palette);
  // The level (contract 3.2), never in the overlay check's capture (2.3).
  if (!forceFill) {
    drawLevel(ctx, { state, width, height, color: colors.rim, keepClear: clear.filter(Boolean),
      bannerAgeMs: levelBannerAgeMs });
  }
  if (!forceFill) drawAppearRipples(ctx, appears, projected, colors.rim, notes ?? {});
  drawEffects(ctx, { state, mapping, width, height, effects, projected, palette });
  drawCountdownScene(ctx, { state, mapping, width, height, projected, palette, countdownS });
  drawGame(ctx, state, mapping, width, height, colors, effects);
  if (!matrix) drawHint(ctx, height, colors.rim);
  if (!forceFill && !(countdownS > 0) && !drawLevelProblem(ctx, state?.level, width, height, colors.rim)) {
    drawHandsFreeHint(ctx, state?.game, width, height, colors.rim);
  }
  ctx.restore();
}

/**
 * The overlay check's bare-wall probes (contract 2.3): each role's fill
 * color, same alpha as a note's fill, no rim. If the camera takes one for a
 * post-it, the fill is unsafe in this room and stays off.
 */
function drawProbes(ctx, probes, palette) {
  if (!Array.isArray(probes)) return;
  for (const probe of probes) {
    if (!Array.isArray(probe?.points) || probe.points.length !== 4 || !probe.points.every(validPoint)) continue;
    tracePolygon(ctx, probe.points);
    ctx.globalAlpha = FILL_ALPHA;
    ctx.fillStyle = markColor(palette, probe.role);
    ctx.fill();
    ctx.globalAlpha = 1;
  }
}

/**
 * What the projector window shows (docs/alignment-and-color.md 2.2 and 3).
 * A scan blackout wins; then a scene the panel asked for locally (a pattern
 * frame of an alignment run, neutral for a color sample, the overlay
 * check's forced fill); then state.projection.mode, where "align" stays
 * black until a pattern arrives. Unknown modes draw live, as before.
 *
 * kind: what to draw ("black", "pattern", "neutral", "live"); mode: the
 * name the page exposes as body[data-mode].
 */
export function projectorScene({ blackout = false, local = null, mode = "live" } = {}) {
  if (blackout) return { kind: "black", mode: "black" };
  if (local?.kind === "pattern") return { kind: "pattern", mode: "align", layout: local.layout, frame: local.frame };
  if (local?.kind === "neutral") return { kind: "neutral", mode: "neutral" };
  if (local?.kind === "overlay_test") return { kind: "live", mode: "live", forceFill: true };
  if (mode === "black") return { kind: "black", mode: "black" };
  if (mode === "align") return { kind: "black", mode: "align" };
  if (mode === "touch") return { kind: "touch", mode: "touch" };
  if (mode === "neutral") return { kind: "neutral", mode: "neutral" };
  return { kind: "live", mode: "live", forceFill: false };
}

/** 6 data bits plus an even parity bit 6 (contract 1.1). */
export function alignmentCode(id) {
  if (!Number.isInteger(id) || id < 1 || id > 63) throw new RangeError(`Identificador de punto fuera de rango: ${id}`);
  let ones = 0;
  for (let rest = id; rest; rest >>= 1) ones += rest & 1;
  return id | ((ones % 2) << 6);
}

/**
 * One coded pattern frame {bit, inverse}, or "warmup": pure black in every
 * theme, and a white dot wherever that bit of the dot's code is 1 (0 for
 * the inverse frame). Returns how many dots it lit. A malformed frame or
 * layout leaves only black.
 */
export function drawAlignmentPattern(ctx, layout, frame, width, height) {
  if (!positive(width) || !positive(height)) return 0;
  const { bit, inverse } = frame === "warmup" ? WARMUP_FRAME : (frame ?? {});
  ctx.save();
  plainEffects(ctx);
  ctx.fillStyle = PATTERN_BACKGROUND;
  ctx.fillRect(0, 0, width, height);
  let lit = 0;
  const usable = Number.isInteger(bit) && bit >= 0 && bit < ALIGNMENT_BITS && typeof inverse === "boolean"
    && Array.isArray(layout?.dots) && positive(layout.radius);
  if (usable) {
    ctx.fillStyle = PATTERN_DOT;
    for (const dot of layout.dots) {
      if (!validPoint(dot) || !Number.isInteger(dot.id) || dot.id < 1 || dot.id > 63) continue;
      if (((alignmentCode(dot.id) >> bit) & 1) !== (inverse ? 0 : 1)) continue;
      ctx.beginPath();
      ctx.arc(dot.x, dot.y, layout.radius, 0, Math.PI * 2);
      ctx.fill();
      lit += 1;
    }
  }
  ctx.restore();
  return lit;
}

/** Touch-grid colors: white and grays only, so no projected stroke reads as a post-it (section 1.6). */
export const TOUCH_COLORS = Object.freeze({ active: "#ffffff", track: "#3a3a3a", done: "#8a8a8a", next: "#5c5c5c",
  text: "#d0d0d0" });

/**
 * The touch alignment, as NUI Group's CCV draws its calibration: every cross
 * of the grid, the current one with a long white cross and a ring that
 * fills while the post-it stays still, done ones with a small check. The
 * ring sits outside a post-it held on the cross; the arms stick out past
 * its edges so the operator can center it. `touch` is state.alignment.touch.
 * Returns the index of the active cross, or -1.
 */
export function drawTouchGrid(ctx, touch, width, height) {
  if (!positive(width) || !positive(height)) return -1;
  ctx.save();
  plainEffects(ctx);
  ctx.fillStyle = PATTERN_BACKGROUND;
  ctx.fillRect(0, 0, width, height);
  const targets = Array.isArray(touch?.targets) ? touch.targets.filter(validPoint) : [];
  if (!targets.length) {
    ctx.restore();
    return -1;
  }
  const side = Math.min(width, height);
  const arm = 0.1 * side;
  const ring = 0.075 * side;
  const step = Number.isInteger(touch.step) ? touch.step : 0;
  // Taken crosses get a check; the ones still to take, a retake included, a
  // small cross.
  const taken = Number.isInteger(touch.captured) ? touch.captured : step;
  const retake = Array.isArray(touch.retake) ? touch.retake : [];
  ctx.lineCap = "round";
  targets.forEach((t, i) => {
    if (i === step) return;
    const done = i < taken && !retake.includes(i);
    ctx.strokeStyle = done ? TOUCH_COLORS.done : TOUCH_COLORS.next;
    ctx.lineWidth = 2;
    ctx.beginPath();
    if (done) {
      // A small check where the post-it was taken.
      ctx.moveTo(t.x - 8, t.y);
      ctx.lineTo(t.x - 2, t.y + 6);
      ctx.lineTo(t.x + 9, t.y - 7);
    } else {
      ctx.moveTo(t.x - 12, t.y);
      ctx.lineTo(t.x + 12, t.y);
      ctx.moveTo(t.x, t.y - 12);
      ctx.lineTo(t.x, t.y + 12);
    }
    ctx.stroke();
  });
  const active = step < targets.length ? step : -1;
  if (active >= 0) {
    const t = targets[active];
    ctx.strokeStyle = TOUCH_COLORS.active;
    ctx.lineWidth = 3;
    ctx.beginPath();
    ctx.moveTo(t.x - arm, t.y);
    ctx.lineTo(t.x + arm, t.y);
    ctx.moveTo(t.x, t.y - arm);
    ctx.lineTo(t.x, t.y + arm);
    ctx.stroke();
    ctx.strokeStyle = TOUCH_COLORS.track;
    ctx.lineWidth = 8;
    ctx.beginPath();
    ctx.arc(t.x, t.y, ring, 0, Math.PI * 2);
    ctx.stroke();
    const progress = touch.phase === "lift" ? 1 : Math.min(1, Math.max(0, Number(touch.progress) || 0));
    if (progress > 0) {
      ctx.strokeStyle = TOUCH_COLORS.active;
      ctx.beginPath();
      ctx.arc(t.x, t.y, ring, -Math.PI / 2, -Math.PI / 2 + progress * Math.PI * 2);
      ctx.stroke();
    }
  }
  // The text keeps clear of the active cross: at the top, or at the bottom
  // while a cross of the upper half is active.
  const lines = touchLines(touch, targets.length);
  const lineHeight = 0.026 * side;
  const top = active < 0 || targets[active].y > height / 2 ? 0.02 * side
    : height - 0.02 * side - (lines.length + 0.4) * lineHeight;
  ctx.fillStyle = TOUCH_COLORS.text;
  ctx.textAlign = "center";
  ctx.textBaseline = "top";
  ctx.font = `600 ${Math.round(0.024 * side)}px system-ui, sans-serif`;
  ctx.fillText(lines[0], width / 2, top);
  ctx.font = `${Math.round(0.018 * side)}px system-ui, sans-serif`;
  lines.slice(1).forEach((line, k) => ctx.fillText(line, width / 2, top + (k + 1.4) * lineHeight));
  ctx.restore();
  return active;
}

/** The projector's instructions, CCV's wording in Spanish. */
export function touchLines(touch, total) {
  const step = Math.min((touch?.step ?? 0) + 1, total);
  const left = Array.isArray(touch?.retake) ? touch.retake.length : 0;
  const head = left ? `REPITIENDO · punto ${step} · ${left === 1 ? "es el último" : `faltan ${left}`}`
    : `CALIBRANDO · punto ${step} de ${total}`;
  const now = touch?.many ? "Hay más de un papel nuevo: dejá uno solo."
    : touch?.phase === "lift" ? "Listo. Sacá el post-it y llevalo a la cruz siguiente."
      : left ? "Ese toque quedó corrido: pegá el post-it centrado en la cruz y sacá la mano."
        : "Poné un post-it centrado en la cruz y dejalo quieto hasta que el círculo se complete.";
  return [head, now, left ? "B: tomar el fondo de nuevo · Esc: cancelar"
    : "B: tomar el fondo de nuevo · R: volver un punto · Esc: cancelar"];
}
