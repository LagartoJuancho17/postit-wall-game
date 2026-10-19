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

function jsonResponse({ status, body }) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8" },
  });
}

/** `role`: "panel" owns the backend; "projector" borrows the panel's. */
export function createApi(role) {
  if (backendMode === "server") return (url, init) => fetch(url, init);
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
