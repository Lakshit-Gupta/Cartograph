// PostgREST minimal client - read-only GET only.
// All views are served via the api-service reverse proxy under /api/postgrest/.
const BASE = "/api/postgrest";

/**
 * Fetch a PostgREST view.
 * @param {string} view - view name (without leading slash).
 * @param {Object} [params] - PostgREST query params (order, limit, offset, select, filters).
 * @param {AbortSignal} [signal]
 * @returns {Promise<Array<Object>>}
 */
export async function getView(view, params = {}, signal) {
  const url = new URL(`${BASE}/${view}`, window.location.origin);
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    url.searchParams.set(k, String(v));
  }
  const res = await fetch(url.toString(), {
    method: "GET",
    headers: { Accept: "application/json" },
    credentials: "same-origin",
    signal,
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`GET ${view} ${res.status} ${res.statusText} ${body.slice(0, 200)}`);
  }
  const data = await res.json();
  if (!Array.isArray(data)) {
    throw new Error(`Expected array from ${view}, got ${typeof data}`);
  }
  return data;
}

/** Pull the first row out (or null) - convenient for single-row views like v_overview. */
export async function getOne(view, params = {}, signal) {
  const rows = await getView(view, params, signal);
  return rows.length > 0 ? rows[0] : null;
}
