/**
 * Gate test: no backend call can hang the panel or the projector (api.mjs).
 * A request that never answers fails with the Spanish timeout message; the
 * projector's own abort still wins; answers pass through untouched.
 */
import assert from "node:assert/strict";
import test from "node:test";

import { createApi, fetchWithDeadline, TIMEOUT_MESSAGE } from "../ui/api.mjs";

/** Like fetch with a server that never answers: only an abort settles it, even one before the call. */
const never = (url, init) => new Promise((_, reject) => {
  const abort = () => reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
  if (init.signal.aborted) abort();
  else init.signal.addEventListener("abort", abort);
});

test("a request that never answers fails after the deadline, in Spanish", async () => {
  const api = createApi("panel", { fetchImpl: never, timeoutMs: 30 });
  const started = Date.now();
  await assert.rejects(api("/api/action", { method: "POST" }), (error) => {
    assert.equal(error.name, "TimeoutError");
    assert.equal(error.message, TIMEOUT_MESSAGE);
    return true;
  });
  assert.ok(Date.now() - started < 1000);
});

test("the caller's own abort is not reported as a timeout", async () => {
  const outer = new AbortController();
  const pending = fetchWithDeadline(never, "/api/tick", { signal: outer.signal }, 5000);
  outer.abort();
  await assert.rejects(pending, (error) => error.name === "AbortError");
  const already = new AbortController();
  already.abort();
  await assert.rejects(fetchWithDeadline(never, "/api/tick", { signal: already.signal }, 5000),
    (error) => error.name === "AbortError");
});

test("answers pass through with the caller's options and the timer is cleared", async () => {
  const seen = [];
  const api = createApi("projector", { timeoutMs: 20, fetchImpl: async (url, init) => {
    seen.push([url, init.method, init.headers, init.signal instanceof AbortSignal]);
    return { status: 200, ok: true };
  } });
  const response = await api("/api/state", { method: "GET", headers: { a: "1" } });
  assert.equal(response.status, 200);
  assert.deepEqual(seen, [["/api/state", "GET", { a: "1" }, true]]);
  await new Promise((done) => setTimeout(done, 40)); // a late timer would throw nowhere, but must not abort anything
});
