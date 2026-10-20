/**
 * The panel's side of the camera <-> projector alignment and of the overlay
 * check (docs/alignment-and-color.md sections 2.3 and 3).
 *
 * Pure sequencing: every effect (backend actions, projector messages,
 * camera frames, uploads, waits) is injected, so Node tests drive it with
 * fakes and the panel wires the real ones. The projector is never left
 * showing a pattern or a neutral scene: every exit path sends "resume".
 */

/** Settle times in ms; an automatic camera also needs its exposure to settle. */
export function alignTiming(cameraFixed) {
  return cameraFixed ? { warmup: 300, settle: 150 } : { warmup: 1000, settle: 350 };
}

/** Decoder failures that a slower run can fix (frames caught mid-change). */
export const RETRY_CODES = new Set(["inaccurate", "inconsistent"]);

/**
 * deps:
 *  - action(name, extra) -> state            backend action
 *  - show(message) -> ack                    projector message, resolves on its ack
 *  - resume()                                 back to state-driven drawing
 *  - settle(ack, ms)                          ms after the ack and >= 2 new camera frames
 *  - sleep(ms)
 *  - capture() -> Blob                        camera frame, JPEG
 *  - upload(path, blob, headers) -> json      POST image/jpeg
 *  - progress(text)                           optional, for the panel
 */
async function collect(deps, timing) {
  const started = await deps.action("align_start");
  const layout = started.alignment.layout;
  try {
    await deps.show({ type: "align_pattern", layout, frame: "warmup" });
    await deps.sleep(timing.warmup);
    for (const [index, frame] of layout.frames.entries()) {
      const ack = await deps.show({ type: "align_pattern", layout, frame });
      await deps.settle(ack, timing.settle);
      const blob = await deps.capture();
      await deps.upload("/api/align_frame", blob, { "X-Postit-Align": `${frame.bit}:${frame.inverse ? 1 : 0}` });
      deps.progress?.(`Alineando: patrón ${index + 1}/${layout.frames.length}…`);
    }
  } catch (error) {
    deps.resume();
    try {
      await deps.action("align_cancel");
    } catch {
      // The run is already gone (projector closed, backend restarted).
    }
    throw error;
  }
  deps.resume();
  return deps.action("align_solve");
}

/**
 * One full alignment: patterns, decode, and on success the overlay check.
 * A failure whose code a slower run can fix is retried once with doubled
 * waits. Returns the last backend state.
 */
export async function runAlignment(deps, { cameraFixed = false } = {}) {
  const timing = alignTiming(cameraFixed);
  let state = await collect(deps, timing);
  if (state.alignment.status === "failed" && RETRY_CODES.has(state.alignment.code)) {
    deps.progress?.("Repitiendo la alineación más despacio…");
    state = await collect(deps, { warmup: timing.warmup * 2, settle: timing.settle * 2 });
  }
  if (state.alignment.status !== "ready") return state;
  return runOverlayCheck(deps, timing);
}

/** Contract 2.3: neutral frame, overlay frame with probes, then check_solve. */
export async function runOverlayCheck(deps, timing) {
  try {
    deps.progress?.("Verificando el relleno de color…");
    for (const [kind, type] of [["neutral", "neutral"], ["overlay", "overlay_test"]]) {
      const ack = await deps.show({ type });
      await deps.settle(ack, timing.settle);
      await deps.upload("/api/check_frame", await deps.capture(), { "X-Postit-Check": kind });
    }
  } finally {
    deps.resume();
  }
  return deps.action("check_solve");
}

/**
 * Whether the panel should start an alignment on its own: the projector is
 * fullscreen and ready, the camera streams, nothing else runs, the backend
 * has no valid mapping (none or stale; never after a failure, so a failure
 * cannot loop), and these exact conditions were not tried yet.
 */
export function shouldAutoAlign({ state, cameraReady, projectorReady, fullscreen, busy, tried }) {
  if (!cameraReady || !projectorReady || !fullscreen || busy || !state?.alignment) return false;
  if (!["none", "stale"].includes(state.alignment.status)) return false;
  if (state.game?.status === "running" && !state.game?.paused) return false;
  if (!(state.camera?.width > 0)) return false;
  return !tried.has(autoAlignKey(state));
}

/** The conditions an alignment depends on: projector size and camera frame. */
export function autoAlignKey(state) {
  const { projection = {}, camera = {} } = state ?? {};
  return `${projection.width}x${projection.height}|${camera.label ?? ""}|${camera.width}x${camera.height}`;
}
