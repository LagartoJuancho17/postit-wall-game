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
export const GOAL_PARTICLES = 160;
export const GOAL_RAYS = 14;
export const PARTY_COLORS = ["#ffd166", "#06d6a0", "#4cc9f0", "#ef476f", "#f72585", "#b8f2e6", "#ffffff"];
// Package 1 (Tobias, 2026-10-27: "feedback en la pared al pegar post-its,
// cuenta regresiva, juice de rebotes"; contract 3.1).
export const HIT_EFFECT_MS = 450;         // a bounce's sparks and flash
export const MAX_HITS = 6;
export const APPEAR_EFFECT_MS = 750;      // the ripple around a post-it just stuck on the wall
export const APPEAR_FORGET_MS = 1500;     // a note unseen this long is new when it comes back
export const APPEAR_NEAR_SHARE = 0.04;    // of the camera's short side: the same note, slightly moved

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
  const empty = { attempt: null, status: null, launchAt: null, endAt: null, trail: [], bounces: 0, at: null,
    hits: [] };
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
  // A bounce between two states: where the ball is now, and how fast it came
  // (game px/s over the last state interval) for the size of the burst.
  const bounces = game.bounces ?? 0;
  let hits = sameAttempt ? (prev.hits ?? []).filter((hit) => now - hit.at < HIT_EFFECT_MS) : [];
  if (sameAttempt && bounces > (prev.bounces ?? 0) && last && prev.at !== null && now > prev.at) {
    const speed = Math.hypot(game.position.x - last.x, game.position.y - last.y) / ((now - prev.at) / 1000);
    hits = [...hits, { at: now, x: game.position.x, y: game.position.y, speed }].slice(-MAX_HITS);
  }
  return { attempt: game.attempts, status: game.status, launchAt, endAt, trail, bounces, at: now, hits };
}

/**
 * Notes that just appeared on the wall (camera px). `known` are the notes
 * seen within APPEAR_FORGET_MS, so a detection that flickers for a frame
 * never ripples again; a window that just opened knows what is already
 * there. `detection` is state.live_detection.
 */
export function trackAppearances(previous, detection, now) {
  const notes = [];
  for (const [role, entry] of Object.entries(detection?.roles ?? {})) {
    for (const shape of entry?.notes ?? []) {
      const points = shape?.points;
      if (!Array.isArray(points) || points.length < 3) continue;
      notes.push({ role, ...polygonCenter(points) });
    }
  }
  const near = APPEAR_NEAR_SHARE * Math.min(detection?.width || 0, detection?.height || 0);
  // The first detection this window sees is what was already there: no ripples.
  if (!previous) return detection?.roles ? { known: notes.map((note) => ({ ...note, seen: now })), appears: [] } : null;
  if (!detection?.roles) return { ...previous, appears: previous.appears.filter((a) => now - a.at < APPEAR_EFFECT_MS) };
  const known = previous.known.filter((note) => now - note.seen < APPEAR_FORGET_MS).map((note) => ({ ...note }));
  const appears = previous.appears.filter((a) => now - a.at < APPEAR_EFFECT_MS);
  for (const note of notes) {
    const match = known.find((k) => k.role === note.role && Math.hypot(k.x - note.x, k.y - note.y) <= near);
    if (match) {
      Object.assign(match, { x: note.x, y: note.y, seen: now });
    } else {
      known.push({ ...note, seen: now });
      appears.push({ ...note, at: now });
    }
  }
  return { known, appears };
}

/** Ripples still playing: [{role, x, y (camera px), ageMs}]. */
export function appearAges(tracked, now) {
  return (tracked?.appears ?? []).map((a) => ({ role: a.role, x: a.x, y: a.y, ageMs: now - a.at }))
    .filter((a) => a.ageMs >= 0 && a.ageMs < APPEAR_EFFECT_MS);
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
    hits: (tracked.hits ?? []).map((hit) => ({ ageMs: now - hit.at, origin: { x: hit.x, y: hit.y }, speed: hit.speed }))
      .filter((hit) => hit.ageMs >= 0 && hit.ageMs < HIT_EFFECT_MS),
  };
}

