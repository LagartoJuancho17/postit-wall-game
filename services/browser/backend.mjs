/**
 * The Python server's HTTP surface, answered inside the browser.
 *
 * `handle(url, init)` takes what the panel would pass to fetch() and returns
 * `{ status, body }` with the status codes and JSON the Python handler
 * (services/web/backend/server.py `create_server`) sends. The panel owns the
 * one backend; the projector window reaches it through `window.opener`.
 *
 * Trace: every controller event goes to the console and to a ring buffer in
 * localStorage (`postit-wall:events`, read it with `postitEvents()`), and
 * every 25 live previews log their detection time (`preview_timing`).
 */

import { alignKey, GameController, HTTP, RequestError } from "./controller.mjs";

export const OPENCV_URL = "/vendor/opencv-4.12.0.js";
const MAX_BODY_BYTES = 8 * 1024 * 1024;
const MAX_FRAME_PIXELS = 12_000_000;
const EVENTS_KEY = "postit-wall:events";
const MAX_EVENTS = 400;
const TIMING_EVERY = 25;

/** Load opencv.js once; resolves with the ready `cv` module. */
export function loadOpenCv(doc = document, src = OPENCV_URL) {
  const win = doc.defaultView;
  win.postitOpenCv ??= new Promise((resolve, reject) => {
    const ready = (cv) => {
      // The Emscripten module is a thenable that resolves to itself:
      // resolving a promise with it would spin forever.
      delete cv.then;
      resolve({ cv });
    };
    const script = doc.createElement("script");
    script.src = src;
    script.async = true;
    script.onerror = () => reject(new Error("No se pudo descargar OpenCV. Revisá la conexión y recargá la página."));
    script.onload = () => {
      const cv = win.cv;
      if (cv?.Mat) ready(cv);
      else cv.onRuntimeInitialized = () => ready(cv);
    };
    doc.head.append(script);
  });
  return win.postitOpenCv.then(({ cv }) => cv);
}

function eventLog(storage, consoleRef = console) {
  return (entry) => {
    consoleRef.info?.("[postit]", entry.event, entry);
    try {
      const events = JSON.parse(storage?.getItem(EVENTS_KEY) || "[]");
      events.push(entry);
      storage?.setItem(EVENTS_KEY, JSON.stringify(events.slice(-MAX_EVENTS)));
    } catch {
      // The console still has it.
    }
  };
}

function percentile(sorted, fraction) {
  return sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * fraction))];
}

const FRAME_MARKERS = new Set([0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7, 0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf]);

/**
 * server.py `_jpeg_dimensions`: the SOF size read before anything is
 * decoded, so a non-JPEG body (a PNG) or an oversized frame is refused
 * without allocating it. Same messages and statuses.
 */
export function jpegDimensions(data) {
  if (data.length < 4 || data[0] !== 0xff || data[1] !== 0xd8) throw new RequestError("La captura debe ser un JPEG válido.");
  let offset = 2;
  while (offset + 3 < data.length) {
    if (data[offset] !== 0xff) throw new RequestError("La captura JPEG está dañada.");
    while (offset < data.length && data[offset] === 0xff) offset += 1;
    if (offset >= data.length) break;
    const marker = data[offset];
    offset += 1;
    if (marker === 0xd9 || marker === 0xda) break;
    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    if (offset + 2 > data.length) break;
    const length = (data[offset] << 8) | data[offset + 1];
    if (length < 2 || offset + length > data.length) throw new RequestError("La captura JPEG está dañada.");
    if (FRAME_MARKERS.has(marker)) {
      if (length < 7) throw new RequestError("La captura JPEG está dañada.");
      const height = (data[offset + 3] << 8) | data[offset + 4];
      const width = (data[offset + 5] << 8) | data[offset + 6];
      if (width <= 0 || height <= 0) throw new RequestError("La captura JPEG no tiene dimensiones válidas.");
      if (width * height > MAX_FRAME_PIXELS) {
        throw new RequestError("La captura supera el límite de 12 megapíxeles.", HTTP.REQUEST_ENTITY_TOO_LARGE);
      }
      return [width, height];
    }
    offset += length;
  }
  throw new RequestError("La captura JPEG no contiene una imagen válida.");
}

