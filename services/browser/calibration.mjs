/**
 * Port of contracts/geometry.py and the parts of services/calibration the
 * web game uses: the camera → projector homography from the four corner
 * notes, and mapping a whole board through it.
 *
 * `estimateHomography` calls OpenCV's own findHomography (opencv.js, same
 * 4.12 release as the Python side), so the matrix is the one Python gets.
 */

const EPS64 = 2.220446049250313e-16;

/** Intersection of diagonals, including perspective-shaped rectangles. */
export function quadrilateralCenter(points) {
  if (points.length !== 4) throw new Error("a quadrilateral needs exactly four vertices");
  const [a, b, c, d] = points;
  const rx = c.x - a.x;
  const ry = c.y - a.y;
  const sx = d.x - b.x;
  const sy = d.y - b.y;
  const denominator = rx * sy - ry * sx;
  if (Math.abs(denominator) < 1e-9) throw new Error("quadrilateral diagonals do not intersect uniquely");
  const qx = b.x - a.x;
  const qy = b.y - a.y;
  const factor = (qx * sy - qy * sx) / denominator;
  return { x: a.x + factor * rx, y: a.y + factor * ry };
}

function pointArray(points, name) {
  if (!Array.isArray(points) || points.some((p) => !p || !Number.isFinite(p.x) || !Number.isFinite(p.y))) {
    throw new Error(`${name} must contain finite Point values`);
  }
  return points.map((p) => [p.x, p.y]);
}

function distinct(rows) {
  return new Set(rows.map(([x, y]) => `${x === 0 ? 0 : x},${y === 0 ? 0 : y}`)).size;
}

/** Singular values of a 3 × 3 matrix by one-sided Jacobi (accurate when tiny). */
export function singularValues3(matrix) {
  const u = matrix.map((row) => [...row]);
  for (let sweep = 0; sweep < 60; sweep += 1) {
    let rotated = false;
    for (let p = 0; p < 2; p += 1) {
      for (let q = p + 1; q < 3; q += 1) {
        let alpha = 0;
        let beta = 0;
        let gamma = 0;
        for (let i = 0; i < 3; i += 1) {
          alpha += u[i][p] * u[i][p];
          beta += u[i][q] * u[i][q];
          gamma += u[i][p] * u[i][q];
        }
        if (gamma === 0 || Math.abs(gamma) <= EPS64 * Math.sqrt(alpha * beta)) continue;
        rotated = true;
        const zeta = (beta - alpha) / (2 * gamma);
        const t = Math.sign(zeta || 1) / (Math.abs(zeta) + Math.sqrt(1 + zeta * zeta));
        const c = 1 / Math.sqrt(1 + t * t);
        const s = c * t;
        for (let i = 0; i < 3; i += 1) {
          const up = u[i][p];
          const uq = u[i][q];
          u[i][p] = c * up - s * uq;
          u[i][q] = s * up + c * uq;
        }
      }
    }
    if (!rotated) break;
  }
  return [0, 1, 2].map((j) => Math.hypot(u[0][j], u[1][j], u[2][j]));
}

/** numpy.linalg.matrix_rank for a 3 × 3 matrix. */
export function matrixRank3(matrix) {
  const values = singularValues3(matrix);
  const tolerance = Math.max(...values) * 3 * EPS64;
  return values.filter((value) => value > tolerance).length;
}

function finiteMatrix(matrix) {
  if (!Array.isArray(matrix) || matrix.length !== 3 ||
      matrix.some((row) => !Array.isArray(row) || row.length !== 3 || row.some((v) => !Number.isFinite(v)))) {
    throw new Error("homography must be a finite 3 × 3 matrix");
  }
  return matrix;
}

/**
 * Camera → projector mapping from matching pixel locations. With more than
 * four observations RANSAC rejects outliers (3 projector pixels). Returns a
 * 3 × 3 array normalized so the bottom-right entry is 1.
 */
export function estimateHomography(cv, cameraPoints, projectorPoints) {
  const source = pointArray(cameraPoints, "camera_points");
  const target = pointArray(projectorPoints, "projector_points");
  if (source.length !== target.length) throw new Error("camera_points and projector_points must have equal length");
  if (source.length < 4) throw new Error("at least four point correspondences are required");
  if (distinct(source) < 4 || distinct(target) < 4) {
    throw new Error("at least four distinct points are required in each image");
  }
  const method = source.length > 4 ? cv.RANSAC : 0;
  const src = cv.matFromArray(source.length, 1, cv.CV_64FC2, source.flat());
  const dst = cv.matFromArray(target.length, 1, cv.CV_64FC2, target.flat());
  const mask = new cv.Mat();
  let found = null;
  try {
    found = cv.findHomography(src, dst, method, 3.0, mask);
  } catch {
    found = null;
  }
  try {
    if (found === null || found.empty() || found.rows !== 3 || found.cols !== 3 ||
        mask.empty() || cv.countNonZero(mask) < 4) {
      throw new Error("point correspondences do not define a homography");
    }
    let matrix = finiteMatrix([0, 1, 2].map((r) => [0, 1, 2].map((c) => found.doubleAt(r, c))));
    if (matrixRank3(matrix) < 3) throw new Error("point correspondences define a degenerate homography");
    // Scale does not change the mapping. Normalizing makes saved values stable.
    const scale = Math.abs(matrix[2][2]) > 1e-12 ? matrix[2][2] : Math.hypot(...matrix.flat());
    matrix = matrix.map((row) => row.map((value) => value / scale));
    return matrix;
  } finally {
    src.delete();
    dst.delete();
    mask.delete();
    found?.delete?.();
  }
}

/** Map a camera pixel; reject positions that map to infinity. */
export function transformPoint(point, matrix) {
  if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error("point coordinates must be finite");
  const m = finiteMatrix(matrix);
  const mapped = m.map((row) => row[0] * point.x + row[1] * point.y + row[2] * 1.0);
  if (Math.abs(mapped[2]) <= 1e-12) throw new Error("point maps outside the finite projector plane");
  const x = mapped[0] / mapped[2];
  const y = mapped[1] / mapped[2];
  if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error("point maps outside the finite projector plane");
  return { x, y };
}

/** Map every board polygon from camera pixels to projector pixels. */
export function transformBoard(board, matrix) {
  const mapped = (polygon) => polygon.map((p) => transformPoint(p, matrix));
  return {
    playfield: mapped(board.playfield),
    start: mapped(board.start),
    goal: mapped(board.goal),
    bumpers: board.bumpers.map(mapped),
    markers: board.markers.map(mapped),
  };
}
