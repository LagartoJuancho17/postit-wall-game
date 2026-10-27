/** Gate tests: the attempt's sounds (sounds.mjs) and how the two windows share them. */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { MAX_BOUNCES_PER_STATE, playSound, readSoundOn, SOUND_KEY, SOUND_NAMES, soundEvents } from "../ui/sounds.mjs";

const read = (name) => readFileSync(new URL(`../ui/${name}`, import.meta.url), "utf8");
const game = (status, extra = {}) => ({ status, attempts: 1, bounces: 0, elapsed_s: 0.1, ...extra });

test("soundEvents: the drop, each bounce, the win and the loss, once each", () => {
  assert.deepEqual(soundEvents(game("ready", { attempts: 0 }), game("running")), ["launch"]);
  assert.deepEqual(soundEvents(game("running"), game("running", { elapsed_s: 0.2 })), [], "nothing new");
  assert.deepEqual(soundEvents(game("running"), game("running", { bounces: 1 })), ["bounce"]);
  assert.deepEqual(soundEvents(game("running", { bounces: 1 }), game("running", { bounces: 9 })),
    Array(MAX_BOUNCES_PER_STATE).fill("bounce"), "a burst of bounces is capped");
  assert.deepEqual(soundEvents(game("running", { bounces: 2 }), game("won", { bounces: 3 })), ["bounce", "win"]);
  assert.deepEqual(soundEvents(game("won"), game("won")), [], "the win sounds once");
  assert.deepEqual(soundEvents(game("running"), game("lost")), ["lost"]);
  // A new attempt after the last one ended.
  assert.deepEqual(soundEvents(game("won"), game("running", { attempts: 2 })), ["launch"]);
  // A window that just opened plays nothing it missed; no game, no sound.
  assert.deepEqual(soundEvents(null, game("running", { bounces: 4 })), []);
  assert.deepEqual(soundEvents(game("running"), null), []);
  assert.deepEqual(soundEvents(game("running"), game("ready", { attempts: 2 })), []);
});

/** A Web Audio stand-in that records what each sound builds. */
function fakeAudio() {
  const made = [];
  const param = () => ({ value: 0, setValueAtTime() {}, exponentialRampToValueAtTime() {} });
  const node = (kind) => {
    const n = { kind, frequency: param(), gain: param(), Q: param(), connect: (to) => to ?? n, start() {}, stop() {} };
    made.push(n);
    return n;
  };
  return {
    made, currentTime: 0, sampleRate: 8000, destination: { kind: "destination" },
    createOscillator: () => node("osc"), createGain: () => node("gain"),
    createBiquadFilter: () => node("filter"), createBufferSource: () => node("source"),
    createBuffer: (channels, length) => ({ getChannelData: () => new Float32Array(length) }),
  };
}

test("every sound is synthesized (no files), and unknown names do nothing", () => {
  for (const name of SOUND_NAMES) {
    const audio = fakeAudio();
    assert.equal(playSound(audio, name), true, name);
    assert.ok(audio.made.some((n) => n.kind === "osc"), `${name} has a tone`);
  }
  const audio = fakeAudio();
  assert.equal(playSound(audio, "explode"), false);
  assert.equal(audio.made.length, 0);
  assert.equal(playSound(null, "win"), false);
});

test("sound is on by default, remembered off, and survives blocked storage", () => {
  assert.equal(readSoundOn({ getItem: () => null }), true);
  assert.equal(readSoundOn({ getItem: (key) => (key === SOUND_KEY ? "off" : null) }), false);
  assert.equal(readSoundOn({ getItem: () => { throw new Error("blocked"); } }), true);
});

test("the projector finds the events and the panel plays them after a user gesture", () => {
  const projector = read("projector.js");
  const panel = read("app.js");
  const page = read("index.html");
  assert.match(projector, /for \(const name of soundEvents\(lastGame, next\?\.game\)\) channel\.postMessage\(\{ type: "sound", name \}\);/);
  assert.match(panel, /data\.type === "sound"\) \{\s*if \(soundOn && audio\) playSound\(audio, data\.name/);
  assert.match(panel, /window\.addEventListener\("keydown", unlockAudio, \{ capture: true \}\);/);
  assert.match(panel, /window\.addEventListener\("pointerdown", unlockAudio, \{ capture: true \}\);/);
  assert.match(page, /id="sound-toggle"/);
});
