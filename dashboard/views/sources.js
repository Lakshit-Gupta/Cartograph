// Sources tab - all source health snapshot.
import { getView } from "../lib/postgrest.js";
import { el, td, clear, buildHeader, sortBy, relTime } from "../lib/dom.js";

const COLS = [
  { key: "slug",                     label: "slug" },
  { key: "status",                   label: "status" },
  { key: "opps_extracted_30d",       label: "30d opps" },
  { key: "ranking_weight",           label: "weight" },
  { key: "last_successful_crawl_at", label: "last crawl" },
  { key: "ban_observed_at",          label: "last ban" },
];

let state = { rows: [], sort: { key: "opps_extracted_30d", dir: "desc" } };

function statusClass(s) {
  if (!s) return "";
  const v = String(s).toLowerCase();
  if (v === "active")            return "good";
  if (v === "quarantined" || v === "banned") return "bad";
  if (v === "paused" || v === "degraded")    return "warn";
  return "";
}

function renderTable(container) {
  clear(container);

  const total = state.rows.length;
  const active = state.rows.filter(r => String(r.status || "").toLowerCase() === "active").length;
  const quarantined = state.rows.filter(r => /quaran|ban/i.test(String(r.status || ""))).length;

  // summary row
  const summary = el("section", { class: "tile-grid" }, [
    el("div", { class: "tile" }, [
      el("div", { class: "tile-label" }, "total sources"),
      el("div", { class: "tile-value" }, String(total)),
    ]),
    el("div", { class: "tile" }, [
      el("div", { class: "tile-label" }, "active"),
      el("div", { class: "tile-value good" }, String(active)),
    ]),
    el("div", { class: "tile" }, [
      el("div", { class: "tile-label" }, "quarantined / banned"),
      el("div", { class: "tile-value " + (quarantined > 0 ? "bad" : "") }, String(quarantined)),
    ]),
  ]);
  container.appendChild(summary);

  const panel = el("section", { class: "panel" }, [el("h2", {}, "source health")]);
  const wrap = el("div", { class: "table-wrap" });
  const table = el("table");
  const thead = el("thead");
  thead.appendChild(buildHeader(COLS, state.sort, (key) => {
    if (state.sort.key === key) state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
    else state.sort = { key, dir: "desc" };
    renderTable(container);
  }));
  table.appendChild(thead);

  const tbody = el("tbody");
  const sorted = sortBy(state.rows, state.sort.key, state.sort.dir);
  if (sorted.length === 0) {
    const tr = el("tr");
    tr.appendChild(el("td", { class: "muted", colspan: String(COLS.length) }, "no sources"));
    tbody.appendChild(tr);
  } else {
    for (const r of sorted) {
      const tr = el("tr");
      tr.appendChild(td(r.slug));
      const sTd = el("td", {});
      const pill = el("span", { class: `pill ${statusClass(r.status)}` });
      pill.textContent = r.status || "?";
      sTd.appendChild(pill);
      tr.appendChild(sTd);
      tr.appendChild(td(r.opps_extracted_30d != null ? Number(r.opps_extracted_30d).toLocaleString() : "0"));
      tr.appendChild(td(r.ranking_weight != null ? Number(r.ranking_weight).toFixed(2) : "--"));
      tr.appendChild(td(relTime(r.last_successful_crawl_at)));
      tr.appendChild(td(r.ban_observed_at ? relTime(r.ban_observed_at) : "--"));
      tbody.appendChild(tr);
    }
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  panel.appendChild(wrap);
  container.appendChild(panel);
}

export async function render(container, signal) {
  clear(container);
  container.appendChild(el("div", { class: "placeholder" }, "loading sources…"));
  state.rows = await getView("v_source_health", { order: "opps_extracted_30d.desc", limit: 1000 }, signal);
  renderTable(container);
}
