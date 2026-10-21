#!/usr/bin/env node
/**
 * Serve the static build (dist/) the way Vercel does with cleanUrls:
 * /projector → projector.html, JS modules as text/javascript.
 *
 *   node scripts/serve-static.mjs [port] [--dir dist] [--fake-camera]
 *
 * `--fake-camera` adds scripts/fake-camera.js to the panel, so the whole
 * flow (scan, calibrate, play) can be driven without a webcam. Local check
 * of the deploy before publishing; nothing here ships.
 */

import { existsSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { dirname, extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const flag = (name) => args.includes(name);
const option = (name) => (args.includes(name) ? args[args.indexOf(name) + 1] : null);
const port = Number(args.find((arg) => /^\d+$/.test(arg)) || 4870);
const dir = resolve(option("--dir") || join(ROOT, "dist"));
const fakeCamera = flag("--fake-camera");
const TYPES = { ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json" };

createServer((request, response) => {
  const path = normalize(decodeURIComponent(new URL(request.url, "http://x").pathname));
  if (fakeCamera && path === "/__fake-camera.js") {
    response.writeHead(200, { "Content-Type": TYPES[".js"], "Cache-Control": "no-store" });
    response.end(readFileSync(join(ROOT, "scripts/fake-camera.js")));
    return;
  }
  let file = join(dir, path === "/" ? "index.html" : path);
  if (!extname(file) && existsSync(`${file}.html`)) file = `${file}.html`;
  if (!file.startsWith(dir) || !existsSync(file) || statSync(file).isDirectory()) {
    response.writeHead(404, { "Content-Type": "text/plain" }).end("404");
    return;
  }
  let body = readFileSync(file);
  if (fakeCamera && file.endsWith("index.html")) {
    body = body.toString().replace('<script type="module"', '<script src="/__fake-camera.js"></script>\n    <script type="module"');
  }
  response.writeHead(200, { "Content-Type": TYPES[extname(file)] || "application/octet-stream", "Cache-Control": "no-store" });
  response.end(body);
}).listen(port, "127.0.0.1", () => {
  console.log(`dist en http://localhost:${port}/${fakeCamera ? " (cámara simulada)" : ""}`);
});
