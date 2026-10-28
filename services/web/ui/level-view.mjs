/**
 * A level on the wall (contract 3.2): the start and goal boxes, walls,
 * spikes, portals, the level's title and bumper count, what to fix, and a
 * banner when the level changes.
 *
 * Section 2.1 rules here as everywhere on the projector: while the camera
 * reads the wall only neutral light lands outside a paper, so every shape is
 * drawn in the theme's neutral color and clipped off every detected paper
 * (grown by RIM_CLEAR_PX). Spikes and portals take their own color only while
 * the backend says the camera is not read (an attempt in the air).
 */

export const LEVEL_BANNER_MS = 3500;
export const ZONE_DASH = Object.freeze([14, 10]);
export const ZONE_LABELS = Object.freeze({ start: "INICIO", goal: "LLEGADA" });
export const SPIKES_COLOR = "#ff4d4d";
export const PORTAL_COLOR = "#46c8ff";
export const LEVEL_FONT = "-apple-system, BlinkMacSystemFont, sans-serif";
const MARGIN_PX = 16;

const validPoint = (p) => p && Number.isFinite(p.x) && Number.isFinite(p.y);
const validShape = (shape) => Array.isArray(shape?.points) && shape.points.length >= 3 && shape.points.every(validPoint);

/** Level space (geometry.width x height) -> projector px: contain-fit, the identity when equal. */
export function levelMapper(geometry, width, height) {
  const gw = Number(geometry?.width);
  const gh = Number(geometry?.height);
  if (!(gw > 0 && gh > 0 && width > 0 && height > 0)) return null;
  const scale = Math.min(width / gw, height / gh);
  const offsetX = (width - gw * scale) / 2;
  const offsetY = (height - gh * scale) / 2;
  return { scale, point: (p) => ({ x: offsetX + p.x * scale, y: offsetY + p.y * scale }) };
}

/** The level's title line on the wall; null in the free mode. */
export function levelTitle(level) {
  if (!level || level.id === "libre") return null;
  return `NIVEL ${level.id} · ${level.name}`.toUpperCase();
}

/** "Rebotes 1 / 2" while a level limits them, else null. */
export function bumperLine(level) {
  if (!level || level.bumpers_max === null || level.bumpers_max === undefined) return null;
  const used = Number(level.check?.bumpers ?? 0);
  return level.bumpers_max === 0 ? "Sin rebotes" : `Rebotes ${used} / ${level.bumpers_max}`;
}

/** The big result text of an ended attempt: why it ended. */
export function resultText(game, level = null) {
  if (game?.status === "won") {
    if (level && level.id !== "libre" && level.next === null) return "¡COMPLETASTE TODOS LOS NIVELES!";
    return "¡LLEGASTE!";
  }
  if (game?.status !== "lost") return null;
  return { spikes: "¡PINCHOS!", stall: "SE QUEDÓ QUIETA", timeout: "SIN TIEMPO" }[game.ended_by] ?? "FUERA DEL CAMPO";
}

/** The line under the result: the level that follows a won one. */
export function nextLine(game, level) {
  if (game?.status !== "won" || !level?.next || !(Number(level.advance_s) > 0)) return null;
  const upcoming = level.list?.find((entry) => entry.id === level.next);
  return `Sigue el nivel ${level.next}${upcoming ? `: ${upcoming.name}` : ""}`;
}

/** Banner opacity for a level shown `ageMs` ago: in, hold, out; 0 when done. */
export function bannerAlpha(ageMs) {
  if (!(ageMs >= 0) || ageMs >= LEVEL_BANNER_MS) return 0;
  return Math.min(1, ageMs / 250, (LEVEL_BANNER_MS - ageMs) / 600);
}

/** Follows the level in play: when this window first saw it. */
export function trackLevel(previous, level, now) {
  const id = level?.id ?? null;
  if (id === null) return previous;
  if (previous?.id === id) return previous;
  // The first state after the window opens is not a change: no banner.
  return { id, at: previous ? now : -Infinity };
}

