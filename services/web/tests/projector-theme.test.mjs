/** Gate tests: dark and light projector backgrounds; a scan always sees black. */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  contrastRatio,
  isThemeShortcut,
  PROJECTOR_THEMES,
  projectorBackground,
  projectorColors,
  projectorTheme,
  SCAN_BLACK,
  THEME_KEY,
} from "../ui/ui-core.mjs";

const read = (name) => readFileSync(new URL(`../ui/${name}`, import.meta.url), "utf8");
const projector = read("projector.js");
const operator = read("app.js");
const page = read("index.html");
const css = read("app.css");

test("whatever the theme, a scan or camera search sees pure black", () => {
  for (const theme of ["dark", "light", "anything"]) {
    assert.equal(projectorBackground(theme, true), SCAN_BLACK);
  }
  assert.equal(SCAN_BLACK, "#000000");
  assert.equal(projectorBackground("light", false), "#f3f2ec");
  assert.equal(projectorBackground("dark", false), "#000000");
});

// docs/alignment-and-color.md 2.1 deliberately changed one thing: the projected
// result texts share the ball's neutral color. The old won green (#77ec8f) was
// the bumper's hue, and the camera re-detects projected role hues as post-its.
test("the dark theme keeps the original projector colors, except the neutral result texts", () => {
  assert.deepEqual(PROJECTOR_THEMES.dark, {
    background: "#000000", playfield: "#56dbe0", grid: "#36696a", start: "#ffdf53",
    goal: "#ffa659", bumper: "#77ec8f", ball: "#f6fbfc", won: "#f6fbfc", lost: "#f6fbfc",
    cross: "#ffffff", crossRing: "#f877d5",
  });
  assert.deepEqual(Object.keys(PROJECTOR_THEMES.light), Object.keys(PROJECTOR_THEMES.dark));
  for (const colors of Object.values(PROJECTOR_THEMES)) {
    assert.equal(colors.won, colors.ball, "won is neutral");
    assert.equal(colors.lost, colors.ball, "lost is neutral");
  }
});

test("every stroke stands out from its background in both themes", () => {
  for (const [theme, colors] of Object.entries(PROJECTOR_THEMES)) {
    for (const [name, color] of Object.entries(colors)) {
      if (name === "background") continue;
      const minimum = name === "grid" ? 2.5 : 4; // the grid is a faint guide on purpose
      const ratio = contrastRatio(color, colors.background);
      assert.ok(ratio >= minimum, `${theme}.${name} ${ratio.toFixed(2)} < ${minimum}`);
    }
  }
  assert.equal(Number(contrastRatio("#000000", "#ffffff").toFixed(1)), 21);
  assert.equal(contrastRatio("#777777", "#777777"), 1);
});

test("unknown or missing themes fall back to dark", () => {
  assert.equal(projectorTheme("light"), "light");
  for (const value of ["dark", null, undefined, "LIGHT", "blanco", 1]) assert.equal(projectorTheme(value), "dark");
  assert.equal(projectorColors("nope"), PROJECTOR_THEMES.dark);
});

test("F switches the background unless typing or modified", () => {
  const base = { key: "f", repeat: false, altKey: false, ctrlKey: false, metaKey: false, target: null };
  assert.equal(isThemeShortcut(base), true);
  assert.equal(isThemeShortcut({ ...base, key: "F" }), true);
  for (const patch of [{ key: "g" }, { repeat: true }, { altKey: true }, { ctrlKey: true }, { metaKey: true },
    { target: { closest: () => ({}) } }]) {
    assert.equal(isThemeShortcut({ ...base, ...patch }), false, JSON.stringify(patch));
  }
});

