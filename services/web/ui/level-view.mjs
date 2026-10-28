/**
 * A level on the wall (contract 3.2): the start and goal boxes, walls,
 * spikes, portals, the level's title and bumper count, what to fix, and a
 * banner when the level changes.
 *
 * Contract 2.7: the boxes and portals take the level's own colors
 * (palette.zones), which the backend keeps off every role's hue, so they may
 * light bare wall even while the camera reads it; they keep LEVEL_CLEAR_PX
 * off every paper. A color the backend flags as a conflict draws neutral.
 *
 * Section 2.1 rules here as everywhere on the projector: while the camera
 * reads the wall only neutral light lands outside a paper, so every shape is
 * drawn in the theme's neutral color and clipped off every detected paper
 * (grown by RIM_CLEAR_PX). Spikes and portals take their own color only while
 * the backend says the camera is not read (an attempt in the air).
 */

export const LEVEL_BANNER_MS = 3500;
export const ZONE_DASH = Object.freeze([16, 12]);
/** How fast the dashes of a waiting box march, px a second. */
export const ZONE_MARCH_PX_S = 28;
export const ZONE_FILL = 0.07;
export const ZONE_FILL_MET = 0.14;
/** The label's ink on its pill: dark on a light color, white on a dark one. */
export const PILL_INK = "#07121f";
export function inkOn(hex) {
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(String(hex).slice(i, i + 2), 16) / 255);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.45 ? PILL_INK : "#ffffff";
}
export const PORTAL_TURNS_S = 0.35;
export const PORTAL_EXIT_PX = 28;
/** How far down the funnel's inner rings sink, in shares of the mouth's half height. */
export const PORTAL_DEPTH = 0.9;
const PORTAL_SPARKS = 8;
const PORTAL_STREAM = 14;
/**
 * The level's own colors (contract 2.7), from the palette's `zones`; null
 * when the backend says one would read as a post-it: neutral light then.
 */
export function zoneColors(palette) {
  const pick = (part) => {
    const zone = palette?.zones?.[part];
    return zone && !zone.conflict && /^#[0-9a-f]{6}$/i.test(zone.hex ?? "") ? zone.hex : null;
  };
  return { start: pick("start"), goal: pick("goal"), portal: pick("portal") };
}
export const ZONE_LABELS = Object.freeze({ start: "INICIO", goal: "LLEGADA" });
export const SPIKES_COLOR = "#ff4d4d";
/**
 * Colored level light keeps this far off every detected paper (projector
 * px): farther than a neutral rim's RIM_CLEAR_PX, so camera blur never
 * mixes a box's color into a note's edge.
 */
export const LEVEL_CLEAR_PX = 10;
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

/** A box: a rounded dashed outline that marches while it waits, solid once met. */
function drawZone(ctx, points, met, label, color, timeMs) {
  const box = bounds(points);
  const w = box.right - box.left;
  const h = box.bottom - box.top;
  if (!(w > 0 && h > 0)) return;
  const radius = Math.min(14, w / 4, h / 4);
  const path = () => {
    ctx.beginPath();
    ctx.roundRect(box.left, box.top, w, h, radius);
  };
  // Waiting: a slow breath; met: steady.
  const breath = met ? 1 : 0.78 + 0.22 * Math.sin((timeMs / 1000) * Math.PI * 1.4);
  path();
  ctx.globalAlpha = met ? ZONE_FILL_MET : ZONE_FILL;
  ctx.fillStyle = color;
  ctx.fill();
  path();
  ctx.globalAlpha = breath;
  ctx.lineWidth = met ? 5 : 4;
  ctx.lineCap = "round";
  ctx.setLineDash(met ? [] : ZONE_DASH);
  ctx.lineDashOffset = met ? 0 : -(timeMs * ZONE_MARCH_PX_S) / 1000;
  ctx.strokeStyle = color;
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.lineDashOffset = 0;
  // Corner brackets, solid: the box reads from across the room.
  const arm = Math.min(28, w / 3, h / 3);
  ctx.globalAlpha = 1;
  ctx.lineWidth = 7;
  ctx.beginPath();
  for (const [x, y, dx, dy] of [[box.left, box.top, 1, 1], [box.right, box.top, -1, 1],
    [box.right, box.bottom, -1, -1], [box.left, box.bottom, 1, -1]]) {
    ctx.moveTo(x, y + dy * arm);
    ctx.lineTo(x, y);
    ctx.lineTo(x + dx * arm, y);
  }
  ctx.stroke();
  ctx.lineCap = "butt";
  // The label in a pill of the box's color, above it.
  const text = met ? `${label} ✓` : label;
  ctx.font = `800 17px ${LEVEL_FONT}`;
  const pillW = ctx.measureText(text).width + 22;
  const pillH = 26;
  const cx = (box.left + box.right) / 2;
  const top = box.top - pillH - 8;
  ctx.globalAlpha = met ? 1 : breath;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.roundRect(cx - pillW / 2, top, pillW, pillH, pillH / 2);
  ctx.fill();
  ctx.globalAlpha = 1;
  ctx.fillStyle = inkOn(color);
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, cx, top + pillH / 2 + 1);
  ctx.textBaseline = "alphabetic";
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

/**
 * A portal: the entry is a vortex (rings turning against each other, sparks
 * circling its rim), the exit a ring that spins the same way with sparks
 * flying out, and a stream of dots runs from one to the other.
 */
