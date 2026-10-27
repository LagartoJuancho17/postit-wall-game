/**
 * Colored effects of an attempt (Tobias, 2026-10-26: "efecto de lanzar la
 * pelota; efecto cuando llega a la llegada, explosión de luces, colores").
 *
 * They light the wall in any color, so they are drawn only while the backend
 * says the camera is not read (`game.camera_paused`: from the drop until
 * END_EFFECT_S after the end). Every function is a pure function of its
 * inputs and the effect's age, so tests can draw any instant exactly.
 */

export const LAUNCH_EFFECT_MS = 700;
export const GOAL_EFFECT_MS = 2400;   // shorter than the backend's END_EFFECT_S (2.5 s)
export const LOST_EFFECT_MS = 900;
export const TRAIL_LENGTH = 16;       // ball positions kept for the trail
export const GOAL_PARTICLES = 96;
export const PARTY_COLORS = ["#ffd166", "#06d6a0", "#4cc9f0", "#ef476f", "#f72585", "#b8f2e6", "#ffffff"];

const TAU = Math.PI * 2;
const easeOut = (t) => 1 - (1 - t) ** 3;

/** A repeatable pseudo-random number in [0, 1) for particle `index`, stream `salt`. */
export function noise(index, salt = 0) {
  let value = (Math.imul(index + 1, 0x9e3779b1) ^ Math.imul(salt + 7, 0x85ebca6b)) >>> 0;
  value = Math.imul(value ^ (value >>> 15), 0x2c1b3c6d) >>> 0;
  value = Math.imul(value ^ (value >>> 12), 0x297a2d39) >>> 0;
  return ((value ^ (value >>> 15)) >>> 0) / 2 ** 32;
}

/**
 * What the projector window remembers between states to time the effects:
 * when the current attempt was dropped and when it ended, where it ended, and
 * the ball's recent positions (game px). `now` in ms.
 */
export function trackEffects(previous, game, now) {
  const empty = { attempt: null, status: null, launchAt: null, endAt: null, trail: [] };
  if (!game || !game.position || !["running", "won", "lost"].includes(game.status)) return empty;
  const prev = previous ?? empty;
  const sameAttempt = prev.attempt === game.attempts;
  const launchAt = sameAttempt && prev.launchAt !== null ? prev.launchAt
    : game.status === "running" ? now - 1000 * (game.elapsed_s ?? 0) : null;
  const ended = game.status === "won" || game.status === "lost";
  const endAt = !ended ? null : sameAttempt && prev.endAt !== null ? prev.endAt : now;
  const last = prev.trail.at(-1);
  const moved = !last || last.x !== game.position.x || last.y !== game.position.y;
  const trail = sameAttempt && game.status === "running"
    ? (moved ? [...prev.trail, { ...game.position }] : prev.trail).slice(-TRAIL_LENGTH)
    : sameAttempt ? prev.trail : [{ ...game.position }];
  return { attempt: game.attempts, status: game.status, launchAt, endAt, trail };
}

/** Ages the renderer needs, in ms; null when that effect is not playing. */
export function effectAges(tracked, now) {
  if (!tracked || tracked.attempt === null) return null;
  return {
    launchAgeMs: tracked.launchAt !== null && now - tracked.launchAt < LAUNCH_EFFECT_MS ? now - tracked.launchAt : null,
    endAgeMs: tracked.endAt !== null ? now - tracked.endAt : null,
    trail: tracked.status === "running" ? tracked.trail : [],
    launchOrigin: tracked.trail[0] ?? null,      // game px
    endOrigin: tracked.trail.at(-1) ?? null,     // game px
  };
}

function polygonCenter(points) {
  const sum = points.reduce((acc, p) => ({ x: acc.x + p.x, y: acc.y + p.y }), { x: 0, y: 0 });
  return { x: sum.x / points.length, y: sum.y / points.length };
}

function polygonSize(points) {
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  return Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys));
}

function fillPolygonPath(ctx, points) {
  ctx.beginPath();
  points.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
  ctx.closePath();
}

function ring(ctx, center, radius, width, color, alpha) {
  if (!(radius > 0) || !(alpha > 0)) return;
  ctx.globalAlpha = alpha;
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.beginPath();
  ctx.arc(center.x, center.y, radius, 0, TAU);
  ctx.stroke();
}

function dot(ctx, center, radius, color, alpha) {
  if (!(radius > 0) || !(alpha > 0)) return;
  ctx.globalAlpha = alpha;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.arc(center.x, center.y, radius, 0, TAU);
  ctx.fill();
}

/**
 * The drop: a white flash over the start paper, two shock rings and a ring
 * of sparks in the start color, from the ball's first position. `scale` is
 * projector px per game px around it (the ball's radius / 10).
 */
