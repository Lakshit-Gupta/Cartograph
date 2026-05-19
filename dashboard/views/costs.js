// Costs tab - per-day spend + per-model breakdown.
import { getView } from "../lib/postgrest.js";
import { el, td, clear, buildHeader, sortBy } from "../lib/dom.js";

const COLS = [
  { key: "date",          label: "date" },
  { key: "kind",          label: "kind" },
  { key: "model",         label: "model" },
  { key: "usd",           label: "usd" },
  { key: "input_tokens",  label: "in tok" },
  { key: "output_tokens", label: "out tok" },
];

let state = { rows: [], sort: { key: "date", dir: "desc" } };

function renderDailyChart(container, rows) {
  const byDate = new Map();
  for (const r of rows) {
    const d = (r.date || "").slice(0, 10);
    if (!d) continue;
    byDate.set(d, (byDate.get(d) || 0) + Number(r.usd || 0));
  }
  const sorted = [...byDate.entries()].sort(([a], [b]) => (a < b ? -1 : 1));
  if (sorted.length === 0) {
    container.appendChild(el("div", { class: "muted" }, "no cost data"));
    return;
  }
  const max = Math.max(...sorted.map(([, v]) => v), 0.01);
  const W = 600, H = 96, P = 8;
  const bw = (W - 2 * P) / sorted.length;

  const SVG = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.style.height = "96px";

  sorted.forEach(([d, v], i) => {
    const h = (v / max) * (H - 2 * P);
    const x = P + i * bw;
    const y = H - P - h;
    const rect = document.createElementNS(SVG, "rect");
    rect.setAttribute("x", x.toFixed(2));
    rect.setAttribute("y", y.toFixed(2));
    rect.setAttribute("width", Math.max(1, bw - 1).toFixed(2));
    rect.setAttribute("height", h.toFixed(2));
    rect.setAttribute("fill", "var(--accent)");
    rect.setAttribute("opacity", "0.85");
    const title = document.createElementNS(SVG, "title");
    title.textContent = `${d}: $${v.toFixed(3)}`;
    rect.appendChild(title);
    svg.appendChild(rect);
  });

  const tMax = document.createElementNS(SVG, "text");
  tMax.setAttribute("x", "4"); tMax.setAttribute("y", "12");
  tMax.textContent = `max $${max.toFixed(2)}`;
  svg.appendChild(tMax);

  container.appendChild(svg);
}

function renderTable(container) {
  clear(container);

  // chart panel
  const chartPanel = el("section", { class: "panel" }, [el("h2", {}, "daily spend - last 30d")]);
  renderDailyChart(chartPanel, state.rows);
  container.appendChild(chartPanel);

  // table panel
  const panel = el("section", { class: "panel" }, [el("h2", {}, "per-call ledger")]);
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
    tr.appendChild(el("td", { class: "muted", colspan: String(COLS.length) }, "no cost rows"));
    tbody.appendChild(tr);
  } else {
    for (const r of sorted) {
      const tr = el("tr");
      tr.appendChild(td((r.date || "").slice(0, 10)));
      tr.appendChild(td(r.kind));
      tr.appendChild(td(r.model));
      const usdTd = el("td", { class: "mono-num right" });
      usdTd.textContent = `$${Number(r.usd || 0).toFixed(4)}`;
      tr.appendChild(usdTd);
      tr.appendChild(td(r.input_tokens != null ? Number(r.input_tokens).toLocaleString() : "--"));
      tr.appendChild(td(r.output_tokens != null ? Number(r.output_tokens).toLocaleString() : "--"));
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
  container.appendChild(el("div", { class: "placeholder" }, "loading costs…"));
  state.rows = await getView("v_cost_daily", { order: "date.desc", limit: 500 }, signal);
  renderTable(container);
}
