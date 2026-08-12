/* ===========================================================================
   Divergence scanner dashboard — core: loading, state, board, drawer.
   The other four views live in views.js and hang off the same App object.

   No framework, no build step, no network beyond the repo's own results/
   directory. Everything below reads what scanner.py already writes.
   =========================================================================== */

const App = window.App = {
  pairs: [],        // results/latest.json
  meta: null,       // results/meta.json (or reconstructed from run.log)
  history: [],      // results/history/index.json
  labels: [],       // localStorage, importable from labels.json
  view: "board",
  sort: "annualised",
  query: "",
  filters: new Set(),
  thresholds: null, // live values in the lab; starts as the run's own
  shipped: null,    // the run's own thresholds, for "reset"
};

const RESULTS = "../results/";
const LS_LABELS = "divergence.labels";
const LS_THEME = "divergence.theme";

/* ── formatting ────────────────────────────────────────────────────────── */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

// Edge and gap are probability fractions; a trader reads them in percentage
// points, so 0.0148 shows as 1.48 — not 1.5%, which invites confusion with
// the annualised return sitting in the next column.
const pp = (v, digits = 2) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(digits);
const signedPp = (v) => (v === null || v === undefined) ? "—" : (v > 0 ? "+" : "") + pp(v);
const pct = (v, digits = 1) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(digits) + "%";
const num = (v, digits = 2) => (v === null || v === undefined) ? "—" : Number(v).toFixed(digits);
const money = (v) => (v === null || v === undefined) ? "—" : "$" + Number(v).toFixed(2);
const days = (v) => (v === null || v === undefined) ? "—" : Math.round(v) + "d";

function relativeTime(iso) {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return { text: "unknown", hours: Infinity };
  const hours = (Date.now() - then) / 3.6e6;
  if (hours < 1) return { text: Math.max(1, Math.round(hours * 60)) + "m ago", hours };
  if (hours < 48) return { text: Math.round(hours) + "h ago", hours };
  return { text: Math.round(hours / 24) + "d ago", hours };
}

const truncate = (text, n) => (text || "").length > n ? text.slice(0, n - 1) + "…" : (text || "");

/* ── the gate, mirrored from scanner.py ────────────────────────────────── */

/**
 * Mirror of `passes_gates` in scanner.py (search for `def passes_gates`).
 * This is the one piece of logic that exists in two languages; if the Python
 * changes, change it here too. The lab is worthless if the two disagree.
 *
 * `rules_sim === null` means one venue carried no rule text at all — an
 * absent text cannot be held to the floor. A measured 0.0 is two texts that
 * exist and disagree completely, and must fail.
 */
function passesGates(confidence, rulesSim, edge, t) {
  if (confidence < t.min_confidence) return [false, "confidence below threshold"];
  if (rulesSim !== null && rulesSim !== undefined && rulesSim < t.min_rules_sim)
    return [false, "rule texts disagree"];
  if (edge > t.max_plausible_edge) return [false, "edge implausibly large"];
  return [true, "kept"];
}

/** The scan's own keep rule, after the gate: `--arb-only` off by default. */
function passesEdgeFloor(pair, t) {
  return Math.abs(pair.mid_gap) >= t.min_edge || pair.edge >= t.min_edge;
}

App.passesGates = passesGates;

/** Same key `review`/`score` use to join a label to a pair. */
const labelKey = (pair) => `${pair.kalshi.id}|${pair.polymarket.url}`;
App.labelKey = labelKey;

/* ── loading ───────────────────────────────────────────────────────────── */

async function getJSON(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.json();
}

async function getText(path) {
  const res = await fetch(path, { cache: "no-store" });
  if (!res.ok) throw new Error(`${path} → ${res.status}`);
  return res.text();
}

/**
 * Rebuild the run metadata from the stderr log.
 *
 * Only used when results/meta.json is absent — a scan committed before
 * --meta existed, or a hand-run scan. Every field is optional and a miss
 * leaves a gap in the funnel rather than a wrong number.
 */
