/** Live camera coordinates are the only game/projector board coordinates. */
import assert from "node:assert/strict";
import test from "node:test";
import { GameController } from "../controller.mjs";
import { frameMat, loadCv, loadGolden } from "./golden.mjs";

const golden = loadGolden();
const cv = await loadCv();
function setup() {
  const now = { value: 0 };
  const game = new GameController({ clock: () => now.value });
  game.cv = cv;
  const frames = [];
  const frame = (name) => { const mat = frameMat(cv, golden, name); frames.push(mat); return mat; };
  return { game, now, frame, close: () => frames.forEach((mat) => mat.delete()) };
}
const act = (game, action, fields = {}) => game.action({ action, ...fields });

test("live preview prepares a launchable camera board before projector opens", () => {
  const env = setup();
  try {
    const preview = env.game.preview(env.frame("wall_430"));
    let state = env.game.snapshot();
    assert.equal(state.phase, "ready");
    assert.deepEqual(state.live_detection, preview);
    assert.deepEqual(state.board, state.camera_board);
    assert.deepEqual(state.board.playfield.points, [{ x: 0, y: 0 }, { x: 960, y: 0 }, { x: 960, y: 720 }, { x: 0, y: 720 }]);
    assert.ok(!("calibration" in state));
    assert.ok(!("target" in state.projection));
    preview.roles.start.notes[0].points[0].x = -999;
    assert.notDeepEqual(env.game.snapshot().live_detection, preview, "responses cannot mutate persisted detections");
    state = act(env.game, "projection_open", { width: 1280, height: 800 });
    assert.equal(state.projection.mode, "live");
    assert.equal(act(env.game, "launch").phase, "running");
  } finally { env.close(); }
});

test("four corner colors are optional and the frame rectangle is the playfield", () => {
  const env = setup();
  try {
    const frame = env.frame("wall_430");
    for (const [x, y] of [[90, 90], [870, 90], [870, 630], [90, 630]]) {
      cv.rectangle(frame, new cv.Point(x - 25, y - 25), new cv.Point(x + 25, y + 25), new cv.Scalar(220, 220, 220), -1);
    }
    const preview = env.game.preview(frame);
    assert.equal(preview.quality.ready, true);
    assert.equal(env.game.snapshot().board.markers.length, 0);
    assert.equal(preview.roles.marker.expected, null);
    assert.equal(env.game.snapshot().phase, "ready");
  } finally { env.close(); }
});

test("invalid live detections invalidate a ready board and cannot launch stale geometry", () => {
  const env = setup();
  try {
    env.game.preview(env.frame("wall_430"));
    act(env.game, "projection_open", { width: 1280, height: 800 });
    env.game.preview(env.frame("missing_start"));
    const state = env.game.snapshot();
    assert.equal(state.board, null);
    assert.equal(state.camera_board, null);
    assert.equal(state.live_detection.roles.start.count, 0);
    assert.throws(() => act(env.game, "launch"), /inicio y una llegada/);
    assert.throws(() => act(env.game, "reset"), /inicio y una llegada/);
  } finally { env.close(); }
});

test("running and paused attempts retain their collision board while overlays move live", () => {
  const env = setup();
  try {
    env.game.preview(env.frame("wall_430"));
    act(env.game, "projection_open", { width: 1280, height: 800 });
    act(env.game, "aim", { degrees: 0 });
    act(env.game, "launch");
    const launchBoard = env.game.snapshot().board;
    const ball = env.game.snapshot().game.position;
    env.game.preview(env.frame("wall_540"));
    let state = env.game.snapshot();
    assert.deepEqual(state.board, launchBoard);
    assert.deepEqual(state.game.position, ball);
    assert.notDeepEqual(state.camera_board.bumpers, launchBoard.bumpers);
    assert.deepEqual(state.live_detection.roles.bumper.notes, state.camera_board.bumpers);
    act(env.game, "pause");
    env.game.preview(env.frame("missing_start"));
    state = env.game.snapshot();
    assert.equal(state.phase, "paused");
    assert.deepEqual(state.board, launchBoard);
    assert.equal(state.camera_board, null);
    assert.throws(() => act(env.game, "reset"), /inicio y una llegada/);
    env.game.preview(env.frame("wall_540"));
    state = act(env.game, "reset");
    assert.equal(state.phase, "ready");
    assert.notDeepEqual(state.board.bumpers, launchBoard.bumpers);
  } finally { env.close(); }
});

