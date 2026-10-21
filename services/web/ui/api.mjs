/**
 * Where the pages' /api calls go.
 *
 * Served by web.py (the local game, with USB camera control), every call is
 * a plain fetch to the Python server. In the static build (Vercel) the HTML
 * carries data-backend="browser": the panel starts the in-browser backend
 * (services/browser, a port of the Python services) and the projector window
 * uses the panel's through window.opener. Either way the page gets a real
 * Response with the same status and JSON.
 */

export const backendMode = globalThis.document?.documentElement?.dataset?.backend === "browser"
  ? "browser" : "server";

/**
 * How long a call to the Python server may take. Every answer is local and
 * takes milliseconds (an alignment decode, ~0.1 s); a request that hangs must
 * fail with a message instead of leaving the panel "Alineando…" forever or
 * the live preview stuck. The in-browser backend has no network, and its
 * first call may legitimately wait for the OpenCV download, so it has none.
 */
export const REQUEST_TIMEOUT_MS = 20000;
export const TIMEOUT_MESSAGE = "El servidor del juego no respondió a tiempo. Revisá que siga abierto y reintentá.";

/** `fetchImpl(url, init)` aborted after `ms`; the caller's own signal still aborts it first. */
export function fetchWithDeadline(fetchImpl, url, init = {}, ms = REQUEST_TIMEOUT_MS) {
  const controller = new AbortController();
  const outer = init.signal;
  const forward = () => controller.abort(outer.reason);
  if (outer?.aborted) forward();
  else outer?.addEventListener("abort", forward, { once: true });
  let expired = false;
  const timer = setTimeout(() => {
    expired = true;
    controller.abort();
  }, ms);
  return fetchImpl(url, { ...init, signal: controller.signal })
    .catch((error) => {
      throw expired ? Object.assign(new Error(TIMEOUT_MESSAGE), { name: "TimeoutError" }) : error;
    })
    .finally(() => {
      clearTimeout(timer);
      outer?.removeEventListener("abort", forward);
    });
}

function jsonResponse({ status, body }) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

/** `role`: "panel" owns the backend; "projector" borrows the panel's. */
export function createApi(role, { fetchImpl = (url, init) => fetch(url, init), timeoutMs = REQUEST_TIMEOUT_MS } = {}) {
  if (backendMode === "server") return (url, init) => fetchWithDeadline(fetchImpl, url, init, timeoutMs);
  let own = null;
  const backend = () => {
    if (role === "projector") {
      let remote = null;
      try {
        remote = window.opener?.postitBackend ?? null;
      } catch {
        remote = null;
      }
      if (!remote) throw new Error("Abrí la proyección desde el panel de control (tecla J).");
      return remote;
    }
    own ??= import("./browser/backend.mjs").then((module) => module.startBrowserBackend(window));
    return own;
  };
  return async (url, init = {}) => {
    const answer = await (await backend()).handle(url, init);
    return jsonResponse(answer);
  };
}
