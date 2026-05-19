// Applications tab - last 100 applications.
import { getView } from "../lib/postgrest.js";
import { el, td, clear, buildHeader, sortBy, relTime } from "../lib/dom.js";

const COLS = [
  { key: "sent_at",               label: "sent" },
  { key: "title",                 label: "opp" },
  { key: "company",               label: "company" },
  { key: "method",                label: "method" },
  { key: "resume_compile_status", label: "resume" },
  { key: "response_status",       label: "response" },
  { key: "response_at",           label: "responded" },
];

let state = { rows: [], sort: { key: "sent_at", dir: "desc" } };

function statusPill(value) {
  if (!value) return null;
  const v = String(value).toLowerCase();
  let cls = "";
  if (["positive", "interview", "offer", "tailored"].includes(v))     cls = "good";
  else if (["rejected", "failed"].includes(v))                         cls = "bad";
  else if (["pending", "fallback", "no_reply"].includes(v))            cls = "warn";
  const pill = el("span", { class: `pill ${cls}` });
  pill.textContent = v;
  return pill;
}

function buildRow(r) {
  const tr = el("tr");
  tr.appendChild(td(relTime(r.sent_at)));

  const titleTd = el("td", { class: "wrap" });
  titleTd.textContent = r.title || "(no title)";
  tr.appendChild(titleTd);

  tr.appendChild(td(r.company));
  tr.appendChild(td(r.method));

  const rTd = el("td", {});
  const rPill = statusPill(r.resume_compile_status);
  if (rPill) rTd.appendChild(rPill); else rTd.appendChild(document.createTextNode("--"));
  tr.appendChild(rTd);

  const sTd = el("td", {});
  const sPill = statusPill(r.response_status);
  if (sPill) sTd.appendChild(sPill); else sTd.appendChild(document.createTextNode("--"));
  tr.appendChild(sTd);

  tr.appendChild(td(r.response_at ? relTime(r.response_at) : "--"));
  return tr;
}

function renderTable(container) {
  clear(container);
  const panel = el("section", { class: "panel" }, [
    el("h2", {}, `applications · last ${state.rows.length}`),
  ]);
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
    tr.appendChild(el("td", { class: "muted", colspan: String(COLS.length) }, "no applications sent yet"));
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
  container.appendChild(el("div", { class: "placeholder" }, "loading applications…"));
  state.rows = await getView("v_recent_applications", { order: "sent_at.desc", limit: 100 }, signal);
  renderTable(container);
}