test("projector resize preserves attempt and camera geometry; speed uses camera pixels", () => {
  const env = setup();
  try {
    env.game.preview(env.frame("wall_430"));
    act(env.game, "projection_open", { width: 1280, height: 800 });
    act(env.game, "launch");
    const before = env.game.snapshot();
    const after = act(env.game, "projection_open", { width: 1920, height: 1080 });
    assert.deepEqual(after.board, before.board);
    assert.deepEqual(after.game, before.game);
    assert.deepEqual(after.live_detection, before.live_detection);
    assert.equal(after.projection.mode, "live");
    assert.ok(Math.abs(Math.hypot(env.game.game.velocity.x, env.game.game.velocity.y) - 720 * 0.4) < 1e-8);
  } finally { env.close(); }
});

test("detections expire after 1500 ms while an active attempt remains frozen", () => {
  const env = setup();
  try {
    env.game.preview(env.frame("wall_430"));
    env.now.value = 1.5;
    assert.ok(env.game.snapshot().live_detection, "the 1500 ms boundary is inclusive");
    env.now.value = 1.501;
    let state = env.game.snapshot();
    assert.equal(state.live_detection, null);
    assert.equal(state.board, null);
    assert.equal(state.game.width, 0);
    assert.throws(() => act(env.game, "launch"), /inicio y una llegada/);
    env.game.preview(env.frame("wall_430"));
    act(env.game, "projection_open", { width: 1280, height: 800 });
    act(env.game, "launch");
    act(env.game, "pause");
    const before = env.game.snapshot();
    env.now.value += 1.501;
    state = env.game.snapshot();
    assert.equal(state.live_detection, null);
    assert.equal(state.camera_board, null);
    assert.deepEqual(state.board, before.board);
    assert.deepEqual(state.game, before.game);
    assert.throws(() => act(env.game, "reset"), /inicio y una llegada/);
  } finally { env.close(); }
});

test("changing the camera or palette clears old detections and camera changes replace attempts", () => {
  const env = setup();
  try {
    env.game.preview(env.frame("wall_430"), "Camera A");
    act(env.game, "projection_open", { width: 1280, height: 800 });
    act(env.game, "launch");
    env.game.preview(env.frame("wall_540"), "Camera B");
    let state = env.game.snapshot();
    assert.equal(state.camera.label, "Camera B");
    assert.equal(state.phase, "ready");
    assert.deepEqual(state.board, state.camera_board);
    act(env.game, "palette_reset");
    state = env.game.snapshot();
    assert.equal(state.live_detection, null);
    assert.equal(state.board, null);
    env.game.preview(env.frame("wall_430"), "Camera B");
    act(env.game, "camera_changed");
    state = env.game.snapshot();
    assert.equal(state.live_detection, null);
    assert.equal(state.board, null);
    assert.equal(state.camera.label, null);
    assert.equal(state.camera.width, 0);
  } finally { env.close(); }
});

test("a changed camera resolution updates overlay coordinates without changing attempt dimensions", () => {
  const env = setup();
  const smaller = new cv.Mat();
  try {
    const frame = env.frame("wall_430");
    env.game.preview(frame);
    act(env.game, "projection_open", { width: 1280, height: 800 });
    act(env.game, "launch");
    const before = env.game.snapshot();
    cv.resize(frame, smaller, new cv.Size(640, 480), 0, 0, cv.INTER_AREA);
    env.game.preview(smaller);
    const state = env.game.snapshot();
    assert.equal(state.live_detection.width, 640);
    assert.equal(state.game.width, 960);
    assert.equal(state.game.height, 720);
    assert.deepEqual(state.board, before.board);
    assert.deepEqual(state.game.position, before.game.position);
  } finally { smaller.delete(); env.close(); }
});