function metaFromLog(text, generatedAt) {
  // The log is written with LF on CI but arrives with CRLF in a Windows
  // checkout, and every line-anchored pattern below would miss on the \r.
  text = (text || "").replace(/\r\n/g, "\n");

  const grab = (re, group = 1) => {
    const m = text.match(re);
    return m ? Number(m[group].replace(/,/g, "")) : undefined;
  };
  const funnel = {
    kalshi_scanned: grab(/kalshi: scanned ([\d,]+) open markets/),
    kalshi_quoted: grab(/open markets, ([\d,]+) carry a resting quote/),
    kalshi_fetched: grab(/kalshi\s+([\d,]+) fetched/),
    kalshi_usable: grab(/kalshi\s+[\d,]+ fetched ->\s+([\d,]+) usable/),
    poly_fetched: grab(/polymarket\s+([\d,]+) fetched/),
    poly_usable: grab(/polymarket\s+[\d,]+ fetched ->\s+([\d,]+) usable/),
    ladder_members: grab(/flagged ([\d,]+) kalshi markets as one-of-N/),
    candidate_pairs: grab(/scoring ([\d,]+) candidate pairs/),
    skipped_prefilter: grab(/\(([\d,]+) skipped by prefilter/),
    skipped_ladder: grab(/skipped by prefilter, ([\d,]+) by ladder rule/),
    above_confidence: grab(/by ladder rule\), ([\d,]+) above confidence/),
    depth_priced: grab(/fetching order books for top ([\d,]+) pairs/),
    hidden_implausible: grab(/hid ([\d,]+) pair\(s\) with implausibly large/),
    kept: grab(/([\d,]+) pair\(s\) saved/),
    scoring_seconds: grab(/scored [\d,]+ pairs in ([\d.]+)s/),
  };
  for (const key of Object.keys(funnel)) {
    if (funnel[key] === undefined) delete funnel[key];
  }

  const dropped = {};
  const table = text.match(/dropped:\n([\s\S]*?)(?:\n\S|$)/);
  if (table) {
    for (const line of table[1].split("\n")) {
      const m = line.match(/^\s+([\d,]+)\s{2,}(.+?)\s*$/);
      if (m) dropped[m[2]] = Number(m[1].replace(/,/g, ""));
    }
  }

  const error = text.match(/^error: (.+)$/m);
  return {
    generated_at: generatedAt,
    reconstructed: true,
    thresholds: {
      min_confidence: grab(/above confidence ([\d.]+)/) ?? 0.45,
      min_rules_sim: 0.35,
      max_plausible_edge: (grab(/implausibly large edges \(>([\d.]+)pp\)/) ?? 10) / 100,
      min_edge: 0.01, size: 100, depth: 15, top: 25,
    },
    weights: { title: 0.55, rules: 0.28, date: 0.17 },
    funnel,
    dropped,
    errors: error ? [error[1]] : [],
  };
}

async function loadFromServer() {
  App.pairs = await getJSON(RESULTS + "latest.json");

  let stamp = null;
  try {
    // The file reads "last run (UTC): 2026-08-12T00:10:06Z" — pull the
    // timestamp out by shape rather than by splitting on a colon, which the
    // time itself also contains.
    const text = await getText(RESULTS + "last_run_time.txt");
    const found = text.match(/\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z/);
    if (found) stamp = found[0];
  } catch (_) { /* optional */ }

  try {
    App.meta = await getJSON(RESULTS + "meta.json");
  } catch (_) {
    // No meta.json: fall back to parsing the stderr log so a scan committed
    // before --meta existed still renders a funnel.
    try {
      App.meta = metaFromLog(await getText(RESULTS + "run.log"), stamp);
    } catch (__) {
      App.meta = metaFromLog("", stamp);
    }
  }

  try {
    App.history = await getJSON(RESULTS + "history/index.json");
  } catch (_) { App.history = []; }
}

