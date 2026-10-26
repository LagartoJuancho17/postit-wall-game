/**
 * Port of services/physics/engine.py: swept-circle collisions with convex
 * post-it polygons, in projector pixels. Angles follow screen coordinates:
 * 0° points right and 90° points down.
 *
 * A polygon is an array of `{ x, y }`; a board is
 * `{ playfield, start, goal, bumpers: [polygon], markers: [polygon] }`.
 * States are frozen `{ position, velocity, elapsed_s, bounce_count, status }`.
 */

export const BALL_RADIUS_PX = 10.0;
// services/physics/engine.py: the web game's gravity, its bounces, its limit.
export const GRAVITY_SHARE = 1.6;
export const LAUNCH_SPEED_SHARE = 1.7;
const RESTITUTION = 0.8;
const REST_STEPS = 2.5;
export const MAX_ATTEMPT_S = 20.0;
// A ball slower than STILL_SPEED_PX_S for STALL_S has stopped: lost then.
export const STILL_SPEED_PX_S = 5.0;
export const STALL_S = 1.5;
// The web game drops the ball from rest under the start note.
export const DROP_GAP_PX = 2.0;
const DROP_NUDGE_LIMIT = 200;
const EPS = 1e-9;
const POSITION_NUDGE_PX = 1e-7;
const SIMULTANEOUS_S = 1e-9; // bumper contacts this close in time are one event
const MAX_EVENTS_PER_STEP = 1024;

export const GameStatus = Object.freeze({ READY: "ready", RUNNING: "running", WON: "won", LOST: "lost" });

const point = (x, y) => ({ x, y });
const dot = (a, b) => a.x * b.x + a.y * b.y;
const sub = (a, b) => point(a.x - b.x, a.y - b.y);
const addScaled = (a, v, t) => point(a.x + v.x * t, a.y + v.y * t);

function gameState(position, velocity, elapsed, bounces, status, gravity = 0.0, still = 0.0) {
  return Object.freeze({ position, velocity, elapsed_s: elapsed, bounce_count: bounces, status, gravity_px_s2: gravity,
    still_s: still });
}

function edges(polygon) {
  return polygon.map((a, i) => [a, polygon[(i + 1) % polygon.length]]);
}

function area2(polygon) {
  let total = 0;
  for (const [a, b] of edges(polygon)) total += a.x * b.y - b.x * a.y;
  return total;
}

function validatePolygon(polygon, label) {
  if (polygon.length < 3 || polygon.some((p) => !Number.isFinite(p.x) || !Number.isFinite(p.y))) {
    throw new Error(`${label} must have at least three finite points`);
  }
  const area = area2(polygon);
  if (Math.abs(area) < EPS) throw new Error(`${label} has zero area`);
  const sign = area > 0 ? 1 : -1;
  for (let i = 0; i < polygon.length; i += 1) {
    const a = polygon[i];
    const b = polygon[(i + 1) % polygon.length];
    const c = polygon[(i + 2) % polygon.length];
    const cross = (b.x - a.x) * (c.y - b.y) - (b.y - a.y) * (c.x - b.x);
    if (cross * sign < -EPS) throw new Error(`${label} must be convex and ordered`);
    if ((b.x - a.x) ** 2 + (b.y - a.y) ** 2 < EPS) throw new Error(`${label} has a repeated point`);
  }
}

function validateBoard(board) {
  validatePolygon(board.playfield, "playfield");
  validatePolygon(board.start, "start");
  validatePolygon(board.goal, "goal");
  board.bumpers.forEach((polygon, i) => validatePolygon(polygon, `bumper ${i}`));
}

function centroid(polygon) {
  const a2 = area2(polygon);
  let cx = 0;
  let cy = 0;
  for (const [a, b] of edges(polygon)) cx += (a.x + b.x) * (a.x * b.y - b.x * a.y);
  for (const [a, b] of edges(polygon)) cy += (a.y + b.y) * (a.x * b.y - b.x * a.y);
  return point(cx / (3 * a2), cy / (3 * a2));
}

function edgeNormals(polygon) {
  const orientation = area2(polygon) > 0 ? 1 : -1;
  return edges(polygon).map(([a, b]) => {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const length = Math.hypot(dx, dy);
    return [a, b, point(orientation * dy / length, -orientation * dx / length)];
  });
}

