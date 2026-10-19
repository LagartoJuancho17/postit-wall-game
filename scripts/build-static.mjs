#!/usr/bin/env node
/**
 * Static build for Vercel: the panel and projector pages with the in-browser
 * backend (services/browser) instead of the Python server.
 *
 *   node scripts/build-static.mjs [outDir]     (default: dist/)
 *
 * Layout matches what web.py serves, so the pages need no other change:
 * /index.html, /projector.html, /app.js, … at the root, the backend modules
 * under /browser/ (api.mjs imports ./browser/backend.mjs), and opencv.js
 * under /vendor/ with its version in the name so it can be cached forever.
 * The HTML gets data-backend="browser"; that is the only switch.
 * dist/build.json records every file's size and sha256 for the deploy log.
 */

import { createHash } from "node:crypto";
import { copyFileSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
export const OPENCV_VERSION = "4.12.0";
const UI_FILES = ["index.html", "projector.html", "app.css", "app.js", "projector.js", "ui-core.mjs", "api.mjs"];
const BROWSER_FILES = ["backend.mjs", "calibration.mjs", "controller.mjs", "numbers.mjs", "palette.mjs",
  "physics.mjs", "vision.mjs"];

function markBrowser(html, name) {
  const tag = /<html\b([^>]*)>/;
  const match = html.match(tag);
  if (!match || /data-backend=/.test(match[0])) throw new Error(`${name}: expected one plain <html> tag`);
  return html.replace(tag, `<html${match[1]} data-backend="browser">`);
}

function walk(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

export function build(outDir = join(ROOT, "dist")) {
  const opencvPackage = JSON.parse(readFileSync(join(ROOT, "node_modules/@techstark/opencv-js/package.json"), "utf8"));
  if (!opencvPackage.version.startsWith(`${OPENCV_VERSION}-`)) {
    throw new Error(`opencv.js ${opencvPackage.version} instalado; el build espera ${OPENCV_VERSION} ` +
      "(la misma versión que opencv-python en requirements.txt). Corré npm ci.");
  }
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(join(outDir, "browser"), { recursive: true });
  mkdirSync(join(outDir, "vendor"), { recursive: true });

  for (const name of UI_FILES) {
    const source = join(ROOT, "services/web/ui", name);
    if (name.endsWith(".html")) writeFileSync(join(outDir, name), markBrowser(readFileSync(source, "utf8"), name));
    else copyFileSync(source, join(outDir, name));
  }
  for (const name of BROWSER_FILES) copyFileSync(join(ROOT, "services/browser", name), join(outDir, "browser", name));
  copyFileSync(join(ROOT, "node_modules/@techstark/opencv-js/dist/opencv.js"),
    join(outDir, "vendor", `opencv-${OPENCV_VERSION}.js`));

  const files = Object.fromEntries(walk(outDir).sort().map((path) => {
    const data = readFileSync(path);
    return [relative(outDir, path), { bytes: data.length, sha256: createHash("sha256").update(data).digest("hex") }];
  }));
  const manifest = { built_at: new Date().toISOString(), commit: process.env.VERCEL_GIT_COMMIT_SHA ?? null,
    opencv: opencvPackage.version, files };
  writeFileSync(join(outDir, "build.json"), `${JSON.stringify(manifest, null, 1)}\n`);
  return manifest;
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const manifest = build(process.argv[2] ? join(process.cwd(), process.argv[2]) : undefined);
  const total = Object.values(manifest.files).reduce((sum, file) => sum + file.bytes, 0);
  console.log(JSON.stringify({ files: Object.keys(manifest.files).length, bytes: total, opencv: manifest.opencv }));
}