/** 3, 2, 1 from the seconds left: the beat (3..1, 0 when done) and how far into it (0..1). */
export function countdownBeat(remainingS, totalS, beats = 3) {
  if (!(remainingS > 0) || !(totalS > 0)) return { beat: 0, within: 1 };
  const length = totalS / beats;
  // 1.6 s of 2.4 is the start of beat 2, though 1.6 / 0.8 is 2.0000000000000004.
  const beat = Math.min(beats, Math.max(1, Math.ceil(remainingS / length - 1e-9)));
  return { beat, within: Math.min(1, Math.max(0, 1 - (remainingS - (beat - 1) * length) / length)) };
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
  dot(ctx, origin, scale * 26 * (1 - t), "#ffffff", 0.55 * (1 - t));   // the pop of light at the ball
  ring(ctx, origin, scale * (14 + 120 * easeOut(t)), 9 * scale * (1 - t) + 2, color, 0.95 * (1 - t));
  ring(ctx, origin, scale * (10 + 80 * easeOut(Math.max(0, t - 0.12) / 0.88)), 4 * scale, "#ffffff", 0.8 * (1 - t));
  for (let i = 0; i < 18; i += 1) {
    const angle = TAU * (i / 18) + noise(i, 1) * 0.35;
    const reach = scale * (22 + 105 * easeOut(t) * (0.7 + 0.6 * noise(i, 2)));
    const spark = { x: origin.x + Math.cos(angle) * reach, y: origin.y + Math.sin(angle) * reach };
    dot(ctx, spark, scale * 6 * (1 - t) + 1, i % 3 === 0 ? "#ffffff" : color, 1 - t);
  }
  ctx.globalAlpha = 1;
}