function fitsPlayfield(position, playfield) {
  return edgeNormals(playfield).every(([a, , normal]) => dot(sub(position, a), normal) <= -BALL_RADIUS_PX + EPS);
}

function distanceSquaredToSegment(p, a, b) {
  const edge = sub(b, a);
  const parameter = Math.max(0.0, Math.min(1.0, dot(sub(p, a), edge) / dot(edge, edge)));
  const near = addScaled(a, edge, parameter);
  const delta = sub(p, near);
  return dot(delta, delta);
}

function overlapsPolygon(position, polygon) {
  if (edgeNormals(polygon).every(([a, , n]) => dot(sub(position, a), n) <= EPS)) return true;
  const radius2 = BALL_RADIUS_PX * BALL_RADIUS_PX;
  return edges(polygon).some(([a, b]) => distanceSquaredToSegment(position, a, b) <= radius2 + EPS);
}

/** A motionless ball at the area centroid of the start note. */
export function readyState(board) {
  validateBoard(board);
  const position = centroid(board.start);
  if (!fitsPlayfield(position, board.playfield)) {
    throw new Error("start centroid must fit inside playfield with ball radius");
  }
  if (overlapsPolygon(position, board.goal)) throw new Error("start overlaps goal");
  if (board.bumpers.some((obstacle) => overlapsPolygon(position, obstacle))) {
    throw new Error("start overlaps a bumper");
  }
  return gameState(position, point(0.0, 0.0), 0.0, 0, GameStatus.READY);
}

/**
 * Where a dropped ball starts (engine.py `drop_point`): the start note's
 * lowest edge point on the vertical through its centroid, plus the radius
 * and DROP_GAP_PX, then 1 px lower at a time while it still touches the note.
 */
export function dropPoint(board) {
  validateBoard(board);
  const center = centroid(board.start);
  let bottom = -Infinity;
  for (const [a, b] of edges(board.start)) {
    if (a.x === b.x) continue;
    const [low, high] = a.x < b.x ? [a, b] : [b, a];
    if (low.x <= center.x && center.x <= high.x) {
      bottom = Math.max(bottom, low.y + (center.x - low.x) * (high.y - low.y) / (high.x - low.x));
    }
  }
  if (!Number.isFinite(bottom)) bottom = Math.max(...board.start.map((p) => p.y));
  let position = point(center.x, bottom + BALL_RADIUS_PX + DROP_GAP_PX);
  for (let step = 0; step < DROP_NUDGE_LIMIT; step += 1) {
    if (!overlapsPolygon(position, board.start)) break;
    position = point(position.x, position.y + 1.0);
  }
  return position;
}

/** A motionless ball at `dropPoint`, checked like `readyState`. */
export function dropReadyState(board) {
  const position = dropPoint(board);
  if (!fitsPlayfield(position, board.playfield)) {
    throw new Error("the ball must fit inside the playfield under the start note");
  }
  if (overlapsPolygon(position, board.goal)) throw new Error("start overlaps goal");
  if (board.bumpers.some((obstacle) => overlapsPolygon(position, obstacle))) {
    throw new Error("start overlaps a bumper");
  }
  return gameState(position, point(0.0, 0.0), 0.0, 0, GameStatus.READY);
}

/** Release the ball at rest under the start note; gravity pulls it down. */
export function drop(board, gravityPxS2) {
  if (!Number.isFinite(gravityPxS2) || gravityPxS2 <= 0) throw new Error("gravity_px_s2 must be positive and finite");
  const state = dropReadyState(board);
  return gameState(state.position, state.velocity, state.elapsed_s, state.bounce_count, GameStatus.RUNNING, gravityPxS2);
}

/**
 * Launch from the start note. An angle of 0° aims right; 90° aims down.
 * `gravityPxS2` pulls toward +y (down the wall); 0 keeps the classic motion.
 */
export function launch(board, angleDegrees, speedPxS, gravityPxS2 = 0.0) {
  if (!Number.isFinite(angleDegrees) || !Number.isFinite(speedPxS) || speedPxS <= 0) {
    throw new Error("angle must be finite and speed_px_s must be positive and finite");
  }
  if (!Number.isFinite(gravityPxS2) || gravityPxS2 < 0) throw new Error("gravity_px_s2 must be nonnegative and finite");
  const state = readyState(board);
  const angle = angleDegrees * (Math.PI / 180);
  return gameState(state.position, point(speedPxS * Math.cos(angle), speedPxS * Math.sin(angle)),
    state.elapsed_s, state.bounce_count, GameStatus.RUNNING, gravityPxS2);
}

