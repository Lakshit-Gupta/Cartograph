// Cartograph dashboard - bootstrap, hash routing, auto-refresh, theme, status.
import * as overview     from "./views/overview.js";
import * as opps         from "./views/opps.js";
import * as applications from "./views/applications.js";
import * as costs        from "./views/costs.js";
import * as sources      from "./views/sources.js";
import * as refits       from "./views/refits.js";

const VIEWS = { overview, opps, applications, costs, sources, refits };
const DEFAULT_TAB = "overview";
const AUTO_REFRESH_MS = 60_000;
const STALE_THRESHOLD_MS = 5 * 60_000;

const $ = (sel) => document.querySelector(sel);
const view       = $("#view");
const tabsRoot   = $("#tabs");
const dot        = $("#status-dot");
const lastFetch  = $("#last-fetch");
const errBanner  = $("#error-banner");
const refreshBtn = $("#refresh-btn");
const themeBtn   = $("#theme-btn");
const autoChk    = $("#auto-refresh");

let currentTab = null;
let lastSuccessAt = 0;
let inFlight = null;     // AbortController
let timer = null;
let staleTimer = null;

// ---------- theme ----------
function loadTheme() {
  const saved = localStorage.getItem("cartograph.theme");
  if (saved === "light" || saved === "dark") {
    document.documentElement.dataset.theme = saved;
  }
}
function toggleTheme() {
  const cur = document.documentElement.dataset.theme || "dark";
  const next = cur === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("cartograph.theme", next);
}

// ---------- status dot ----------
function setStatus(state, msg) {
  dot.dataset.state = state;
  dot.title = msg || state;
}
function refreshStaleness() {
  if (!lastSuccessAt) return;
  const age = Date.now() - lastSuccessAt;
  if (dot.dataset.state === "error") return;          // sticky until next success
  if (age > STALE_THRESHOLD_MS) setStatus("stale", `stale (${Math.floor(age / 1000)}s)`);
  else                          setStatus("live",  `live (${Math.floor(age / 1000)}s ago)`);
}
function updateLastFetchText() {
  if (!lastSuccessAt) { lastFetch.textContent = "--:--:--"; return; }
  const d = new Date(lastSuccessAt);
  lastFetch.textContent = d.toTimeString().slice(0, 8);
}

// ---------- error banner ----------
function showError(msg) {
  errBanner.hidden = false;
  errBanner.textContent = `! ${msg}`;
}
function hideError() {
  errBanner.hidden = true;
  errBanner.textContent = "";
}

// ---------- routing ----------
function parseTab() {
  const raw = (location.hash || "").replace(/^#/, "").split("?")[0];
  return VIEWS[raw] ? raw : DEFAULT_TAB;
}

function markActiveTab(tab) {
  for (const a of tabsRoot.querySelectorAll("a")) {
    a.classList.toggle("active", a.dataset.tab === tab);
  }
}

// ---------- main render ----------
async function loadView(tab) {
  currentTab = tab;
  markActiveTab(tab);
  if (inFlight) inFlight.abort();
  inFlight = new AbortController();
  setStatus(lastSuccessAt ? dot.dataset.state : "boot", "fetching");
  try {
    await VIEWS[tab].render(view, inFlight.signal);
    lastSuccessAt = Date.now();
    hideError();
    setStatus("live", "live");
    updateLastFetchText();
  } catch (err) {
    if (err && err.name === "AbortError") return; // user switched tabs
    console.warn("view load failed", err);
    setStatus("error", String(err.message || err));
    showError(`failed to load ${tab}: ${err.message || err}`);
  } finally {
    inFlight = null;
  }
}

function scheduleAutoRefresh() {
  if (timer) clearInterval(timer);
  if (autoChk.checked) {
    timer = setInterval(() => {
      if (document.visibilityState === "visible" && currentTab) loadView(currentTab);
    }, AUTO_REFRESH_MS);
  }
}

// ---------- wiring ----------
function init() {
  loadTheme();
  themeBtn.addEventListener("click", toggleTheme);

  refreshBtn.addEventListener("click", () => { if (currentTab) loadView(currentTab); });

  autoChk.addEventListener("change", () => {
    localStorage.setItem("cartograph.auto", autoChk.checked ? "1" : "0");
    scheduleAutoRefresh();
  });
  const autoPref = localStorage.getItem("cartograph.auto");
  if (autoPref === "0") autoChk.checked = false;

  window.addEventListener("hashchange", () => loadView(parseTab()));

  // refocus tab => refresh if stale
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && currentTab) {
      const age = Date.now() - lastSuccessAt;
      if (age > STALE_THRESHOLD_MS) loadView(currentTab);
    }
  });

  // staleness ticker - updates dot every 10s
  staleTimer = setInterval(refreshStaleness, 10_000);

  if (!location.hash) location.hash = `#${DEFAULT_TAB}`;
  scheduleAutoRefresh();
  loadView(parseTab());
}

// boot when DOM ready (module scripts are deferred, so DOM is parsed already)
init();
