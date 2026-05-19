// Refits tab - ranker_weights_fit and source_refit_log history.
import { getView } from "../lib/postgrest.js";
import { el, td, clear, buildHeader, sortBy, relTime } from "../lib/dom.js";

const RANKER_COLS = [
  { key: "fit_at",         label: "when" },
  { key: "version",        label: "version" },
  { key: "n_samples",      label: "samples" },
  { key: "auc",            label: "auc" },
  { key: "loss",           label: "loss" },
  { key: "weights_summary", label: "weights" },
];

const SOURCE_COLS = [
  { key: "fit_at",       label: "when" },
  { key: "n_apps",       label: "apps" },
  { key: "n_sources",    label: "sources updated" },
  { key: "auc",          label: "auc" },
  { key: "notes",        label: "notes" },
];

let state = {
  ranker: [], source: [],
  rSort: { key: "fit_at", dir: "desc" },
  sSort: { key: "fit_at", dir: "desc" },
};

function buildPanel(title, rows, cols, sortRef, onSortChange) {
  const panel = el("section", { class: "panel" }, [el("h2", {}, title)]);
  const wrap = el("div", { class: "table-wrap" });
  const table = el("table");
  const thead = el("thead");
  thead.appendChild(buildHeader(cols, sortRef, onSortChange));
  table.appendChild(thead);
  const tbody = el("tbody");
  const sorted = sortBy(rows, sortRef.key, sortRef.dir);
  if (sorted.length === 0) {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "muted", colspan: String(cols.length) }, "no rows"));
    tbody.appendChild(tr);
  } else {
    for (const r of sorted) {
      const tr = el("tr");
      for (const c of cols) {
        let v = r[c.key];
        if (c.key === "fit_at") v = relTime(v);
        else if (typeof v === "object" && v != null) v = JSON.stringify(v).slice(0, 80);
        else if (typeof v === "number") v = Number.isInteger(v) ? v.toLocaleString() : v.toFixed(3);
        tr.appendChild(td(v));
      }
      tbody.appendChild(tr);
    }
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  panel.appendChild(wrap);
  return panel;
}

function renderAll(container) {
  clear(container);
  container.appendChild(buildPanel(
    "ranker weight fits", state.ranker, RANKER_COLS, state.rSort,
    (key) => {
      if (state.rSort.key === key) state.rSort.dir = state.rSort.dir === "asc" ? "desc" : "asc";
      else state.rSort = { key, dir: "desc" };
      renderAll(container);
    },
  ));
  container.appendChild(buildPanel(
    "source weight refits", state.source, SOURCE_COLS, state.sSort,
    (key) => {
      if (state.sSort.key === key) state.sSort.dir = state.sSort.dir === "asc" ? "desc" : "asc";
      else state.sSort = { key, dir: "desc" };
      renderAll(container);
    },
  ));
}

export async function render(container, signal) {
  clear(container);
  container.appendChild(el("div", { class: "placeholder" }, "loading refits…"));
  const [r, s] = await Promise.all([
    getView("v_ranker_fits", { order: "fit_at.desc", limit: 14 }, signal),
    getView("v_source_refits", { order: "fit_at.desc", limit: 14 }, signal),
  ]);
  state.ranker = r; state.source = s;
  renderAll(container);
}