/** The ball's trail while it falls: older positions smaller and fainter, start color to goal color. */
export function drawTrail(ctx, { points, radius, from = "#ffd166", to = "#ef476f" }) {
  if (!Array.isArray(points) || points.length < 2 || !(radius > 0)) return;
  const n = points.length;
  for (let i = 0; i < n - 1; i += 1) {
    const k = (i + 1) / n;   // 0 oldest .. 1 newest
    dot(ctx, points[i], radius * (0.45 + 0.75 * k), k < 0.5 ? from : to, 0.12 + 0.5 * k);
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
  // Rays of light from the goal, turning slowly, for the first second.
  if (seconds < 1.2) {
    const fade = 1 - seconds / 1.2;
    const length = size * (1.2 + 5 * easeOut(Math.min(1, seconds / 0.5)));
    for (let k = 0; k < GOAL_RAYS; k += 1) {
      const angle = TAU * (k / GOAL_RAYS) + seconds * 0.8;
      ctx.globalAlpha = 0.55 * fade;
      ctx.strokeStyle = palette[k % palette.length];
      ctx.lineWidth = scale * (10 * fade + 2);
      ctx.beginPath();
      ctx.moveTo(center.x + Math.cos(angle) * size * 0.6, center.y + Math.sin(angle) * size * 0.6);
      ctx.lineTo(center.x + Math.cos(angle) * length, center.y + Math.sin(angle) * length);
      ctx.stroke();
    }
  }
  for (let k = 0; k < 4; k += 1) {
    const local = (seconds - 0.2 * k) / 1.3;
    if (local <= 0 || local >= 1) continue;
    ring(ctx, center, size * (0.6 + 5 * easeOut(local)), 14 * scale * (1 - local) + 2, palette[k % palette.length],
      0.95 * (1 - local));
  }
  const gravity = 700 * scale;
  for (let i = 0; i < GOAL_PARTICLES; i += 1) {
    const angle = TAU * noise(i, 3);
    const speed = scale * (260 + 760 * noise(i, 4));
    const life = 1.0 + 1.35 * noise(i, 5);
    if (seconds >= life) continue;
    const drag = 1 - Math.min(0.6, seconds * 0.35);
    const p = {
      x: center.x + Math.cos(angle) * speed * seconds * drag,
      y: center.y + Math.sin(angle) * speed * seconds * drag + 0.5 * gravity * seconds * seconds,
    };
    const fade = 1 - seconds / life;
    dot(ctx, p, scale * (4 + 7 * noise(i, 6)) * (0.45 + 0.55 * fade), palette[i % palette.length], fade);
  }
  ctx.globalAlpha = 1;
}

/** The ball left the field: a short dim puff where it was last seen. */
export function drawLostEffect(ctx, { ageMs, origin, color = "#9aa6aa", scale = 1 }) {
  if (!(ageMs >= 0 && ageMs < LOST_EFFECT_MS) || !origin) return;
  const t = ageMs / LOST_EFFECT_MS;
  ring(ctx, origin, scale * (12 + 60 * easeOut(t)), 5 * scale, color, 0.6 * (1 - t));
  for (let i = 0; i < 8; i += 1) {
    const angle = TAU * (i / 8) + 0.3;
    const reach = scale * 30 * easeOut(t);
    dot(ctx, { x: origin.x + Math.cos(angle) * reach, y: origin.y + Math.sin(angle) * reach }, scale * 2.5 * (1 - t), color,
      0.7 * (1 - t));
  }
  ctx.globalAlpha = 1;
}

/**
 * A bounce: the bumper paper flashes, a ring and sparks in its color burst
 * from where the ball hit. `intensity` 0..1 from the impact speed.
 */
export function drawHitEffect(ctx, { ageMs, origin, paper = null, color = "#77ec8f", scale = 1, intensity = 0.5 }) {
  if (!(ageMs >= 0 && ageMs < HIT_EFFECT_MS) || !origin) return;
  const t = ageMs / HIT_EFFECT_MS;
  const power = Math.min(1, Math.max(0.15, intensity));
  if (paper?.length >= 3) {
    ctx.globalAlpha = (0.35 + 0.45 * power) * (1 - t) ** 2;
    ctx.fillStyle = "#ffffff";
    fillPolygonPath(ctx, paper);
    ctx.fill();
  }
  ring(ctx, origin, scale * (8 + (30 + 50 * power) * easeOut(t)), scale * (3 + 4 * power) * (1 - t) + 1, color,
    0.9 * (1 - t));
  const sparks = 6 + Math.round(10 * power);
  for (let i = 0; i < sparks; i += 1) {
    const angle = TAU * (i / sparks) + noise(i, 9) * 0.6;
    const reach = scale * (10 + (35 + 55 * power) * easeOut(t) * (0.6 + 0.8 * noise(i, 10)));
    const spark = { x: origin.x + Math.cos(angle) * reach, y: origin.y + Math.sin(angle) * reach };
    dot(ctx, spark, scale * (2 + 3 * power) * (1 - t) + 0.5, i % 4 === 0 ? "#ffffff" : color, 1 - t);
  }
  ctx.globalAlpha = 1;
}

/**
 * The countdown, while the camera is paused (contract 3.1): the start paper
 * pulses on every beat, a ring closes on the spot the ball drops from and the
 * beat's number pops there in the start color.
 */
export function drawCountdown(ctx, { remainingS, totalS, beats = 3, origin, startPaper = null, color = "#ffd166",
  scale = 1, font = "sans-serif" }) {
  const { beat, within } = countdownBeat(remainingS, totalS, beats);
  if (!beat || !origin) return 0;
  if (startPaper?.length >= 3) {
    ctx.globalAlpha = 0.6 * (1 - within) ** 2;
    ctx.fillStyle = "#ffffff";
    fillPolygonPath(ctx, startPaper);
    ctx.fill();
  }
  ring(ctx, origin, scale * (14 + 70 * (1 - easeOut(within))), scale * 5, color, 0.35 + 0.6 * (1 - within));
  ring(ctx, origin, scale * 14, scale * 2, "#ffffff", 0.5);
  const pop = 1.3 - 0.3 * easeOut(Math.min(1, within * 4));
  ctx.globalAlpha = 1 - 0.55 * within;
  ctx.fillStyle = color;
  ctx.font = `800 ${Math.round(scale * 64 * pop)}px ${font}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(String(beat), origin.x, origin.y + scale * 70);
  ctx.globalAlpha = 1;
  return beat;
}
