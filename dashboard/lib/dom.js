// Tiny DOM helpers - avoid innerHTML for any user-derived strings.
// All text children become text nodes via textContent semantics.

/**
 * Create a DOM element.
 * @param {string} tag
 * @param {Object} [attrs] - attribute map. `class` and `data-*` supported.
 * @param {string|Node|Array<string|Node>} [children]
 */
export function el(tag, attrs = {}, children = null) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = String(v);
    else node.setAttribute(k, String(v));
  }
  if (children != null) {
    appendChildren(node, children);
  }
  return node;
}

export function appendChildren(node, children) {
  if (children == null) return;
  if (Array.isArray(children)) {
    for (const c of children) appendChildren(node, c);
    return;
  }
  if (children instanceof Node) {
    node.appendChild(children);
    return;
  }
  node.appendChild(document.createTextNode(String(children)));
}

export function frag(...children) {
  const f = document.createDocumentFragment();
  for (const c of children) appendChildren(f, c);
  return f;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** Escape a string for use inside an HTML attribute value. Rarely needed since
 *  setAttribute handles quoting, but exposed for SVG inline-style edge cases. */
export function escapeAttr(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

/** Build a <td> with text content (no markup). */
export function td(value, opts = {}) {
  const node = el("td", { class: opts.class || "" });
  if (value == null || value === "") {
    node.appendChild(document.createTextNode("--"));
    node.classList.add("muted");
  } else {
    node.appendChild(document.createTextNode(String(value)));
  }
  return node;
}

/** Build a header row with sortable columns. cols = [{key,label,kind?}] */
export function buildHeader(cols, sortState, onSort) {
  const tr = el("tr");
  for (const c of cols) {
    const th = el("th", { "data-key": c.key }, c.label);
    if (sortState && sortState.key === c.key) {
      th.setAttribute("aria-sort", sortState.dir === "asc" ? "ascending" : "descending");
    }
    th.addEventListener("click", () => onSort(c.key));
    tr.appendChild(th);
  }
  return tr;
}

/** Generic sort - returns a new array sorted by key. */
export function sortBy(rows, key, dir) {
  const out = rows.slice();
  out.sort((a, b) => {
    const va = a?.[key]; const vb = b?.[key];
    if (va == null && vb == null) return 0;
    if (va == null) return 1;
    if (vb == null) return -1;
    if (typeof va === "number" && typeof vb === "number") return va - vb;
    return String(va).localeCompare(String(vb));
  });
  if (dir === "desc") out.reverse();
  return out;
}

/** Format an ISO date - "5m ago", "2h ago", "Mar 04 12:33". */
export function relTime(iso) {
  if (!iso) return "--";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "--";
  const diff = (Date.now() - t) / 1000;
  if (diff < 60)         return `${Math.floor(diff)}s ago`;
  if (diff < 3600)       return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400)      return `${Math.floor(diff / 3600)}h ago`;
  if (diff < 7 * 86400)  return `${Math.floor(diff / 86400)}d ago`;
  return new Date(iso).toISOString().slice(5, 16).replace("T", " ");
}