function trace(ctx, points) {
  ctx.beginPath();
  points.forEach((p, k) => (k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
  ctx.closePath();
}

function bounds(points) {
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  return { left: Math.min(...xs), right: Math.max(...xs), top: Math.min(...ys), bottom: Math.max(...ys) };
}

/** Teeth along a strip, pointing `facing`; their bases fill the strip's far side. */
export function spikeTeeth(points, facing = "up") {
  const box = bounds(points);
  const along = facing === "up" || facing === "down";
  const length = along ? box.right - box.left : box.bottom - box.top;
  const depth = along ? box.bottom - box.top : box.right - box.left;
  if (!(length > 0 && depth > 0)) return [];
  const count = Math.max(1, Math.round(length / (depth * 1.1)));
  const step = length / count;
  const teeth = [];
  for (let k = 0; k < count; k += 1) {
    const a = k * step;
    const b = a + step;
    const m = a + step / 2;
    if (facing === "up") teeth.push([{ x: box.left + a, y: box.bottom }, { x: box.left + m, y: box.top }, { x: box.left + b, y: box.bottom }]);
    else if (facing === "down") teeth.push([{ x: box.left + a, y: box.top }, { x: box.left + m, y: box.bottom }, { x: box.left + b, y: box.top }]);
    else if (facing === "left") teeth.push([{ x: box.right, y: box.top + a }, { x: box.left, y: box.top + m }, { x: box.right, y: box.top + b }]);
    else teeth.push([{ x: box.left, y: box.top + a }, { x: box.right, y: box.top + m }, { x: box.left, y: box.top + b }]);
  }
  return teeth;
}

function drawZone(ctx, points, met, label, color) {
  trace(ctx, points);
  ctx.setLineDash(met ? [] : ZONE_DASH);
  ctx.lineWidth = met ? 4 : 3;
  ctx.globalAlpha = met ? 0.95 : 0.7;
  ctx.strokeStyle = color;
  ctx.stroke();
  ctx.setLineDash([]);
  const box = bounds(points);
  ctx.font = `700 18px ${LEVEL_FONT}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.fillStyle = color;
  ctx.fillText(met ? `${label} ✓` : label, (box.left + box.right) / 2, box.top - 6);
  ctx.globalAlpha = 1;
}

function drawWall(ctx, points, color) {
  trace(ctx, points);
  ctx.globalAlpha = 0.22;
  ctx.fillStyle = color;
  ctx.fill();
  ctx.save();
  trace(ctx, points);
  ctx.clip();
  const box = bounds(points);
  ctx.globalAlpha = 0.55;
  ctx.lineWidth = 2;
  ctx.strokeStyle = color;
  ctx.beginPath();
  const span = box.bottom - box.top;
  for (let x = box.left - span; x < box.right; x += 14) {
    ctx.moveTo(x, box.bottom);
    ctx.lineTo(x + span, box.top);
  }
  ctx.stroke();
  ctx.restore();
  trace(ctx, points);
  ctx.globalAlpha = 0.95;
  ctx.lineWidth = 3;
  ctx.strokeStyle = color;
  ctx.stroke();
  ctx.globalAlpha = 1;
}

function drawSpikes(ctx, points, facing, color) {
  ctx.globalAlpha = 0.9;
  ctx.fillStyle = color;
  for (const tooth of spikeTeeth(points, facing)) {
    trace(ctx, tooth);
    ctx.fill();
  }
  ctx.globalAlpha = 1;
}

function drawPortal(ctx, entry, exit, scale, color) {
  trace(ctx, entry);
  ctx.lineWidth = 3;
  ctx.globalAlpha = 0.95;
  ctx.strokeStyle = color;
  ctx.stroke();
  const box = bounds(entry);
  const cx = (box.left + box.right) / 2;
  const cy = (box.top + box.bottom) / 2;
  ctx.globalAlpha = 0.45;
  ctx.beginPath();
  ctx.ellipse(cx, cy, Math.max(2, (box.right - box.left) / 2 - 8), Math.max(1, (box.bottom - box.top) / 2 - 4), 0, 0, Math.PI * 2);
  ctx.stroke();
  for (const [radius, alpha] of [[16, 0.9], [26, 0.45]]) {
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.arc(exit.x, exit.y, radius * scale, 0, Math.PI * 2);
    ctx.stroke();
  }
  // From the entry to the exit, faint: where the ball comes out.
  ctx.globalAlpha = 0.25;
  ctx.setLineDash([6, 10]);
  ctx.beginPath();
  ctx.moveTo(cx, box.top);
  ctx.lineTo(exit.x, exit.y + 26 * scale);
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.globalAlpha = 1;
}

/** Everything detected keeps clear of our light (2.1): an even-odd clip per paper. */
function clipPapers(ctx, keepClear) {
  for (const region of keepClear) {
    if (!region) continue;
    ctx.beginPath();
    ctx.rect(-1e5, -1e5, 2e5, 2e5);
    region.forEach((p, k) => (k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.closePath();
    ctx.clip("evenodd");
  }
}

/**
 * The level's shapes and texts. keepClear: every detected paper in
 * projector px, already grown by RIM_CLEAR_PX. Returns how many shapes it drew.
 */
export function drawLevel(ctx, { state, width, height, color, keepClear = [], bannerAgeMs = null }) {
  const level = state?.level;
  const geometry = level?.geometry;
  const map = levelMapper(geometry, width, height);
  const game = state?.game;
  const running = game?.camera_paused === true && game?.paused !== true;
  let drawn = 0;
  ctx.save();
  if (map) {
    clipPapers(ctx, keepClear);
    const shape = (s) => (validShape(s) ? s.points.map(map.point) : null);
    const check = level.check ?? {};
    for (const [role, key] of [["start", "start_zone"], ["goal", "goal_zone"]]) {
      const points = shape(geometry[key]);
      if (!points) continue;
      drawZone(ctx, points, check[role] === true, ZONE_LABELS[role], color);
      drawn += 1;
    }
    for (const wall of geometry.walls ?? []) {
      const points = shape(wall);
      if (points) {
        drawWall(ctx, points, color);
        drawn += 1;
      }
    }
    for (const strip of geometry.spikes ?? []) {
      const points = shape(strip);
      if (points) {
        drawSpikes(ctx, points, strip.facing, running ? SPIKES_COLOR : color);
        drawn += 1;
      }
    }
    for (const portal of geometry.portals ?? []) {
      const entry = shape(portal.entry);
      if (entry && validPoint(portal.exit)) {
        drawPortal(ctx, entry, map.point(portal.exit), map.scale, running ? PORTAL_COLOR : color);
        drawn += 1;
      }
    }
  }
  ctx.restore();
  ctx.save();
  ctx.fillStyle = color;
  // Bottom right: the window's own label and most start boxes sit up top,
  // the play hint and what to fix at the bottom center.
  ctx.textAlign = "right";
  ctx.textBaseline = "alphabetic";
  const title = levelTitle(level);
  if (title) {
    ctx.globalAlpha = 0.85;
    ctx.font = `700 20px ${LEVEL_FONT}`;
    ctx.fillText(title, width - MARGIN_PX, height - MARGIN_PX - 24);
  }
  const bumpers = bumperLine(level);
  if (bumpers) {
    const over = level.bumpers_max !== null && Number(level.check?.bumpers ?? 0) > level.bumpers_max;
    ctx.globalAlpha = over ? 1 : 0.7;
    ctx.font = `${over ? 700 : 500} 16px ${LEVEL_FONT}`;
    ctx.fillText(bumpers, width - MARGIN_PX, height - MARGIN_PX);
  }
  const alpha = bannerAlpha(bannerAgeMs);
  if (alpha > 0 && title) {
    ctx.globalAlpha = alpha;
    ctx.textAlign = "center";
    ctx.font = `800 64px ${LEVEL_FONT}`;
    ctx.fillText(`NIVEL ${level.id}`, width / 2, height / 2 - 10);
    ctx.font = `500 28px ${LEVEL_FONT}`;
    ctx.fillText(level.name, width / 2, height / 2 + 34);
    if (level.hint) {
      ctx.font = `400 20px ${LEVEL_FONT}`;
      ctx.fillText(level.hint, width / 2, height / 2 + 70);
    }
  }
  ctx.restore();
  return drawn;
}

/** What to fix, at the bottom center instead of the play hint; false when nothing. */
export function drawLevelProblem(ctx, level, width, height, color) {
  const text = level?.problem;
  if (typeof text !== "string" || !text) return false;
  ctx.save();
  ctx.font = `700 24px ${LEVEL_FONT}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "bottom";
  ctx.fillStyle = color;
  ctx.fillText(text, width / 2, height - MARGIN_PX);
  ctx.restore();
  return true;
}
