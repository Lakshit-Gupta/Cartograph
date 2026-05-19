// Overview tab - tile grid + score components viz + 30d cost sparkline.
// All user-supplied strings are inserted via textContent; only static markup
// uses template literals + DOM .append() to avoid XSS via innerHTML.
import { getOne, getView } from "../lib/postgrest.js";
import { el, frag, clear, escapeAttr } from "../lib/dom.js";

const fmt = {
  int: (n) => (n == null ? "--" : Number(n).toLocaleString()),
  pct: (n) => (n == null ? "--" : `${(Number(n) * 100).toFixed(1)}%`),
  usd: (n) => (n == null ? "$--" : `$${Number(n).toFixed(2)}`),
};

function tile(label, value, sub, cls) {
  const v = el("div", { class: `tile-value ${cls || ""}` }, value);
  const children = [el("div", { class: "tile-label" }, label), v];
  if (sub) children.push(el("div", { class: "tile-sub" }, sub));
  return el("div", { class: "tile" }, children);
}

function classify(value, goodIf, warnIf) {
  if (value == null) return "";
  if (typeof goodIf === "function" && goodIf(value)) return "good";
  if (typeof warnIf === "function" && warnIf(value)) return "warn";
  return "";
}

const COMPONENT_COLORS = {
  kw_match:       "var(--accent)",
  embedding_sim:  "var(--info)",
  comp_score:     "#a371f7",
  freshness:      "var(--warn)",
  source_quality: "#39c5cf",
  response_rate:  "#f778ba",
};

function scoreBar(components) {
  let comp = components;
  if (typeof comp === "string") {
    try { comp = JSON.parse(comp); } catch { comp = null; }
  }
  if (!comp || typeof comp !== "object") {
    return el("div", { class: "muted" }, "no score breakdown");
  }
  const entries = Object.entries(comp).filter(([, v]) => typeof v === "number" && v > 0);
  if (entries.length === 0) {
    return el("div", { class: "muted" }, "empty components");
  }
  const total = entries.reduce((a, [, v]) => a + v, 0) || 1;
  const bar = el("div", { class: "score-bar" });
  const legend = el("div", { class: "score-legend" });
  for (const [k, v] of entries) {
    const pct = (v / total) * 100;
    const color = COMPONENT_COLORS[k] || "var(--fg-muted)";
    const seg = el("span", {});
    seg.style.width = `${pct.toFixed(2)}%`;
    seg.style.background = color;
    seg.title = `${k}: ${v.toFixed(3)}`;
    bar.appendChild(seg);

    const swatch = el("i", {});
    swatch.style.background = color;
    legend.appendChild(el("span", {}, [swatch, document.createTextNode(`${k} ${v.toFixed(2)}`)]));
  }
  return frag(bar, legend);
}

function sparkline(daily) {
  const byDate = new Map();
  for (const row of daily) {
    const d = (row.date || "").slice(0, 10);
    if (!d) continue;
    byDate.set(d, (byDate.get(d) || 0) + Number(row.usd || 0));
  }
  const points = [...byDate.entries()].sort(([a], [b]) => (a < b ? -1 : 1));
  if (points.length === 0) return el("div", { class: "muted" }, "no cost data");
  const W = 600, H = 64, P = 4;
  const max = Math.max(...points.map(([, v]) => v), 0.01);
  const step = points.length > 1 ? (W - 2 * P) / (points.length - 1) : 0;
  const coords = points.map(([d, v], i) => ({
    x: P + i * step,
    y: H - P - (v / max) * (H - 2 * P),
    d, v,
  }));
  const line = coords.map((c, i) => `${i ? "L" : "M"}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(" ");
  const area = `${line} L${(P + (points.length - 1) * step).toFixed(1)},${H - P} L${P},${H - P} Z`;
  const last = coords[coords.length - 1];

  const SVG = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "cost sparkline");

  const pArea = document.createElementNS(SVG, "path");
  pArea.setAttribute("class", "area"); pArea.setAttribute("d", area);
  const pLine = document.createElementNS(SVG, "path");
  pLine.setAttribute("class", "line"); pLine.setAttribute("d", line);
  const tRight = document.createElementNS(SVG, "text");
  tRight.setAttribute("x", String(W - 4)); tRight.setAttribute("y", "12");
  tRight.setAttribute("text-anchor", "end");
  tRight.textContent = `$${last.v.toFixed(2)} @ ${last.d.slice(5)}`;
  const tLeft = document.createElementNS(SVG, "text");
  tLeft.setAttribute("x", "4"); tLeft.setAttribute("y", "12");
  tLeft.textContent = `max $${max.toFixed(2)}`;

  svg.append(pArea, pLine, tRight, tLeft);
  return svg;
}

export async function render(container, signal) {
  clear(container);
  container.appendChild(el("div", { class: "placeholder" }, "loading overview…"));

  const [ov, opps, daily] = await Promise.all([
    getOne("v_overview", {}, signal),
    getView("v_recent_opps", { order: "score.desc", limit: 1 }, signal),
    getView("v_cost_daily", { order: "date.desc", limit: 60 }, signal),
  ]);
  const o = ov || {};
  const topOpp = opps[0] || null;

  clear(container);
  const grid = el("section", { class: "tile-grid", "aria-label": "key metrics" }, [
    tile("opps 24h", fmt.int(o.opps_24h), "newly ranked",
         classify(o.opps_24h, v => v > 0)),
    tile("applied today", fmt.int(o.applied_today), `7d: ${fmt.int(o.applied_7d)}`,
         classify(o.applied_today, v => v > 0, v => v === 0)),
    tile("sent 24h", fmt.int(o.sent_24h), "emails out"),
    tile("resp rate 30d", fmt.pct(o.response_rate_30d), "any positive",
         classify(o.response_rate_30d, v => v >= 0.05, v => v < 0.02)),
    tile("cost today", fmt.usd(o.cost_today_usd), `mtd: ${fmt.usd(o.cost_mtd_usd)}`,
         classify(o.cost_today_usd, v => v < 2, v => v >= 2.5)),
    tile("sources",
         `${fmt.int(o.active_sources)}/${fmt.int((o.active_sources || 0) + (o.quarantined_sources || 0))}`,
         `quarantined: ${fmt.int(o.quarantined_sources)}`,
         classify(o.quarantined_sources, v => v === 0, v => v >= 3)),
    tile("identities", fmt.int(o.healthy_identities), "healthy",
         classify(o.healthy_identities, v => v > 0, v => v === 0)),
  ]);
  container.appendChild(grid);

  const topPanel = el("section", { class: "panel" }, [el("h2", {}, "top-ranked opp - score breakdown")]);
  if (topOpp) {
    const header = el("div", {});
    header.style.marginBottom = "8px";
    header.append(
      el("strong", {}, String(topOpp.title || "(no title)")),
      el("span", { class: "muted" }, ` · ${topOpp.company || "?"}`),
      el("span", { class: "muted" }, ` · score ${Number(topOpp.score || 0).toFixed(3)}`),
    );
    topPanel.append(header, scoreBar(topOpp.score_components));
  } else {
    topPanel.appendChild(el("div", { class: "muted" }, "no ranked opps yet"));
  }
  container.appendChild(topPanel);

  const costPanel = el("section", { class: "panel" }, [
    el("h2", {}, "spend - last 30d"),
    sparkline(daily),
  ]);
  container.appendChild(costPanel);

  // suppress lint-style "unused" on escapeAttr if not used here
  void escapeAttr;
}