export function drawLaunchEffect(ctx, { ageMs, origin, startPaper = null, color = "#ffd166", scale = 1 }) {
  if (!(ageMs >= 0 && ageMs < LAUNCH_EFFECT_MS) || !origin) return;
  const t = ageMs / LAUNCH_EFFECT_MS;
  if (startPaper?.length >= 3) {
    ctx.globalAlpha = 0.85 * (1 - t) ** 2;
    ctx.fillStyle = "#ffffff";
    fillPolygonPath(ctx, startPaper);
    ctx.fill();
  }
  ring(ctx, origin, scale * (12 + 70 * easeOut(t)), 4 * scale, color, 0.9 * (1 - t));
  ring(ctx, origin, scale * (8 + 45 * easeOut(Math.max(0, t - 0.15) / 0.85)), 2 * scale, "#ffffff", 0.7 * (1 - t));
  for (let i = 0; i < 12; i += 1) {
    const angle = TAU * (i / 12) + noise(i, 1) * 0.4;
    const reach = scale * (18 + 55 * easeOut(t) * (0.7 + 0.6 * noise(i, 2)));
    const spark = { x: origin.x + Math.cos(angle) * reach, y: origin.y + Math.sin(angle) * reach };
    dot(ctx, spark, scale * 3 * (1 - t), i % 3 === 0 ? "#ffffff" : color, 1 - t);
  }
  ctx.globalAlpha = 1;
}

/** The ball's trail while it falls: older positions smaller and fainter, start color to goal color. */
export function drawTrail(ctx, { points, radius, from = "#ffd166", to = "#ef476f" }) {
  if (!Array.isArray(points) || points.length < 2 || !(radius > 0)) return;
  const n = points.length;
  for (let i = 0; i < n - 1; i += 1) {
    const k = (i + 1) / n;   // 0 oldest .. 1 newest
    dot(ctx, points[i], radius * (0.25 + 0.6 * k), k < 0.5 ? from : to, 0.08 + 0.42 * k);
  }
  ctx.globalAlpha = 1;
}

/**
 * The goal: the goal paper flashes, three rings expand in party colors and
 * GOAL_PARTICLES sparks burst out and fall under their own gravity, fading.
 */
export function drawGoalEffect(ctx, { ageMs, goalPaper, colors = [], scale = 1 }) {
  if (!(ageMs >= 0 && ageMs < GOAL_EFFECT_MS) || !(goalPaper?.length >= 3)) return;
  const t = ageMs / GOAL_EFFECT_MS;
  const seconds = ageMs / 1000;
  const center = polygonCenter(goalPaper);
  const size = Math.max(polygonSize(goalPaper), 20 * scale);
  const palette = [...colors.filter((c) => /^#[0-9a-f]{6}$/i.test(c ?? "")), ...PARTY_COLORS];
  // A pulsing flash over the paper, strongest at first.
  ctx.globalAlpha = Math.max(0, 0.9 * (1 - t) * (0.6 + 0.4 * Math.cos(seconds * 18)));
  ctx.fillStyle = "#ffffff";
  fillPolygonPath(ctx, goalPaper);
  ctx.fill();
  for (let k = 0; k < 3; k += 1) {
    const local = (seconds - 0.18 * k) / 1.1;
    if (local <= 0 || local >= 1) continue;
    ring(ctx, center, size * (0.5 + 3.2 * easeOut(local)), 6 * scale * (1 - local) + 1, palette[k % palette.length],
      0.9 * (1 - local));
  }
  const gravity = 900 * scale;
  for (let i = 0; i < GOAL_PARTICLES; i += 1) {
    const angle = TAU * noise(i, 3);
    const speed = scale * (220 + 520 * noise(i, 4));
    const life = 0.9 + 1.4 * noise(i, 5);
    if (seconds >= life) continue;
    const drag = 1 - Math.min(0.6, seconds * 0.35);
    const p = {
      x: center.x + Math.cos(angle) * speed * seconds * drag,
      y: center.y + Math.sin(angle) * speed * seconds * drag + 0.5 * gravity * seconds * seconds,
    };
    const fade = 1 - seconds / life;
    dot(ctx, p, scale * (2 + 4 * noise(i, 6)) * (0.4 + 0.6 * fade), palette[i % palette.length], fade);
  }
  ctx.globalAlpha = 1;
}

/** The ball left the field: a short dim puff where it was last seen. */
export function drawLostEffect(ctx, { ageMs, origin, color = "#9aa6aa", scale = 1 }) {
  if (!(ageMs >= 0 && ageMs < LOST_EFFECT_MS) || !origin) return;
  const t = ageMs / LOST_EFFECT_MS;
  ring(ctx, origin, scale * (10 + 40 * easeOut(t)), 3 * scale, color, 0.6 * (1 - t));
  for (let i = 0; i < 8; i += 1) {
    const angle = TAU * (i / 8) + 0.3;
    const reach = scale * 30 * easeOut(t);
    dot(ctx, { x: origin.x + Math.cos(angle) * reach, y: origin.y + Math.sin(angle) * reach }, scale * 2.5 * (1 - t), color,
      0.7 * (1 - t));
  }
  ctx.globalAlpha = 1;
}