/** The file:// path — the user hands us the files the fetch could not reach. */
function loadFromFiles(files) {
  const jobs = Array.from(files).map((file) => new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name.toLowerCase(), text: reader.result });
    reader.onerror = () => resolve(null);
    reader.readAsText(file);
  }));

  return Promise.all(jobs).then((loaded) => {
    let log = null;
    for (const item of loaded) {
      if (!item) continue;
      if (item.name.endsWith(".log") || item.name.endsWith(".txt")) { log = item.text; continue; }
      let data;
      try { data = JSON.parse(item.text); } catch (_) { continue; }

      if (item.name.includes("meta")) App.meta = data;
      else if (item.name.includes("index")) App.history = data;
      else if (item.name.includes("label")) App.labels = data;
      else if (Array.isArray(data) && data.length && data[0].kalshi) App.pairs = data;
    }
    if (!App.meta && log) App.meta = metaFromLog(log, null);
    if (!App.meta) App.meta = metaFromLog("", null);
    if (!App.pairs.length) throw new Error("no scan file among those — expected latest.json");
  });
}

/* ── derived state ─────────────────────────────────────────────────────── */

function adoptThresholds() {
  const t = (App.meta && App.meta.thresholds) || {};
  App.shipped = {
    min_confidence: t.min_confidence ?? 0.45,
    min_rules_sim: t.min_rules_sim ?? 0.35,
    max_plausible_edge: t.max_plausible_edge ?? 0.10,
    min_edge: t.min_edge ?? 0.01,
  };
  App.thresholds = { ...App.shipped };
}

/** Rows in view, in sort order, each tagged with whether the gate keeps it. */
function visibleRows() {
  const q = App.query.trim().toLowerCase();
  const t = App.thresholds;

  let rows = App.pairs.map((pair) => {
    const [kept, reason] = passesGates(pair.confidence, pair.rules_similarity, pair.edge, t);
    return { pair, kept: kept && passesEdgeFloor(pair, t), reason };
  });

  if (q) {
    rows = rows.filter(({ pair }) => {
      const hay = [pair.kalshi.question, pair.polymarket.question,
                   pair.kalshi.rules, pair.polymarket.rules,
                   pair.kalshi.id].join(" ").toLowerCase();
      return hay.includes(q);
    });
  }
  if (App.filters.has("warned")) rows = rows.filter((r) => r.pair.warnings.length > 0);
  if (App.filters.has("depth")) rows = rows.filter((r) => !r.pair.priced_at_top_of_book);
  if (App.filters.has("rules")) rows = rows.filter((r) => r.pair.rules_similarity !== null);
  if (App.filters.has("soon")) {
    rows = rows.filter((r) => r.pair.days_to_settle !== null && r.pair.days_to_settle < 30);
  }

  const key = {
    annualised: (p) => p.annualised ?? -9e9,
    edge: (p) => p.edge,
    gap: (p) => Math.abs(p.mid_gap),
    confidence: (p) => p.confidence,
  }[App.sort];
  rows.sort((a, b) => key(b.pair) - key(a.pair));
  return rows;
}
App.visibleRows = visibleRows;

/* ── header + KPIs ─────────────────────────────────────────────────────── */

function renderFreshness() {
  const stamp = App.meta && App.meta.generated_at;
  if (!stamp) return;
  const { text, hours } = relativeTime(stamp);
  $("freshness").hidden = false;
  $("fresh-rel").textContent = text;
  $("fresh-abs").textContent = stamp;
  // The schedule is daily, so a run older than ~26h means one was missed.
  $("fresh-dot").className = "dot " + (hours < 26 ? "fresh" : hours < 72 ? "aging" : "stale");
}

function kpiTile(label, value, note, tone) {
  const tile = el("div", "kpi");
  tile.append(el("div", "kpi-label", label));
  tile.append(el("div", "kpi-value" + (tone ? " " + tone : ""), value));
  tile.append(el("div", "kpi-note", note));
  return tile;
}

