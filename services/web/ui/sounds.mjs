/**
 * Sounds of an attempt (Tobias, 2026-10-26: "sonidos al lanzar, cuando
 * rebota, y cuando llega y gana"), synthesized with Web Audio: no files.
 *
 * The projector window sees every state (~30 per second), so it finds the
 * events (`soundEvents`) and sends them over the BroadcastChannel; the panel
 * plays them (`playSound`), because Chrome only lets a page make sound after
 * the user interacted with it, and the panel is where Space is pressed.
 */

export const SOUND_NAMES = ["launch", "bounce", "win", "lost", "tick"];
export const MAX_BOUNCES_PER_STATE = 3;   // several bounces between two states: at most this many clicks
export const SOUND_KEY = "postit-wall:sound";

/** The countdown's beat (3, 2, 1; 0 when none) from a snapshot of `game` (contract 3.1). */
export function countdownBeatOf(game) {
  const left = Number(game?.countdown_s);
  const total = Number(game?.countdown_total_s);
  if (!(left > 0) || !(total > 0)) return 0;
  return Math.min(3, Math.max(1, Math.ceil(left / (total / 3) - 1e-9)));   // as effects.mjs countdownBeat
}

/** What happened between two snapshots of `game`, as sound names in order. */
export function soundEvents(previous, game) {
  // A new beat of the countdown ticks; the drop that follows is "launch".
  const beat = countdownBeatOf(game);
  if (beat > 0) return previous && countdownBeatOf(previous) !== beat ? ["tick"] : [];
  // A window that just opened has no "before": it plays nothing for what it missed.
  if (!previous || !game || !["running", "won", "lost"].includes(game.status)) return [];
  const same = previous && previous.attempts === game.attempts;
  const events = [];
  if (!same || previous.status === "ready" || previous.status === "none") {
    if (game.status === "running" || (game.elapsed_s ?? 0) < 0.25) events.push("launch");
  }
  const before = same ? previous.bounces ?? 0 : 0;
  const bounces = Math.max(0, (game.bounces ?? 0) - before);
  for (let k = 0; k < Math.min(bounces, MAX_BOUNCES_PER_STATE); k += 1) events.push("bounce");
  const ended = game.status === "won" || game.status === "lost";
  if (ended && !(same && previous.status === game.status)) events.push(game.status === "won" ? "win" : "lost");
  return events;
}

/** Sound on or off, remembered in the browser (on by default). */
export function readSoundOn(storage) {
  try {
    return storage?.getItem(SOUND_KEY) !== "off";
  } catch {
    return true;
  }
}

function tone(audio, { type = "sine", from, to = from, start = 0, duration, gain = 0.2, attack = 0.005 }) {
  const t0 = audio.currentTime + start;
  const osc = audio.createOscillator();
  const amp = audio.createGain();
  osc.type = type;
  osc.frequency.setValueAtTime(from, t0);
  if (to !== from) osc.frequency.exponentialRampToValueAtTime(to, t0 + duration);
  amp.gain.setValueAtTime(0.0001, t0);
  amp.gain.exponentialRampToValueAtTime(gain, t0 + attack);
  amp.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
  osc.connect(amp).connect(audio.destination);
  osc.start(t0);
  osc.stop(t0 + duration + 0.02);
}

function noiseBurst(audio, { start = 0, duration, from, to = from, gain = 0.15, q = 1.2 }) {
  const t0 = audio.currentTime + start;
  const length = Math.max(1, Math.round(audio.sampleRate * duration));
  const buffer = audio.createBuffer(1, length, audio.sampleRate);
  const data = buffer.getChannelData(0);
  let seed = 12345;
  for (let i = 0; i < length; i += 1) {
    seed = (seed * 1103515245 + 12345) >>> 0;   // repeatable noise
    data[i] = seed / 2 ** 31 - 1;
  }
  const source = audio.createBufferSource();
  source.buffer = buffer;
  const filter = audio.createBiquadFilter();
  filter.type = "bandpass";
  filter.Q.value = q;
  filter.frequency.setValueAtTime(from, t0);
  if (to !== from) filter.frequency.exponentialRampToValueAtTime(to, t0 + duration);
  const amp = audio.createGain();
  amp.gain.setValueAtTime(0.0001, t0);
  amp.gain.exponentialRampToValueAtTime(gain, t0 + 0.02);
  amp.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
  source.connect(filter).connect(amp).connect(audio.destination);
  source.start(t0);
  source.stop(t0 + duration + 0.02);
}

/**
 * Play one sound on an AudioContext (or anything with its API). Unknown
 * names are ignored. `variant` shifts a bounce's pitch so a run of bounces
 * does not sound like one repeated click.
 */
export function playSound(audio, name, variant = 0, intensity = 1) {
  if (!audio || !SOUND_NAMES.includes(name)) return false;
  const power = Math.min(1, Math.max(0.15, Number.isFinite(intensity) ? intensity : 1));
  if (name === "launch") {
    // A whoosh going up, and a short rising "pop".
    noiseBurst(audio, { duration: 0.35, from: 500, to: 3200, gain: 0.18 });
    tone(audio, { type: "triangle", from: 260, to: 880, duration: 0.22, gain: 0.16 });
  } else if (name === "bounce") {
    // Harder hits sound louder and a little higher (contract 3.1).
    const pitch = 440 * 2 ** ((variant % 5) / 12) * (0.85 + 0.4 * power);
    tone(audio, { type: "triangle", from: pitch, to: pitch / 2, duration: 0.1 + 0.06 * power, gain: 0.08 + 0.2 * power });
    noiseBurst(audio, { duration: 0.05, from: 2400, gain: 0.03 + 0.07 * power, q: 3 });
  } else if (name === "tick") {
    // 3, 2, 1: the same short beep, a step higher each beat (variant = beat).
    const pitch = 660 * 2 ** ((3 - Math.min(3, Math.max(1, variant))) * 2 / 12);
    tone(audio, { type: "square", from: pitch, duration: 0.12, gain: 0.06 });
    tone(audio, { type: "sine", from: pitch, duration: 0.16, gain: 0.14 });
  } else if (name === "win") {
    // A rising major arpeggio, a held chord and a sparkle.
    [523.25, 659.25, 783.99, 1046.5].forEach((f, i) => {
      tone(audio, { type: "square", from: f, duration: 0.16, start: i * 0.1, gain: 0.07 });
      tone(audio, { type: "sine", from: f, duration: 0.2, start: i * 0.1, gain: 0.12 });
    });
    [523.25, 659.25, 783.99, 1046.5].forEach((f) => tone(audio, { type: "triangle", from: f, duration: 0.9, start: 0.42, gain: 0.08, attack: 0.02 }));
    noiseBurst(audio, { duration: 0.8, start: 0.42, from: 6000, to: 9000, gain: 0.06, q: 0.7 });
  } else {
    // Lost: a soft falling tone.
    tone(audio, { type: "sawtooth", from: 392, to: 110, duration: 0.55, gain: 0.08 });
  }
  return true;
}