function drawPortal(ctx, entry, exit, scale, color, timeMs, live) {
  const box = bounds(entry);
  const cx = (box.left + box.right) / 2;
  const cy = (box.top + box.bottom) / 2;
  const rx = Math.max(12, (box.right - box.left) / 2);
  const ry = Math.max(10, (box.bottom - box.top) / 2);
  const turn = (timeMs / 1000) * PORTAL_TURNS_S * Math.PI * 2;
  const glow = live ? 1 : 0.85;
  // The mouth: a faint disc, then rings, each a few arcs turning.
  ctx.globalAlpha = 0.16 * glow;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
  ctx.fill();
  ctx.strokeStyle = color;
  ctx.lineCap = "round";
  ctx.globalAlpha = glow;
  ctx.lineWidth = 4;
  ctx.beginPath();
  ctx.ellipse(cx, cy, rx, ry, 0, 0, Math.PI * 2);
  ctx.stroke();
  // A funnel: each inner ring a little lower, so the mouth reads as a hole.
  for (const [k, share, arcs] of [[0, 0.8, 3], [1, 0.6, 3], [2, 0.42, 2], [3, 0.24, 2]]) {
    const spin = (k % 2 ? -1 : 1) * turn * (1 + 0.5 * k);
    const depth = ry * PORTAL_DEPTH * (1 - share);
    ctx.globalAlpha = glow * (0.95 - 0.17 * k);
    ctx.lineWidth = 3.2 - 0.5 * k;
    for (let a = 0; a < arcs; a += 1) {
      const start = spin + (a * Math.PI * 2) / arcs;
      ctx.beginPath();
      ctx.ellipse(cx, cy + depth, rx * share, ry * share, 0, start, start + (Math.PI * 2) / arcs * 0.6);
      ctx.stroke();
    }
  }
  ctx.fillStyle = color;
  for (let k = 0; k < PORTAL_SPARKS; k += 1) {
    const a = turn * 0.8 + (k * Math.PI * 2) / PORTAL_SPARKS;
    ctx.globalAlpha = glow * (0.55 + 0.45 * Math.sin(turn * 2 + k));
    ctx.beginPath();
    ctx.arc(cx + Math.cos(a) * (rx + 6), cy + Math.sin(a) * (ry + 6), 2.6, 0, Math.PI * 2);
    ctx.fill();
  }
  // The exit: a spinning ring, an inner glow, sparks flung outward.
  const r = PORTAL_EXIT_PX * scale;
  ctx.globalAlpha = 0.18 * glow;
  ctx.beginPath();
  ctx.arc(exit.x, exit.y, r, 0, Math.PI * 2);
  ctx.fill();
  ctx.globalAlpha = glow;
  ctx.lineWidth = 4;
  for (let a = 0; a < 3; a += 1) {
    const start = -turn * 1.2 + (a * Math.PI * 2) / 3;
    ctx.beginPath();
    ctx.arc(exit.x, exit.y, r, start, start + Math.PI * 0.45);
    ctx.stroke();
  }
  ctx.lineWidth = 2;
  ctx.globalAlpha = glow * 0.6;
  ctx.beginPath();
  ctx.arc(exit.x, exit.y, r * 0.55, turn, turn + Math.PI * 1.2);
  ctx.stroke();
  for (let k = 0; k < PORTAL_SPARKS; k += 1) {
    const phase = ((timeMs / 1000) * 0.9 + k / PORTAL_SPARKS) % 1;
    const a = (k * Math.PI * 2) / PORTAL_SPARKS + turn * 0.3;
    const out = r * (1.05 + 0.9 * phase);
    ctx.globalAlpha = glow * (1 - phase);
    ctx.beginPath();
    ctx.arc(exit.x + Math.cos(a) * out, exit.y + Math.sin(a) * out, 2.4, 0, Math.PI * 2);
    ctx.fill();
  }
  // The link: dots flowing along a curve from the mouth to the exit.
  const from = { x: cx, y: box.top - 4 };
  const to = { x: exit.x, y: exit.y + r + 4 };
  const bend = { x: (from.x + to.x) / 2, y: Math.min(from.y, to.y) - 0.25 * Math.abs(to.x - from.x) };
  for (let k = 0; k < PORTAL_STREAM; k += 1) {
    const t = ((timeMs / 1000) * 0.35 + k / PORTAL_STREAM) % 1;
    const x = (1 - t) * (1 - t) * from.x + 2 * (1 - t) * t * bend.x + t * t * to.x;
    const y = (1 - t) * (1 - t) * from.y + 2 * (1 - t) * t * bend.y + t * t * to.y;
    ctx.globalAlpha = glow * 0.5 * Math.sin(Math.PI * t);
    ctx.beginPath();
    ctx.arc(x, y, 2, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.lineCap = "butt";
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
export function drawLevel(ctx, { state, width, height, color, keepClear = [], bannerAgeMs = null, timeMs = 0 }) {
  const level = state?.level;
  const geometry = level?.geometry;
  const map = levelMapper(geometry, width, height);
  const game = state?.game;
  const running = game?.camera_paused === true && game?.paused !== true;
  const colors = zoneColors(state?.palette);
  let drawn = 0;
  ctx.save();
  if (map) {
    clipPapers(ctx, keepClear);
    const shape = (s) => (validShape(s) ? s.points.map(map.point) : null);
    const check = level.check ?? {};
    for (const [role, key] of [["start", "start_zone"], ["goal", "goal_zone"]]) {
      const points = shape(geometry[key]);
      if (!points) continue;
      drawZone(ctx, points, check[role] === true, ZONE_LABELS[role], colors[role] ?? color, timeMs);
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
        drawPortal(ctx, entry, map.point(portal.exit), map.scale, colors.portal ?? color, timeMs, running);
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
