/**
 * Port of contracts/palette.py and services/vision/palette.py.
 *
 * Which camera colors count as each kind of post-it, and how a color the
 * operator picked becomes a hue band. Hues use OpenCV's 0..179 scale. Every
 * message is the Python one, word for word: the panel shows it as is.
 */

import { pyMod, pyRound } from "./numbers.mjs";

export const ROLES = ["start", "goal", "bumper", "marker"];
export const ROLE_LABELS = { start: "inicio", goal: "llegada", bumper: "rebotes", marker: "esquinas" };

export const HUE_TOLERANCE = 10;
export const MIN_HUE_DISTANCE = 8;
export const MIN_SAMPLE_SATURATION = 45;
export const MIN_SAMPLE_VALUE = 40;

const HEX = /^#[0-9a-f]{6}$/;
const HEX_ERROR = "El color debe tener la forma #rrggbb.";

/** A frozen, validated rule: `{ hex, hue_low, hue_high, min_saturation, min_value }`. */
export function colorRule(hex, hueLow, hueHigh, minSaturation, minValue) {
  if (typeof hex !== "string" || !HEX.test(hex)) throw new Error(HEX_ERROR);
  const rule = { hex, hue_low: hueLow, hue_high: hueHigh, min_saturation: minSaturation, min_value: minValue };
  for (const [name, top] of [["hue_low", 179], ["hue_high", 179], ["min_saturation", 255], ["min_value", 255]]) {
    const value = rule[name];
    if (typeof value !== "number" || !Number.isInteger(value) || value < 0 || value > top) {
      throw new Error(`${name} debe ser un entero entre 0 y ${top}.`);
    }
  }
  return Object.freeze(rule);
}

/** Every hue a rule accepts; a rule with low > high wraps through red. */
export function ruleHues(rule) {
  const hues = [];
  if (rule.hue_low <= rule.hue_high) {
    for (let hue = rule.hue_low; hue <= rule.hue_high; hue += 1) hues.push(hue);
  } else {
    for (let hue = rule.hue_low; hue < 180; hue += 1) hues.push(hue);
    for (let hue = 0; hue <= rule.hue_high; hue += 1) hues.push(hue);
  }
  return hues;
}

/** A frozen palette `{ start, goal, bumper, marker }`; roles never share a hue. */
export function makePalette(rules) {
  const owner = new Map();
  for (const role of ROLES) {
    for (const hue of ruleHues(rules[role])) {
      if (owner.has(hue)) {
        throw new Error(`Los colores de ${ROLE_LABELS[owner.get(hue)]} y ${ROLE_LABELS[role]} se superponen.`);
      }
      owner.set(hue, role);
    }
  }
  return Object.freeze(Object.fromEntries(ROLES.map((role) => [role, rules[role]])));
}

export function paletteJson(palette) {
  return Object.fromEntries(ROLES.map((role) => [role, { ...palette[role] }]));
}

export const DEFAULT_PALETTE = makePalette({
  start: colorRule("#f7d84b", 20, 39, 75, 55),
  goal: colorRule("#ff9b49", 5, 19, 75, 55),
  bumper: colorRule("#6cc678", 40, 90, 75, 55),
  marker: colorRule("#d75ab9", 125, 179, 75, 55),
});

export function normalizeHex(value) {
  if (typeof value !== "string") throw new Error(HEX_ERROR);
  const text = value.trim().toLowerCase();
  if (!HEX.test(text)) throw new Error(HEX_ERROR);
  return text;
}

/** Python's colorsys.rgb_to_hsv. */
function rgbToHsv(r, g, b) {
  const maxc = Math.max(r, g, b);
  const minc = Math.min(r, g, b);
  const v = maxc;
  if (minc === maxc) return [0, 0, v];
  const rangec = maxc - minc;
  const s = rangec / maxc;
  const rc = (maxc - r) / rangec;
  const gc = (maxc - g) / rangec;
  const bc = (maxc - b) / rangec;
  let h;
  if (r === maxc) h = bc - gc;
  else if (g === maxc) h = 2.0 + rc - bc;
  else h = 4.0 + gc - rc;
  return [pyMod(h / 6.0, 1.0), s, v];
}