function sweptPolygonHit(position, velocity, polygon) {
  let best = null;
  const consider = (hit) => {
    if (best === null || hit.time < best.time) best = hit;
  };
  for (const [a, b, normal] of edgeNormals(polygon)) {
    const distance = dot(sub(position, a), normal);
    const approach = dot(velocity, normal);
    if (distance >= BALL_RADIUS_PX - EPS && approach < -EPS) {
      const t = (BALL_RADIUS_PX - distance) / approach;
      if (t >= -EPS) {
        const q = addScaled(position, velocity, Math.max(0.0, t));
        const edge = sub(b, a);
        const u = dot(sub(q, a), edge) / dot(edge, edge);
        if (u >= -EPS && u <= 1.0 + EPS) consider({ time: Math.max(0.0, t), normal, kind: "polygon" });
      }
    }
  }
  const speed2 = dot(velocity, velocity);
  if (speed2 > EPS) {
    for (const vertex of polygon) {
      const relative = sub(position, vertex);
      const c = dot(relative, relative) - BALL_RADIUS_PX ** 2;
      if (c < -EPS) continue;
      const b = 2 * dot(relative, velocity);
      const discriminant = b * b - 4 * speed2 * c;
      if (discriminant < 0) continue;
      const t = (-b - Math.sqrt(Math.max(0.0, discriminant))) / (2 * speed2);
      if (t < -EPS) continue;
      const q = addScaled(position, velocity, Math.max(0.0, t));
      const radial = sub(q, vertex);
      const radialLength = Math.hypot(radial.x, radial.y);
      if (radialLength > EPS) {
        const normal = point(radial.x / radialLength, radial.y / radialLength);
        if (dot(velocity, normal) < -EPS) consider({ time: Math.max(0.0, t), normal, kind: "polygon" });
      }
    }
  }
  return best;
}

function playfieldExit(position, velocity, playfield) {
  let best = null;
  for (const [a, , normal] of edgeNormals(playfield)) {
    const distance = dot(sub(position, a), normal);
    const outwardSpeed = dot(velocity, normal);
    if (outwardSpeed > EPS) {
      const t = (-BALL_RADIUS_PX - distance) / outwardSpeed;
      if (t >= -EPS && (best === null || Math.max(0.0, t) < best.time)) {
        best = { time: Math.max(0.0, t), normal, kind: "exit" };
      }
    }
  }
  return best;
}

const PRIORITY = { goal: 0, exit: 1, bumper: 2 };

/**
 * Advance a fixed board by `dtS`; terminal states remain unchanged. Goal
 * overlap wins; crossing the playable inset loses; a bumper reflects.
 */
/** A new resting surface for the rest of a step: keep the newest two (a V holds the ball). */
function restOn(supports, normal) {
  if (!supports.some((kept) => dot(normal, kept) > 1 - 1e-9)) {
    supports.push(normal);
    if (supports.length > 2) supports.shift();
  }
}