/** A decoded camera JPEG as a BGR cv.Mat, with the server's size limits. */
export async function decodeJpeg(cv, blob, doc = globalThis.document) {
  if (!blob || typeof blob.arrayBuffer !== "function") throw new RequestError("La captura debe ser un JPEG válido.");
  if (blob.size > MAX_BODY_BYTES) {
    throw new RequestError("La captura o acción supera 8 MB.", HTTP.REQUEST_ENTITY_TOO_LARGE);
  }
  jpegDimensions(new Uint8Array(await blob.arrayBuffer()));
  let bitmap;
  try {
    bitmap = await createImageBitmap(blob, { colorSpaceConversion: "none", premultiplyAlpha: "none" });
  } catch {
    throw new RequestError("No se pudo decodificar la imagen de la cámara.");
  }
  try {
    const { width, height } = bitmap;
    if (width * height > MAX_FRAME_PIXELS) {
      throw new RequestError("La captura supera el límite de 12 megapíxeles.", HTTP.REQUEST_ENTITY_TOO_LARGE);
    }
    const canvas = typeof OffscreenCanvas === "function"
      ? new OffscreenCanvas(width, height)
      : Object.assign(doc.createElement("canvas"), { width, height });
    const context = canvas.getContext("2d", { willReadFrequently: true, alpha: false });
    context.drawImage(bitmap, 0, 0);
    const rgba = cv.matFromImageData(context.getImageData(0, 0, width, height));
    const bgr = new cv.Mat();
    cv.cvtColor(rgba, bgr, cv.COLOR_RGBA2BGR);
    rgba.delete();
    return bgr;
  } finally {
    bitmap.close?.();
  }
}

/** Python's urllib.parse.unquote: a malformed escape stays as written instead of throwing. */
export function unquote(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value.replace(/(?:%[0-9a-fA-F]{2})+/g, (run) => {
      try {
        return decodeURIComponent(run);
      } catch {
        return run;
      }
    });
  }
}

function headerBag(headers) {
  if (!headers) return new Map();
  const entries = typeof headers.entries === "function" && !Array.isArray(headers)
    ? [...headers.entries()] : Array.isArray(headers) ? headers : Object.entries(headers);
  return new Map(entries.map(([key, value]) => [String(key).toLowerCase(), String(value)]));
}

export class BrowserBackend {
  /**
   * `cv`: a promise of the opencv.js module. `decode(cv, blob)`: JPEG → BGR
   * Mat. Both are injectable so the routing is tested in Node.
   */
  constructor({ controller, cv, decode = decodeJpeg, log = null, now = () => performance.now() }) {
    this.controller = controller;
    this.decode = decode;
    this.log = log;
    this.now = now;
    this.timings = [];
    this.cvReady = Promise.resolve(cv).then((module) => {
      controller.cv = module;
      return module;
    });
    this.cvReady.catch(() => {});
  }

  error(error) {
    if (!(error instanceof RequestError)) throw error;
    return { status: error.status, body: { error: error.message, state: this.controller.snapshot() } };
  }

