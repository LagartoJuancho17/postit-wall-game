/**
 * Port of services/web/backend/handsfree.py: the countdown and the cover
 * gesture (contract 3.1). Same operations in the same order, so both
 * backends fire on the same frames; see that file for why.
 */

export const COUNTDOWN_S = 2.4;
export const COUNTDOWN_BEATS = 3;
export const COVER_MIN_S = 1.0;
export const COVER_MAX_S = 4.0;
export const COVER_DARK_FRAMES = 2;
export const COVER_NEAR_SHARE = 0.05;
export const COVER_SHRINK = 0.85;

export function coverBegin() {
  return { seen: null, area: null, hidden_since: null, armed: false, dark: 0, spoiled: false };
}

/** The one start note's [center, area] (camera px); null without exactly one. */
export function startNote(notes) {
  const starts = notes.start ?? [];
  if (starts.length !== 1) return null;
  const points = starts[0];
  let sx = 0.0;
  let sy = 0.0;
  let twice = 0.0;
  points.forEach((point, k) => {
    const next = points[(k + 1) % points.length];
    sx += point.x;
    sy += point.y;
    twice += point.x * next.y - next.x * point.y;
  });
  return [{ x: sx / points.length, y: sy / points.length }, Math.abs(twice) / 2];
}

/** One live frame: [cover, event], event "armed", "fire" or null. */
export function coverUpdate(cover, note, goalVisible, now, [width, height]) {
  const seen = cover.seen;
  let hiddenSince = cover.hidden_since;
  let start = null;
  let area = null;
  if (note !== null) {
    [start, area] = note;
    if (cover.area !== null && area < COVER_SHRINK * cover.area) start = null;   // the projector's ghost on the hand
  }
  if (start !== null) {
    let fire = false;
    if (seen !== null && hiddenSince !== null && !cover.spoiled) {
      const hidden = now - hiddenSince;
      const dx = start.x - seen.x;
      const dy = start.y - seen.y;
      const near = Math.sqrt(dx * dx + dy * dy) <= COVER_NEAR_SHARE * Math.min(width, height);
      fire = near && COVER_MIN_S <= hidden && hidden <= COVER_MAX_S;
    }
    const usual = cover.area === null || fire || hiddenSince !== null ? area : 0.7 * cover.area + 0.3 * area;
    return [{ ...coverBegin(), seen: start, area: usual }, fire ? "fire" : null];
  }
  if (seen === null) return [cover, null];
  if (hiddenSince === null) {
    cover = { ...cover, hidden_since: now, dark: 0, spoiled: false };
    hiddenSince = now;
  }
  const dark = goalVisible ? 0 : cover.dark + 1;
  const spoiled = cover.spoiled || dark >= COVER_DARK_FRAMES;
  cover = { ...cover, dark, spoiled, armed: cover.armed && !spoiled };
  const hidden = now - hiddenSince;
  if (hidden > COVER_MAX_S) return [coverBegin(), null];
  if (!cover.armed && !spoiled && hidden >= COVER_MIN_S) return [{ ...cover, armed: true }, "armed"];
  return [cover, null];
}
