// Opportunities tab - sortable table of last 50 ranked opps.
import { getView } from "../lib/postgrest.js";
import { el, td, clear, buildHeader, sortBy, relTime } from "../lib/dom.js";

const COLS = [
  { key: "score",      label: "score" },
  { key: "title",      label: "title" },
  { key: "company",    label: "company" },
  { key: "category",   label: "lane" },
  { key: "source",     label: "source" },
  { key: "posted_at",  label: "posted" },
  { key: "first_seen", label: "seen" },
];

let state = { rows: [], sort: { key: "score", dir: "desc" } };

function laneClass(cat) {
  if (!cat) return "";
  const k = String(cat).toLowerCase();
  if (k.includes("freelance"))  return "good";
  if (k.includes("fellowship")) return "warn";
  return "";
}

function buildRow(r) {
  const tr = el("tr");
  const scoreCell = el("td", { class: "mono-num" });
  const score = Number(r.score || 0);
  scoreCell.textContent = score.toFixed(3);
  if (score >= 0.7) scoreCell.classList.add("good");
  else if (score < 0.3) scoreCell.classList.add("muted");
  tr.appendChild(scoreCell);

  const titleTd = el("td", { class: "wrap" });
  titleTd.textContent = r.title || "(no title)";
  tr.appendChild(titleTd);

  tr.appendChild(td(r.company));

  const laneTd = el("td", {});
  const pill = el("span", { class: `pill ${laneClass(r.category)}` });
  pill.textContent = r.category || "?";
  laneTd.appendChild(pill);
  tr.appendChild(laneTd);

  tr.appendChild(td(r.source));
  tr.appendChild(td(relTime(r.posted_at)));
  tr.appendChild(td(relTime(r.first_seen)));
  return tr;
}

function renderTable(container) {
  clear(container);

  const panel = el("section", { class: "panel" }, [
    el("h2", {}, `opportunities · ${state.rows.length} recent (ranked)`),
  ]);
  const wrap = el("div", { class: "table-wrap" });
  const table = el("table");
  const thead = el("thead");
  thead.appendChild(buildHeader(COLS, state.sort, (key) => {
    if (state.sort.key === key) {
      state.sort.dir = state.sort.dir === "asc" ? "desc" : "asc";
    } else {
      state.sort = { key, dir: "desc" };
    }
    renderTable(container);
  }));
  table.appendChild(thead);

  const tbody = el("tbody");
  const sorted = sortBy(state.rows, state.sort.key, state.sort.dir);
  if (sorted.length === 0) {
    const tr = el("tr");
    const cell = el("td", { class: "muted", colspan: String(COLS.length) }, "no opps yet");
    tr.appendChild(cell);
    tbody.appendChild(tr);
  } else {
    for (const r of sorted) tbody.appendChild(buildRow(r));
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  panel.appendChild(wrap);
  container.appendChild(panel);
}

export async function render(container, signal) {
  clear(container);
  container.appendChild(el("div", { class: "placeholder" }, "loading opportunities…"));
  state.rows = await getView("v_recent_opps", { order: "score.desc", limit: 50 }, signal);
  renderTable(container);
}