function renderKpis() {
  const kept = App.pairs.filter((p) => passesGates(
    p.confidence, p.rules_similarity, p.edge, App.thresholds)[0] && passesEdgeFloor(p, App.thresholds));
  const edges = kept.map((p) => p.edge);
  const anns = kept.map((p) => p.annualised).filter((v) => v !== null && v !== undefined);
  const confs = kept.map((p) => p.confidence).sort((a, b) => a - b);
  const settles = kept.map((p) => p.days_to_settle).filter((v) => v !== null && v !== undefined);
  const profit = kept.reduce((sum, p) => sum + p.edge * (p.contracts || 0), 0);
  const size = (App.meta && App.meta.thresholds && App.meta.thresholds.size) || 100;

  const strip = $("kpis");
  strip.hidden = false;
  strip.replaceChildren(
    kpiTile("Opportunities", String(kept.length),
            `of ${App.pairs.length} in the scan`),
    kpiTile("Best edge", edges.length ? pp(Math.max(...edges)) : "—",
            "pp per contract, after fees", edges.length ? "pos" : ""),
    kpiTile("Best annualised", anns.length ? pct(Math.max(...anns)) : "—",
            "compounded to settlement", anns.length ? "pos" : ""),
    kpiTile("Median confidence", confs.length ? num(confs[Math.floor(confs.length / 2)]) : "—",
            "text similarity, not proof"),
    kpiTile("Profit at size", money(profit),
            `all legs filled, ${size} contracts each`),
    kpiTile("Nearest settle", settles.length ? days(Math.min(...settles)) : "—",
            "soonest resolution in view"),
  );
  $("tab-count-board").textContent = String(kept.length);
}
App.renderKpis = renderKpis;

/* ── board ─────────────────────────────────────────────────────────────── */

function edgeCell(pair, maxEdge) {
  const cell = el("span", "edgecell");
  const bar = el("i", "edgebar");
  bar.style.width = Math.max(2, (pair.edge / (maxEdge || 1)) * 62) + "px";
  cell.append(bar, el("span", pair.edge > 0 ? "pos" : "", pp(pair.edge)));
  return cell;
}

function gapCell(pair, maxGap) {
  const wrap = document.createDocumentFragment();
  wrap.append(el("span", pair.mid_gap >= 0 ? "" : "", signedPp(pair.mid_gap)));
  const viz = el("span", "gapviz");
  const bar = el("i");
  const frac = Math.min(1, Math.abs(pair.mid_gap) / (maxGap || 1)) * 0.5;
  bar.style.width = (frac * 100) + "%";
  bar.style.background = `var(--${pair.mid_gap >= 0 ? "kalshi" : "poly"})`;
  if (pair.mid_gap >= 0) bar.style.left = "50%"; else bar.style.right = "50%";
  viz.append(bar);
  wrap.append(viz);
  return wrap;
}

function confidenceCell(pair) {
  const row = el("div", "meter-row");
  const meter = el("div", "meter");
  const fill = el("i");
  fill.style.width = (pair.confidence * 100) + "%";
  fill.style.background = pair.confidence >= 0.7 ? "var(--pos)"
                        : pair.confidence >= 0.55 ? "var(--warn)" : "var(--text-faint)";
  const tick = el("u");
  tick.style.left = (App.thresholds.min_confidence * 100) + "%";
  meter.append(fill, tick);
  row.append(meter, el("span", "val", num(pair.confidence)));
  row.title = `title ${num(pair.title_similarity)} · rules `
            + (pair.rules_similarity === null ? "n/a" : num(pair.rules_similarity))
            + ` · date ${num(pair.date_similarity)}`;
  return row;
}

/** "buy YES kalshi / NO polymarket" → two badges plus an arrow. */
function legCell(pair) {
  const wrap = el("div", "leg");
  const m = (pair.edge_leg || "").match(/buy (\w+) (\w+) \/ (\w+) (\w+)/i);
  if (!m) { wrap.append(el("span", "faint", "—")); return wrap; }
  wrap.append(el("span", "badge " + m[1].toLowerCase(), m[1].toUpperCase()));
  wrap.append(el("span", "badge " + m[2].toLowerCase(), m[2] === "kalshi" ? "K" : "P"));
  wrap.append(el("span", "faint", "/"));
  wrap.append(el("span", "badge " + m[3].toLowerCase(), m[3].toUpperCase()));
  wrap.append(el("span", "badge " + m[4].toLowerCase(), m[4] === "kalshi" ? "K" : "P"));
  wrap.title = pair.edge_leg;
  return wrap;
}

function marketsCell(pair) {
  const wrap = el("div", "qpair");
  for (const [cls, side] of [["k", pair.kalshi], ["p", pair.polymarket]]) {
    const line = el("div", "qline " + cls);
    line.append(el("span", "tick", cls === "k" ? "KAL" : "POLY"));
    const txt = el("span", "txt", side.question);
    txt.title = side.question;
    line.append(txt);
    wrap.append(line);
  }
  return wrap;
}