test("the projector draws only theme colors and blacks out for scans", () => {
  assert.doesNotMatch(projector, /["']#[0-9a-fA-F]{3,8}["']/, "hard-coded color in projector.js");
  assert.match(projector, /ctx\.fillStyle = projectorBackground\(theme, blackoutForced, backdrop\);/);
  assert.match(projector, /data\.type === "theme"\) \{\s*theme = projectorTheme\(data\.theme\);\s*draw\(\);/);
  assert.match(projector, /window\.addEventListener\("storage", \(event\) => \{\s*if \(event\.key === BACKDROP_KEY\) \{/);
  assert.match(projector, /if \(event\.key !== THEME_KEY\) return;/);
  // F keeps working in the live projector after calibration was removed.
  assert.match(projector, /if \(isThemeShortcut\(event\)\) \{[\s\S]*?setTheme\(theme === "dark" \? "light" : "dark"\);/);
  assert.doesNotMatch(projector, /calibrat|drawBoard|drawTarget/);
  assert.match(css, /body\.projector-page\[data-blackout="true"\] \{ background: #000; \}/);
});

test("the panel switch saves the choice and tells the projector window", () => {
  for (const id of ["theme-dark", "theme-light"]) assert.match(page, new RegExp(`id="${id}"`));
  assert.match(operator, /localStorage\.setItem\(THEME_KEY, projectorBackdrop\);/);
  assert.match(operator, /channel\.postMessage\(\{ type: "theme", theme: projectorBackdrop \}\);\s*render\(\);/);
  // A projector that just opened gets the current choice even without storage.
  // A projector that (re)announces itself gets the panel's theme, and says
  // whether it is fullscreen (automatic alignment waits for that).
  assert.match(operator, /data\.type === "ready"\) \{\s*popupReady = true;\s*projectorFullscreen = Boolean\(data\.fullscreen\);\s*channel\.postMessage\(\{ type: "theme", theme: projectorBackdrop \}\);/);
  // J keeps priority; F comes right after it.
  assert.match(operator, /openProjector\(\);\s*return;\s*\}\s*if \(isThemeShortcut\(event\)\) \{/);
  assert.equal(THEME_KEY, "postit-wall:projector-theme");
});

test("the operator's own background: stored, sent to the projector, refused when it reads as a post-it", async () => {
  const core = await import("../ui/ui-core.mjs");
  const palette = { roles: {
    start: { hue_low: 21, hue_high: 35, min_saturation: 89, min_value: 82 },
    goal: { hue_low: 164, hue_high: 10, min_saturation: 66, min_value: 102 },   // wraps through red
    bumper: { hue_low: 37, hue_high: 51, min_saturation: 70, min_value: 88 },
    marker: { hue_low: 125, hue_high: 162, min_saturation: 75, min_value: 55 },
  } };
  // services/vision/palette.py hsv_from_hex, value for value.
  assert.deepEqual(core.hsvFromHex("#1a2a4a"), [110, 165, 74]);
  assert.deepEqual(core.hsvFromHex("#ff4fa0"), [166, 176, 255]);
  assert.deepEqual(core.hsvFromHex("#000000"), [0, 0, 0]);
  assert.equal(core.backdropConflict("#ffd400", palette), "start");
  assert.equal(core.backdropConflict("#88cc33", palette), "bumper");
  assert.equal(core.backdropConflict("#ff2010", palette), "goal", "a wrapped hue window");
  for (const safe of ["#000000", "#1a2a4a", "#202020", "#ffffff", "#7a7a80", "nope", null]) {
    assert.equal(core.backdropConflict(safe, palette), null, String(safe));
  }
  assert.equal(core.themeForBackdrop("#1a2a4a"), "dark");
  assert.equal(core.themeForBackdrop("#e8e4d8"), "light");
  assert.equal(core.projectorBackground("dark", false, "#1A2A4A"), "#1a2a4a");
  assert.equal(core.projectorBackground("dark", true, "#1a2a4a"), "#000000", "scans stay pure black");
  assert.equal(core.projectorBackground("light", false, "#12345"), core.projectorColors("light").background);
  const store = new Map([[core.BACKDROP_KEY, "#ABCDEF"]]);
  assert.equal(core.readBackdrop({ getItem: (key) => store.get(key) ?? null }), "#abcdef");
  assert.equal(core.readBackdrop({ getItem: () => "red" }), null);
  assert.equal(core.readBackdrop({ getItem: () => { throw new Error("blocked"); } }), null);
  // The panel control, its refusal and the channel message.
  assert.match(page, /id="backdrop-color" type="color"/);
  assert.match(page, /id="backdrop-reset"/);
  assert.match(operator, /const role = backdropConflict\(hex, state\?\.palette\);/);
  assert.match(operator, /Ese fondo la cámara lo confunde con/);
  assert.match(operator, /channel\.postMessage\(\{ type: "backdrop", hex: customBackdrop \}\);/);
  assert.match(projector, /data\.type === "backdrop"\) \{/);
  // F (either window) goes back to a theme background.
  assert.match(projector, /if \(!localScene && !blackoutForced && backdrop !== null\) dropBackdrop\(\);/);
  assert.match(operator, /customBackdrop !== null\) setBackdrop\(null\);/);
});