  /** Same routes, checks and statuses as the Python handler. */
  handle(url, init = {}) {
    try {
      const path = new URL(url, "http://postit.local").pathname;
      const method = (init.method || "GET").toUpperCase();
      if (method === "GET") {
        if (path === "/api/state") return { status: 200, body: this.controller.snapshot() };
        if (path === "/api/tick") return { status: 200, body: this.controller.tick() };
        if (path === "/api/camera") return { status: 200, body: this.controller.usbCamera() };
        throw new RequestError("Ruta desconocida.", HTTP.NOT_FOUND);
      }
      if (method !== "POST") throw new RequestError("Ruta desconocida.", HTTP.NOT_FOUND);
      const headers = headerBag(init.headers);
      if (headers.get("x-postit-client") !== "1") {
        throw new RequestError("Falta la cabecera X-Postit-Client: 1.", HTTP.FORBIDDEN);
      }
      if (!["/api/action", "/api/frame", "/api/preview", "/api/camera", "/api/align_frame",
        "/api/check_frame"].includes(path)) {
        throw new RequestError("Ruta desconocida.", HTTP.NOT_FOUND);
      }
      const contentType = (headers.get("content-type") || "").split(";", 1)[0].trim();
      const expected = path === "/api/action" || path === "/api/camera" ? "application/json" : "image/jpeg";
      if (contentType !== expected) {
        throw new RequestError(`Content-Type esperado: ${expected}.`, HTTP.UNSUPPORTED_MEDIA_TYPE);
      }
      if (path === "/api/frame" || path === "/api/preview" || path === "/api/align_frame" ||
          path === "/api/check_frame") {
        const label = headers.get("x-postit-camera");
        return this.frameRoute(path, init.body, label ? unquote(label) : null, headers)
          .catch((error) => this.error(error));
      }
      const text = typeof init.body === "string" ? init.body : "";
      if (text.length > MAX_BODY_BYTES) {
        throw new RequestError("La captura o acción supera 8 MB.", HTTP.REQUEST_ENTITY_TOO_LARGE);
      }
      let payload;
      try {
        payload = JSON.parse(text);
      } catch {
        throw new RequestError("JSON inválido.");
      }
      if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
        throw new RequestError("La acción debe ser un objeto JSON.");
      }
      const body = path === "/api/camera" ? this.controller.usbApply(payload) : this.controller.action(payload);
      return { status: 200, body };
    } catch (error) {
      return this.error(error);
    }
  }

  async frameRoute(path, blob, label, headers = new Map()) {
    const ticket = this.controller.ticket();   // what this frame belongs to, before the decode awaits
    // Header checks first, as the Python handler does before decoding.
    if (path === "/api/check_frame") this.controller.checkKind(headers.get("x-postit-check") ?? null);
    else this.controller.scanLabel(label);
    if (path === "/api/align_frame") alignKey(headers.get("x-postit-align") ?? null);
    let cv;
    try {
      cv = await this.cvReady;
    } catch (error) {
      throw new RequestError(error.message, HTTP.BAD_GATEWAY);
    }
    const frame = await this.decode(cv, blob);
    try {
      if (path === "/api/frame") return { status: 200, body: this.controller.scan(frame, label, ticket) };
      if (path === "/api/align_frame") {
        return { status: 200, body: this.controller.alignFrame(frame, headers.get("x-postit-align") ?? null, label) };
      }
      if (path === "/api/check_frame") {
        return { status: 200, body: this.controller.checkFrame(frame, headers.get("x-postit-check") ?? null) };
      }
      const started = this.now();
      const body = this.controller.preview(frame, label, ticket);
      this.recordTiming(this.now() - started, frame);
      return { status: 200, body };
    } finally {
      frame.delete();
    }
  }

  recordTiming(ms, frame) {
    this.timings.push(ms);
    if (this.timings.length < TIMING_EVERY) return;
    const sorted = [...this.timings].sort((a, b) => a - b);
    this.timings = [];
    this.log?.({ at: new Date().toISOString(), event: "preview_timing", frames: sorted.length,
      p50_ms: Math.round(percentile(sorted, 0.5)), p95_ms: Math.round(percentile(sorted, 0.95)),
      width: frame.cols, height: frame.rows });
  }
}

/** The panel's backend: one per page, reachable by the projector window. */
export function startBrowserBackend(win = window) {
  if (win.postitBackend) return win.postitBackend;
  let storage = null;
  try {
    storage = win.localStorage;
  } catch {
    storage = null; // private mode: colors and presets last until reload
  }
  const log = eventLog(storage, win.console);
  const controller = new GameController({ storage, log });
  const backend = new BrowserBackend({ controller, cv: loadOpenCv(win.document), log });
  win.postitBackend = backend;
  win.postitEvents = () => {
    try {
      return JSON.parse(storage?.getItem(EVENTS_KEY) || "[]");
    } catch {
      return [];
    }
  };
  log({ at: new Date().toISOString(), event: "browser_backend_started", user_agent: win.navigator?.userAgent });
  return backend;
}