export function step(board, state, dtS) {
  if (!Number.isFinite(dtS) || dtS < 0) throw new Error("dt_s must be nonnegative and finite");
  if (state.status !== GameStatus.RUNNING || dtS === 0) return state;
  if (!fitsPlayfield(state.position, board.playfield)) {
    return gameState(state.position, state.velocity, state.elapsed_s, state.bounce_count, GameStatus.LOST,
      state.gravity_px_s2 ?? 0.0, state.still_s ?? 0.0);
  }
  if (overlapsPolygon(state.position, board.goal)) {
    return gameState(state.position, state.velocity, state.elapsed_s, state.bounce_count, GameStatus.WON,
      state.gravity_px_s2 ?? 0.0, state.still_s ?? 0.0);
  }
  const gravity = state.gravity_px_s2 ?? 0.0;
  const stillBefore = state.still_s ?? 0.0;
  if (gravity > 0 && (state.elapsed_s >= MAX_ATTEMPT_S || stillBefore >= STALL_S)) {
    return gameState(state.position, state.velocity, state.elapsed_s, state.bounce_count, GameStatus.LOST, gravity,
      stillBefore);
  }
  let { position, velocity } = state;
  let elapsed = state.elapsed_s;
  let bounces = state.bounce_count;
  let remaining = dtS;
  const supports = []; // surfaces the ball rests on for the rest of this step (at most 2)
  const slide = (vector, normal) => {
    const into = dot(vector, normal);
    return into >= 0 ? vector : point(vector.x - into * normal.x, vector.y - into * normal.y);
  };
  // Without its components into the supporting surfaces: the ball slides
  // along one, or stops when it rests on two that both hold it (a V).
  const along = (vector) => {
    if (!supports.length) return vector;
    if (supports.length === 1) return slide(vector, supports[0]);
    const [first, second] = supports;
    if (dot(vector, first) >= 0 && dot(vector, second) >= 0) return vector;
    for (const [normal, other] of [[first, second], [second, first]]) {
      const moved = slide(vector, normal);
      if (dot(moved, other) >= -EPS && dot(moved, normal) >= -EPS) return moved;
    }
    return point(0.0, 0.0);
  };
  // The chord with the mean velocity ends exactly on the parabola.
  const chordOver = (travel) => along(point(velocity.x, velocity.y + 0.5 * gravity * travel));
  const running = (travel) => {
    const end = along(point(velocity.x, velocity.y + gravity * travel));
    const speed = Math.sqrt(end.x * end.x + end.y * end.y);
    const still = gravity > 0 && speed < STILL_SPEED_PX_S ? stillBefore + dtS : 0.0;
    return gameState(addScaled(position, chordOver(travel), travel), end, elapsed + travel, bounces, GameStatus.RUNNING,
      gravity, still);
  };
  for (let event = 0; event < MAX_EVENTS_PER_STEP; event += 1) {
    const chord = chordOver(remaining);
    const events = [];
    const goal = sweptPolygonHit(position, chord, board.goal);
    if (goal !== null) events.push({ ...goal, kind: "goal" });
    for (const obstacle of board.bumpers) {
      const hit = sweptPolygonHit(position, chord, obstacle);
      if (hit !== null) events.push({ ...hit, kind: "bumper" });
    }
    const exitHit = playfieldExit(position, chord, board.playfield);
    if (exitHit !== null) events.push(exitHit);
    if (!events.length) return running(remaining);
    // A simultaneous goal contact takes priority over a bumper or boundary.
    let hit = events[0];
    for (const candidate of events.slice(1)) {
      if (candidate.time < hit.time || (candidate.time === hit.time && PRIORITY[candidate.kind] < PRIORITY[hit.kind])) {
        hit = candidate;
      }
    }
    if (hit.time > remaining + EPS) return running(remaining);
    const travel = Math.min(hit.time, remaining);
    position = addScaled(position, chord, travel);
    velocity = along(point(velocity.x, velocity.y + gravity * travel));
    elapsed += travel;
    remaining -= travel;
    if (hit.kind === "goal") return gameState(position, velocity, elapsed, bounces, GameStatus.WON, gravity, stillBefore);
    if (hit.kind === "exit") return gameState(position, velocity, elapsed, bounces, GameStatus.LOST, gravity, stillBefore);
    // Every bumper touched at this same instant answers together: a ball
    // dropped into a symmetric V touches both faces at once.
    const contacts = events.filter((candidate) => candidate.kind === "bumper" && candidate.time <= hit.time + SIMULTANEOUS_S)
      .map((candidate) => candidate.normal);
    let bounced = false;
    for (const normal of contacts) {
      const into = dot(velocity, normal);
      if (into >= 0) {
        // Already leaving: rest on it so the chord slides along it (a rounded corner).
        if (gravity > 0) restOn(supports, normal);
        continue;
      }
      let resting = false;
      if (gravity > 0) {
        // Lose part of the normal speed; too weak a bounce rests and slides.
        let out = -RESTITUTION * into;
        resting = out < REST_STEPS * gravity * dtS;
        if (resting) out = 0.0;
        velocity = point(velocity.x + (out - into) * normal.x, velocity.y + (out - into) * normal.y);
        if (resting) {
          restOn(supports, normal);
          velocity = along(velocity);
        }
      } else {
        velocity = sub(velocity, point(2 * into * normal.x, 2 * into * normal.y));
      }
      bounced = bounced || !resting; // resting contact is not a bounce
    }
    for (const normal of contacts) position = addScaled(position, normal, POSITION_NUDGE_PX);
    if (bounced) bounces += 1;
    if (remaining <= EPS) return running(remaining);
  }
  throw new Error(`more than ${MAX_EVENTS_PER_STEP} collisions in one step; reduce dt_s or speed`);
}