function flagsCell(pair) {
  const wrap = el("div", "flags");
  if (pair.warnings.length) {
    const f = el("span", "flag warn", "!" + pair.warnings.length);
    f.title = pair.warnings.join("\n");
    wrap.append(f);
  }
  if (pair.priced_at_top_of_book) {
    const f = el("span", "flag top", "TOB");
    f.title = "Priced at top of book only — no depth was fetched for this pair, "
            + "so the quoted size may not be available.";
    wrap.append(f);
  }
  if (pair.rules_similarity === null) {
    const f = el("span", "flag warn", "R?");
    f.title = "One venue published no resolution text, so the rules could not be compared.";
    wrap.append(f);
  }
  return wrap;
}

function renderBoard() {
  const rows = visibleRows();
  const body = $("board-body");
  body.replaceChildren();
  $("board-empty").hidden = rows.length > 0;

  const maxEdge = Math.max(...rows.map((r) => r.pair.edge), 0.0001);
  const maxGap = Math.max(...rows.map((r) => Math.abs(r.pair.mid_gap)), 0.0001);

  rows.forEach(({ pair, kept, reason }, index) => {
    const tr = el("tr");
    if (!kept) {
      tr.className = "is-cut";
      tr.title = "Dropped by the current thresholds: " + reason;
    }
    const cells = [
      ["num", edgeCell(pair, maxEdge)],
      ["num", el("span", (pair.annualised ?? 0) > 0 ? "pos" : "", pct(pair.annualised))],
      ["num", gapCell(pair, maxGap)],
      ["conf-col", confidenceCell(pair)],
      ["num", document.createTextNode(days(pair.days_to_settle))],
      ["", legCell(pair)],
      ["markets-col", marketsCell(pair)],
      ["flags-col", flagsCell(pair)],
    ];
    for (const [cls, content] of cells) {
      const td = el("td", cls);
      td.append(content);
      tr.append(td);
    }
    tr.addEventListener("click", () => openDrawer(pair));
    body.append(tr);
  });
}
App.renderBoard = renderBoard;

/* ── export ────────────────────────────────────────────────────────────── */

function exportCsv() {
  const columns = ["confidence", "mid_gap", "edge", "annualised", "days_to_settle", "edge_leg",
                   "cost_per_contract", "priced_at_top_of_book", "kalshi_mid", "poly_mid",
                   "kalshi_question", "poly_question", "kalshi_id", "poly_url",
                   "title_similarity", "rules_similarity", "days_apart", "warnings"];
  const cell = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
  };
  const lines = [columns.join(",")];
  for (const { pair: p } of visibleRows()) {
    lines.push([p.confidence, p.mid_gap, p.edge, p.annualised, p.days_to_settle, p.edge_leg,
                p.cost_per_contract, p.priced_at_top_of_book, p.kalshi.mid, p.polymarket.mid,
                p.kalshi.question, p.polymarket.question, p.kalshi.id, p.polymarket.url,
                p.title_similarity, p.rules_similarity, p.days_apart,
                p.warnings.join("; ")].map(cell).join(","));
  }
  download("divergence-view.csv", "﻿" + lines.join("\n"), "text/csv");
}

