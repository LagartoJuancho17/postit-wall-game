/** The static package must include the renderer at the requested output location. */
import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, posix } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";

test("static CLI respects an absolute output path and packages both live views", () => {
  const temporary = mkdtempSync(join(tmpdir(), "postit-static-test-"));
  try {
    const output = join(temporary, "site");
    const script = fileURLToPath(new URL("../../../scripts/build-static.mjs", import.meta.url));
    const built = spawnSync(process.execPath, [script, output], { cwd: temporary, encoding: "utf8" });
    assert.equal(built.status, 0, built.stderr);
    const manifest = JSON.parse(readFileSync(join(output, "build.json"), "utf8"));
    for (const name of ["live-view.mjs", "app.js", "projector.js", "browser/controller.mjs"]) {
      assert.ok(manifest.files[name]?.bytes > 0, name);
    }
    for (const name of ["index.html", "projector.html"]) {
      const html = readFileSync(join(output, name), "utf8");
      assert.match(html, /data-backend="browser"/);
      // A static host answers /favicon.ico with 404, a console error on every load.
      assert.match(html, /<link rel="icon" href="data:image\/svg\+xml,/, `${name} declares its icon inline`);
    }
    // Every relative import in the package resolves to a packaged file: a
    // module missing from build-static.mjs would only fail in the browser.
    const modules = Object.keys(manifest.files).filter((name) => /\.(m?js)$/.test(name) && !name.startsWith("vendor/"));
    let imports = 0;
    for (const name of modules) {
      const source = readFileSync(join(output, name), "utf8");
      for (const match of source.matchAll(/(?:from\s+|import\s*\(\s*)["'](\.{1,2}\/[^"']+)["']/g)) {
        const target = posix.normalize(posix.join(posix.dirname(name), match[1]));
        assert.ok(manifest.files[target]?.bytes > 0, `${name} imports ${match[1]}, which is not packaged`);
        imports += 1;
      }
    }
    assert.ok(imports >= 15, `only ${imports} relative imports found`);
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});
