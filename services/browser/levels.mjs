/**
 * Port of services/levels/__init__.py (contract 3.2): the levels of
 * services/levels/levels.json in one space's pixels, their obstacles on a
 * board, and how the notes on the wall stand against them. Same operations
 * in the same order, so both backends agree to the bit.
 */
import DATA from "../levels/levels.json" with { type: "json" };
import { pyRound } from "./numbers.mjs";

export const FREE_LEVEL = "libre";
export const BALL_RADIUS_PX = 10.0;
const PORTAL_CLEAR_PX = 4.0;
const COVER_NAMES = Object.freeze({ wall: "una pared", spikes: "los pinchos", portal: "un portal" });

export class LevelProblem extends Error {}

function level(entry) {
  return Object.freeze({
    id: entry.id, name: String(entry.name ?? entry.id), hint: String(entry.hint ?? ""),
    start_zone: entry.start_zone ?? null, goal_zone: entry.goal_zone ?? null,
    bumpers_max: entry.bumpers_max ?? null,
    walls: entry.walls ?? [], spikes: entry.spikes ?? [], portals: entry.portals ?? [],
    solution: entry.solution ?? [],
  });
}

/** The levels; levels.json is checked by the Python loader and its tests. */
export const LEVELS = Object.freeze(DATA.levels.map(level));

export function levelById(id) {
  return LEVELS.find((entry) => entry.id === id) ?? null;
}

export function nextLevel(current) {
  if (current.id === FREE_LEVEL) return null;
  const index = LEVELS.indexOf(current);
  return index + 1 < LEVELS.length ? LEVELS[index + 1] : null;
}

function polygon(shape, width, height) {
  let corners;
  if (shape.rect) {
    const [x0, y0, x1, y1] = shape.rect;
    corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  } else {
    corners = shape.points;
  }
  return corners.map(([x, y]) => ({ x: x * width, y: y * height }));
}

const zone = (rect, width, height) => (rect === null ? null : polygon({ rect }, width, height));

export function levelGeometry(current, width, height) {
  return {
    width, height,
    start_zone: zone(current.start_zone, width, height),
    goal_zone: zone(current.goal_zone, width, height),
    walls: current.walls.map((shape) => polygon(shape, width, height)),
    spikes: current.spikes.map((shape) => polygon(shape, width, height)),
    facings: current.spikes.map((shape) => shape.facing ?? "up"),
    portals: current.portals.map((p) => ({ entry: polygon(p.entry, width, height),
      exit: { x: p.exit[0] * width, y: p.exit[1] * height } })),
  };
}

export function withObstacles(board, geometry) {
  return { ...board, walls: geometry.walls, spikes: geometry.spikes, portals: geometry.portals };
}

// ---------------------------------------------------------------- checks

function vertexMean(points) {
  let sx = 0.0;
  let sy = 0.0;
  for (const point of points) {
    sx += point.x;
    sy += point.y;
  }
  return { x: sx / points.length, y: sy / points.length };
}

function area2(points) {
  let total = 0.0;
  points.forEach((a, k) => {
    const b = points[(k + 1) % points.length];
    total += a.x * b.y - b.x * a.y;
  });
  return total;
}

/** Inside or on the edge of a convex polygon, either winding. */
export function inside(point, points) {
  const sign = area2(points) > 0 ? 1.0 : -1.0;
  for (let k = 0; k < points.length; k += 1) {
    const a = points[k];
    const b = points[(k + 1) % points.length];
    if (sign * ((b.x - a.x) * (point.y - a.y) - (b.y - a.y) * (point.x - a.x)) < 0) return false;
  }
  return true;
}

function separated(first, second) {
  for (const points of [first, second]) {
    for (let k = 0; k < points.length; k += 1) {
      const a = points[k];
      const b = points[(k + 1) % points.length];
      const nx = b.y - a.y;
      const ny = a.x - b.x;
      const project = (p) => p.x * nx + p.y * ny;
      const one = first.map(project);
      const two = second.map(project);
      if (Math.max(...one) < Math.min(...two) || Math.max(...two) < Math.min(...one)) return true;
    }
  }
  return false;
}

/** Whether two convex polygons share any point (separating axis test). */
export function overlaps(first, second) {
  return !separated(first, second);
}

function near(point, points, distance) {
  if (inside(point, points)) return true;
  const limit = distance * distance;
  for (let k = 0; k < points.length; k += 1) {
    const a = points[k];
    const b = points[(k + 1) % points.length];
    const ex = b.x - a.x;
    const ey = b.y - a.y;
    const length2 = ex * ex + ey * ey;
    const t = length2 === 0 ? 0.0 : Math.max(0.0, Math.min(1.0, ((point.x - a.x) * ex + (point.y - a.y) * ey) / length2));
    const dx = point.x - (a.x + t * ex);
    const dy = point.y - (a.y + t * ey);
    if (dx * dx + dy * dy <= limit) return true;
  }
  return false;
}

/** services/levels `level_check`: notes are { role: [polygon] } in the geometry's space. */
export function levelCheck(current, geometry, notes) {
  const placed = (role, area) => {
    if (area === null) return null;
    const found = notes[role] ?? [];
    return found.length === 1 && inside(vertexMean(found[0]), area);
  };
  const papers = [...(notes.start ?? []), ...(notes.goal ?? []), ...(notes.bumper ?? [])];
  const covered = [];
  if (papers.some((paper) => geometry.walls.some((wall) => overlaps(paper, wall)))) covered.push("wall");
  if (papers.some((paper) => geometry.spikes.some((strip) => overlaps(paper, strip)))) covered.push("spikes");
  if (papers.some((paper) => geometry.portals.some((portal) => overlaps(paper, portal.entry)
      || near(portal.exit, paper, BALL_RADIUS_PX + PORTAL_CLEAR_PX)))) covered.push("portal");
  return { start: placed("start", geometry.start_zone), goal: placed("goal", geometry.goal_zone),
    bumpers: (notes.bumper ?? []).length, bumpers_max: current.bumpers_max, covered };
}

export function levelProblem(check) {
  if (check.start === false) return "Poné el post-it de inicio dentro de su recuadro.";
  if (check.goal === false) return "Poné el post-it de llegada dentro de su recuadro.";
  const limit = check.bumpers_max;
  const count = check.bumpers;
  if (limit !== null && count > limit) {
    if (limit === 0) return `Este nivel se juega sin rebotes: sacá ${count}.`;
    return `Este nivel deja usar ${limit} rebote${limit !== 1 ? "s" : ""}: sacá ${count - limit}.`;
  }
  if (check.covered.length) return `Un post-it tapa ${COVER_NAMES[check.covered[0]]}: movelo.`;
  return null;
}

// ---------------------------------------------------------------- json

const pointsJson = (points) => ({ points: points.map((p) => ({ x: pyRound(p.x, 3), y: pyRound(p.y, 3) })) });

export function geometryJson(geometry) {
  return {
    width: geometry.width, height: geometry.height,
    start_zone: geometry.start_zone === null ? null : pointsJson(geometry.start_zone),
    goal_zone: geometry.goal_zone === null ? null : pointsJson(geometry.goal_zone),
    walls: geometry.walls.map(pointsJson),
    spikes: geometry.spikes.map((strip, k) => ({ ...pointsJson(strip), facing: geometry.facings[k] })),
    portals: geometry.portals.map((portal) => ({ entry: pointsJson(portal.entry),
      exit: { x: pyRound(portal.exit.x, 3), y: pyRound(portal.exit.y, 3) } })),
  };
}
