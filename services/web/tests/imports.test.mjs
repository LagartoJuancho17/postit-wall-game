/**
 * Gate test: every ui-core name the page scripts call is imported and exported.
 *
 * node --check only parses; a call to a name that was never imported passes
 * it and then throws at load, leaving the whole panel blank. Found while
 * adding the projector theme: projectorTheme was called but not imported.
 */

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import * as core from "../ui/ui-core.mjs";

const read = (name) => readFileSync(new URL(`../ui/${name}`, import.meta.url), "utf8");

function imported(source) {
  const block = source.match(/import\s*\{([^}]*)\}\s*from\s*"\.\/ui-core\.mjs";/);
  assert.ok(block, "imports ui-core");
  return block[1].split(",").map((name) => name.trim()).filter(Boolean);
}

for (const script of ["app.js", "projector.js"]) {
  test(`${script} imports only what ui-core exports, and everything it uses from it`, () => {
    const source = read(script);
    const names = imported(source);
    for (const name of names) assert.ok(name in core, `${name} is not exported by ui-core`);
    const body = source.slice(source.indexOf('from "./ui-core.mjs";'));
    for (const name of Object.keys(core)) {
      const used = new RegExp(`(?<![\\w.$])${name}\\b`).test(body);
      if (used) assert.ok(names.includes(name), `${script} uses ${name} without importing it`);
    }
  });
}
