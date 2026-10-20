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
 * drawAlignmentPattern draws the coded dot frames of section 1.2, and
 * projectorScene picks which of these the projector window shows.
 */
import {
  applyHomography,
  containTransform,
  convexHull,
  homographyLocalScale,
  isHomography,
  offsetConvexPolygon,
  PALETTE_ROLES,
  polygonArea,
  projectorTheme,
} from "./ui-core.mjs";

export const LIVE_MAX_AGE_MS = 1500;
export const FALLBACK_COLORS = { start: "#ffdf53", goal: "#ffa659", bumper: "#77ec8f", marker: "#ff77d3" };

/** Projector px between a paper's edge and its rim (outward) or its fill (inward). */
export const RIM_OFFSET_PX = 6;
/**
 * Light inside a paper when its role color is not allowed (aligned only):
 * neutral, so it can never be taken for a post-it, yet a dark room still
 * sees the whole paper. None under the light theme, whose background
 * already lights the wall (contract 2.1).
 */
export const NEUTRAL_FILL = Object.freeze({ dark: "#ffffff", light: null });
export const FILL_INSET_PX = 5;
export const RIM_WIDTH_PX = 3;
/** The rim stroke's canvas miter limit; the offset path itself is a pure miter. */
export const RIM_MITER_LIMIT = 2;
export const FILL_ALPHA = 0.35;
/** A thinner inset fill is noise on the wall, not a note. */
export const MIN_FILL_AREA_PX = 25;
export const BALL_RADIUS_PX = 10;
export const AIM_LENGTH_PX = 70;
export const AIM_WIDTH_PX = 3;
export const RESULT_TEXTS = Object.freeze({ won: "¡LLEGASTE!", lost: "FUERA DEL CAMPO" });
export const UNALIGNED_HINT = "Sin alinear · pulsá A en el panel";
export const HINT_FONT_PX = 16;
export const HINT_MARGIN_PX = 16;
/** The only colors the projector uses outside a paper, per theme. */
export const NEUTRAL = {
  dark: { rim: "#ffffff", ball: "#f6fbfc" },
  light: { rim: "#2b2f2e", ball: "#17211f" },
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
    const color = palette?.roles?.[role]?.hex || FALLBACK_COLORS[role];
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
function cameraToProjector(matrix, camera) {
  const [, , [g, h, i]] = matrix;
  const reference = positive(camera?.width) && positive(camera?.height)
    ? g * camera.width / 2 + h * camera.height / 2 + i : i;
  const side = Math.sign(reference) || 1;
  return (point) => (validPoint(point) && side * (g * point.x + h * point.y + i) > 1e-9
    ? applyHomography(matrix, point) : null);
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
      if (points.every(Boolean)) projected.push({ role, points });
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

function drawRim(ctx, points, color) {
  const rim = rimPolygon(points);
  if (!rim) return;
  tracePolygon(ctx, rim);
  ctx.lineWidth = RIM_WIDTH_PX;
  ctx.lineJoin = "miter";
  ctx.miterLimit = RIM_MITER_LIMIT;
  ctx.strokeStyle = color;
  ctx.stroke();
}

/** Role color strictly inside the paper, so a ghost on bare wall shrinks every frame. */
function drawFill(ctx, points, color) {
  const inset = fillPolygon(points);
  if (!inset) return;
  tracePolygon(ctx, inset);
  ctx.globalAlpha = FILL_ALPHA;
  ctx.fillStyle = color;
  ctx.fill();
  ctx.globalAlpha = 1;
}

/**
 * Ball center, radius and aim tip in projector px, from game.space (1.5).
 * "projector": drawn as is, radius 10 px (a game measured on another
 * viewport size is contain-fitted until the backend catches up). "camera":
 * through H when aligned, radius scaled by H's local stretch, or through
 * the contain transform when not.
 */
function placeBall(game, camera, mapping, width, height) {
  const position = game.position;
  const angle = (game.aim_degrees ?? -45) * Math.PI / 180;
  const direction = { x: Math.cos(angle), y: Math.sin(angle) };
  if (game.space !== "projector" && mapping) {
    const center = mapping.toProjector(position);
    const stretch = center && homographyLocalScale(mapping.matrix, position);
    if (!center || !positive(stretch)) return null;
    const tip = mapping.toProjector({
      x: position.x + AIM_LENGTH_PX * direction.x, y: position.y + AIM_LENGTH_PX * direction.y,
    });
    return { center, radius: BALL_RADIUS_PX * stretch, tip };
  }
  const fit = game.space === "projector"
    ? fitFrame(game.width || width, game.height || height, width, height)
    : fitFrame(game.width || camera?.width, game.height || camera?.height, width, height);
  if (!fit) return null;
  const center = { x: fit.offsetX + position.x * fit.scale, y: fit.offsetY + position.y * fit.scale };
  return {
    center,
    radius: BALL_RADIUS_PX * fit.scale,
    tip: { x: center.x + AIM_LENGTH_PX * fit.scale * direction.x, y: center.y + AIM_LENGTH_PX * fit.scale * direction.y },
  };
}

function drawGame(ctx, state, mapping, width, height, color) {
  const game = state?.game;
  if (!game) return;
  const ball = validPoint(game.position) ? placeBall(game, state.camera, mapping, width, height) : null;
  if (ball) {
    if (game.status === "ready" && validPoint(ball.tip)) {
      ctx.strokeStyle = color;
      ctx.lineWidth = AIM_WIDTH_PX;
      ctx.beginPath();
      ctx.moveTo(ball.center.x, ball.center.y);
      ctx.lineTo(ball.tip.x, ball.tip.y);
      ctx.stroke();
    }
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(ball.center.x, ball.center.y, ball.radius, 0, Math.PI * 2);
    ctx.fill();
  }
  if (game.status === "won" || game.status === "lost") {
    ctx.font = `700 42px ${TEXT_FONT}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "alphabetic";
    ctx.fillStyle = color;
    ctx.fillText(RESULT_TEXTS[game.status], width / 2, 75);
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
  width, height, fresh = true, forceFill = false,
} = {}) {
  if (!positive(width) || !positive(height)) return;
  const colors = neutralColors(theme);
  const notes = fresh ? preview : null;
  let matrix = alignedMatrix(state, width, height);
  if (matrix && !previewFitsAlignment(state.alignment, notes)) matrix = null;
  const mapping = matrix && { matrix, toProjector: cameraToProjector(matrix, state.alignment.camera) };
  const fill = Boolean(matrix) && (forceFill || state.alignment.overlay?.fill === true);
  const neutralFill = matrix && !fill ? NEUTRAL_FILL[projectorTheme(theme)] : null;
  ctx.save();
  plainEffects(ctx);
  for (const { role, points } of projectedNotes(notes, mapping?.toProjector, width, height)) {
    if (fill) {
      const hex = palette?.roles?.[role]?.hex;
      drawFill(ctx, points, HEX.test(hex ?? "") ? hex : FALLBACK_COLORS[role]);
    } else if (neutralFill) {
      drawFill(ctx, points, neutralFill);
    }
    drawRim(ctx, points, colors.rim);
  }
  if (fill && forceFill) drawProbes(ctx, state.alignment.overlay?.probes, palette);
  drawGame(ctx, state, mapping, width, height, colors.ball);
  if (!matrix) drawHint(ctx, height, colors.rim);
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
    const hex = palette?.roles?.[probe.role]?.hex;
    tracePolygon(ctx, probe.points);
    ctx.globalAlpha = FILL_ALPHA;
    ctx.fillStyle = HEX.test(hex ?? "") ? hex : FALLBACK_COLORS[probe.role] ?? "#ffffff";
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