/** Hex color in OpenCV 8-bit HSV: hue 0..179, saturation and value 0..255. */
export function hsvFromHex(value) {
  const text = normalizeHex(value);
  const [red, green, blue] = [1, 3, 5].map((i) => parseInt(text.slice(i, i + 2), 16) / 255);
  const [hue, saturation, brightness] = rgbToHsv(red, green, blue);
  return [pyMod(pyRound(hue * 180), 180), pyRound(saturation * 255), pyRound(brightness * 255)];
}

export function hueDistance(a, b) {
  const difference = pyMod(Math.abs(a - b), 180);
  return Math.min(difference, 180 - difference);
}

export function ruleFromHex(value) {
  const text = normalizeHex(value);
  const [hue, saturation, brightness] = hsvFromHex(text);
  if (saturation < MIN_SAMPLE_SATURATION) {
    throw new Error("Ese color es casi gris: la pared se confundiría con el post-it. Elegí un tono más vivo.");
  }
  if (brightness < MIN_SAMPLE_VALUE) {
    throw new Error("Ese color es demasiado oscuro: se confundiría con las sombras.");
  }
  return colorRule(
    text,
    pyMod(hue - HUE_TOLERANCE, 180),
    pyMod(hue + HUE_TOLERANCE, 180),
    Math.min(120, Math.max(45, pyRound(saturation * 0.5))),
    Math.min(120, Math.max(40, pyRound(brightness * 0.4))),
  );
}

function arc(rule, hues) {
  if (hues.size === 0) throw new Error("No queda ningún tono libre para ese color.");
  const starts = [...hues].filter((hue) => !hues.has(pyMod(hue - 1, 180)));
  if (starts.length !== 1) throw new Error("Los colores elegidos se cruzan entre sí. Probá con otros tonos.");
  const low = starts[0];
  let high = low;
  while (hues.has(pyMod(high + 1, 180))) high = pyMod(high + 1, 180);
  return colorRule(rule.hex, low, high, rule.min_saturation, rule.min_value);
}

/**
 * Palette from picked colors; null (or a missing role) keeps the default.
 * `changed` names the role the operator just edited, so a refusal speaks
 * about that role instead of whichever role comes first.
 */
export function buildPalette(choices, changed = null) {
  const unknown = Object.keys(choices).filter((role) => !ROLES.includes(role)).sort();
  if (unknown.length) throw new Error(`Rol desconocido: ${unknown.join(", ")}.`);
  const isCustom = (role) => choices[role] !== null && choices[role] !== undefined;
  const custom = new Set(ROLES.filter(isCustom));
  const base = Object.fromEntries(ROLES.map((role) => [
    role, custom.has(role) ? ruleFromHex(choices[role]) : DEFAULT_PALETTE[role],
  ]));
  const center = Object.fromEntries(ROLES.map((role) => [role, hsvFromHex(base[role].hex)[0]]));
  const owned = Object.fromEntries(ROLES.map((role) => [role, new Set(ruleHues(base[role]))]));
  ROLES.forEach((first, index) => {
    for (const second of ROLES.slice(index + 1)) {
      if (!custom.has(first) && !custom.has(second)) continue;
      if (hueDistance(center[first], center[second]) < MIN_HUE_DISTANCE) {
        let picked;
        if (changed === first || changed === second) picked = changed;
        else picked = custom.has(first) ? first : second;
        const other = picked === first ? second : first;
        throw new Error(
          `El color de ${ROLE_LABELS[picked]} se parece demasiado al de ${ROLE_LABELS[other]}. ` +
          `Elegí otro o cambiá primero el de ${ROLE_LABELS[other]}.`);
      }
      for (const [role, rival] of [[first, second], [second, first]]) {
        owned[role] = new Set([...owned[role]].filter(
          (hue) => hueDistance(hue, center[role]) < hueDistance(hue, center[rival])));
      }
    }
  });
  return makePalette(Object.fromEntries(ROLES.map((role) => [role, arc(base[role], owned[role])])));
}