function download(name, text, type) {
  const url = URL.createObjectURL(new Blob([text], { type: type || "application/json" }));
  const a = el("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}
App.download = download;

/* ── view switching ────────────────────────────────────────────────────── */

const VIEWS = ["board", "pipeline", "lab", "labels", "trends"];

function showView(name) {
  if (!VIEWS.includes(name)) name = "board";
  App.view = name;
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("is-active", tab.dataset.view === name);
  }
  for (const view of document.querySelectorAll(".view")) view.classList.remove("is-active");
  $("view-" + name).classList.add("is-active");

  if (name === "board") renderBoard();
  if (name === "pipeline") App.renderPipeline();
  if (name === "lab") App.renderLab();
  if (name === "labels") App.renderLabels();
  if (name === "trends") App.renderTrends();
}
App.showView = showView;

/* ── theme ─────────────────────────────────────────────────────────────── */

/** What the page is actually showing right now, chosen or inherited. */
function effectiveTheme() {
  return document.documentElement.dataset.theme
      || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark");
}

function applyTheme(theme, remember) {
  document.documentElement.dataset.theme = theme;
  if (remember) localStorage.setItem(LS_THEME, theme);
  document.querySelector("[data-theme-icon]").textContent = theme === "dark" ? "☾" : "☀";
}

/* ── wiring ────────────────────────────────────────────────────────────── */

function wireBoardControls() {
  $("q").addEventListener("input", (e) => { App.query = e.target.value; renderBoard(); });

  $("sortseg").addEventListener("click", (e) => {
    const button = e.target.closest("button");
    if (!button) return;
    App.sort = button.dataset.sort;
    for (const b of $("sortseg").children) b.classList.toggle("is-active", b === button);
    renderBoard();
  });

  $("chips").addEventListener("click", (e) => {
    const chip = e.target.closest(".chip");
    if (!chip) return;
    const name = chip.dataset.filter;
    if (App.filters.has(name)) App.filters.delete(name); else App.filters.add(name);
    chip.classList.toggle("is-active", App.filters.has(name));
    renderBoard();
  });

  $("btn-export-csv").addEventListener("click", exportCsv);
  $("scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("drawer").hidden) closeDrawer();
  });
}

function wireLoaders() {
  const pick = $("filepick");
  const open = () => pick.click();
  $("btn-pick").addEventListener("click", open);
  $("btn-load").addEventListener("click", open);
  pick.addEventListener("change", () => {
    loadFromFiles(pick.files).then(start).catch(showLoadError);
  });

  const zone = $("dropzone");
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.add("over"); });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, (e) => { e.preventDefault(); zone.classList.remove("over"); });
  }
  zone.addEventListener("drop", (e) => {
    loadFromFiles(e.dataTransfer.files).then(start).catch(showLoadError);
  });
}

function showLoadError(err) {
  $("load-title").textContent = "Could not read the scan";
  $("load-body").textContent = String(err && err.message ? err.message : err);
  $("loadbox").hidden = false;
}

/** Everything downstream of having data. */
function start() {
  adoptThresholds();
  App.labels = App.labels.length ? App.labels : loadStoredLabels();
  $("view-loading").classList.remove("is-active");
  $("tabs").hidden = false;
  renderFreshness();
  renderKpis();
  showView(location.hash.slice(1) || "board");

  const wanted = new URLSearchParams(location.search).get("pair");
  if (wanted) {
    const pair = App.pairs.find((p) => p.kalshi.id === wanted);
    if (pair) openDrawer(pair);
  }

  const errors = (App.meta && App.meta.errors) || [];
  if (errors.length) {
    $("banner-error").hidden = false;
    $("banner-error").textContent = "Last scan reported an error: " + errors.join(" · ");
  }
}

function loadStoredLabels() {
  try { return JSON.parse(localStorage.getItem(LS_LABELS)) || []; } catch (_) { return []; }
}
App.loadStoredLabels = loadStoredLabels;
App.saveLabels = () => localStorage.setItem(LS_LABELS, JSON.stringify(App.labels));

function init() {
  const stored = localStorage.getItem(LS_THEME);
  if (stored) applyTheme(stored, false);
  document.querySelector("[data-theme-icon]").textContent =
    effectiveTheme() === "dark" ? "☾" : "☀";
  $("btn-theme").addEventListener("click", () => {
    applyTheme(effectiveTheme() === "dark" ? "light" : "dark", true);
  });
  wireBoardControls();
  wireLoaders();
  document.querySelector(".tabs").addEventListener("click", (e) => {
    const tab = e.target.closest(".tab");
    if (tab) showView(tab.dataset.view);
  });

  loadFromServer().then(start).catch((err) => {
    if (location.protocol === "file:") {
      $("load-title").textContent = "Serve the repo, or drop the files in";
      $("load-body").textContent = "";
    } else {
      showLoadError(err);
      return;
    }
    $("loadbox").hidden = false;
  });
}

document.addEventListener("DOMContentLoaded", init);
