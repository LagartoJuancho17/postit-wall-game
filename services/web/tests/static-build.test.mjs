/** The static package must include the renderer at the requested output location. */
import assert from "node:assert/strict";
import test from "node:test";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
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
      assert.match(readFileSync(join(output, name), "utf8"), /data-backend="browser"/);
    }
  } finally {
    rmSync(temporary, { recursive: true, force: true });
  }
});
