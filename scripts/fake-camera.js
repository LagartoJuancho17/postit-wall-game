/*
 * A camera for testing the panel without hardware. Loaded only by
 * `node scripts/serve-static.mjs --fake-camera`, never by the build.
 *
 * It replaces getUserMedia with a canvas stream of the synthetic wall the
 * Python evals use (services/web/backend/eval.py `_image`): gray wall, four
 * fuchsia corners, yellow start, orange goal, one green bumper. From the
 * console, `postitFakeCamera.wall({ greenX: 540 })` moves the bumper and
 * `postitFakeCamera.wall({ hues: "recolored" })` repaints it with the colors
 * the default palette refuses.
 */
(() => {
  const DEFAULT = { start: "#e6e617", goal: "#e66a17", bumper: "#17e639", marker: "#e617b6" };
  // HSV(105/176/28/140, 230, 230) like eval.RECOLORED
  const RECOLORED = { start: "#177ee6", goal: "#e61732", bumper: "#e6d817", marker: "#a117e6" };
  const canvas = document.createElement("canvas");
  canvas.width = 960;
  canvas.height = 720;
  const ctx = canvas.getContext("2d");
  let options = { greenX: 430, hues: "default" };

  function rect(color, left, top, right, bottom) {
    ctx.fillStyle = color;
    ctx.fillRect(left, top, right - left + 1, bottom - top + 1);
  }

  function draw() {
    const hue = options.hues === "recolored" ? RECOLORED : DEFAULT;
    rect("rgb(220,220,220)", 0, 0, 959, 719);
    for (const [x, y] of [[90, 90], [870, 90], [870, 630], [90, 630]]) rect(hue.marker, x - 22, y - 22, x + 22, y + 22);
    rect(hue.start, 150, 320, 210, 380);
    rect(hue.goal, 750, 320, 810, 380);
    rect(hue.bumper, options.greenX - 35, 210, options.greenX + 35, 260);
  }

  draw();
  setInterval(draw, 100); // captureStream only emits frames when the canvas paints
  const label = "Cámara simulada (pared de prueba)";
  const device = { kind: "videoinput", deviceId: "fake-wall", groupId: "fake", label, toJSON() { return this; } };
  // A fresh stream per request: the panel stops the tracks it no longer
  // uses, and a stopped track never delivers frames again (it showed 2 × 2).
  navigator.mediaDevices.getUserMedia = async () => {
    const stream = canvas.captureStream(15);
    const track = stream.getVideoTracks()[0];
    Object.defineProperty(track, "label", { value: label });
    const settings = track.getSettings.bind(track);
    track.getSettings = () => ({ ...settings(), deviceId: device.deviceId });
    return stream;
  };
  navigator.mediaDevices.enumerateDevices = async () => [device];
  window.postitFakeCamera = {
    wall(next) {
      options = { ...options, ...next };
      draw();
    },
  };
})();
