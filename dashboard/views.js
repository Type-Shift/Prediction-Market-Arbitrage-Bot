/* ===========================================================================
   Divergence scanner dashboard — pipeline, threshold lab, labels, trends.
   Loaded after app.js; hangs its renderers off the shared App object.
   =========================================================================== */

(function (App) {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const num = (v, d = 2) => (v === null || v === undefined) ? "—" : Number(v).toFixed(d);
  const pct = (v, d = 1) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(d) + "%";
  const pp = (v, d = 2) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(d);
  const count = (v) => (v === null || v === undefined) ? "—" : Number(v).toLocaleString();

  function panel(title, ...children) {
    const p = el("div", "panel");
    p.append(el("h2", "panel-title", title));
    p.append(...children);
    return p;
  }

  function statTile(label, value, tone) {
    const s = el("div", "stat");
    s.append(el("div", "stat-label", label));
    s.append(el("div", "stat-value" + (tone ? " " + tone : ""), value));
    return s;
  }

  /* ── pipeline ────────────────────────────────────────────────────────── */

  // label, funnel key, bar class. Order is the order the pipeline runs in.
  const STAGES = [
    ["Kalshi markets scanned", "kalshi_scanned", ""],
    ["…carrying a resting quote", "kalshi_quoted", ""],
    ["Kalshi taken into matching", "kalshi_usable", ""],
    ["Polymarket taken into matching", "poly_usable", "poly"],
    ["Candidate pairs scored", "candidate_pairs", ""],
    ["…skipped by the ladder rule", "skipped_ladder", "drop"],
    ["…skipped by the score prefilter", "skipped_prefilter", "drop"],
    ["Above the confidence floor", "above_confidence", "keep"],
    ["Order books fetched", "depth_priced", "keep"],
    ["Hidden as implausible", "hidden_implausible", "drop"],
    ["Kept", "kept", "keep"],
  ];

  App.renderPipeline = function renderPipeline() {
    const view = $("view-pipeline");
    const meta = App.meta || {};
    const funnel = meta.funnel || {};
    view.replaceChildren();

    if (meta.reconstructed) {
      const note = el("div", "banner error");
      note.style.margin = "0 0 20px";
      note.style.background = "var(--warn-soft)";
      note.style.borderColor = "var(--warn)";
      note.style.color = "var(--warn)";
      note.textContent = "No results/meta.json in this scan — these counts were parsed back "
        + "out of run.log. The next scheduled run writes them as data.";
      view.append(note);
    }

    const present = STAGES.filter(([, key]) => funnel[key] !== undefined);
    if (!present.length) {
      view.append(panel("Pipeline", el("p", "empty-body", "No run telemetry was found.")));
      return;
    }

    // Log scale: the first stage is 30,000 and the last is 25. On a linear
    // axis every bar after the second is a single invisible pixel.
    const max = Math.max(...present.map(([, k]) => funnel[k]));
    const scale = (v) => (v <= 0 ? 0 : Math.log10(v + 1) / Math.log10(max + 1)) * 100;

    const rows = el("div", "funnel");
    for (const [label, key, cls] of present) {
      const row = el("div", "funnel-row");
      row.append(el("div", "funnel-label", label));
      const track = el("div", "funnel-track");
      const fill = el("div", "funnel-fill" + (cls ? " " + cls : ""));
      fill.style.width = Math.max(1.5, scale(funnel[key])) + "%";
      track.append(fill);
      row.append(track, el("div", "funnel-value", count(funnel[key])));
      rows.append(row);
    }

    const scaleNote = el("p", "panel-note",
      "Bars are on a log scale — the first stage is three orders of magnitude "
      + "larger than the last."
      + (funnel.scoring_seconds !== undefined
         ? ` Scoring took ${funnel.scoring_seconds}s.` : ""));

    const left = panel("Attrition", rows, scaleNote);

    const dropped = meta.dropped || {};
    const dropRows = Object.entries(dropped)
      .filter(([reason]) => !/DROPPED \(any reason\)/.test(reason))
      .sort((a, b) => b[1] - a[1]);
    const table = el("table", "plain");
    const thead = el("thead");
    const hr = el("tr");
    hr.append(el("th", "", "Reason a market was dropped"), el("th", "num", "Count"));
    thead.append(hr);
    table.append(thead);
    const tbody = el("tbody");
    if (!dropRows.length) {
      const tr = el("tr");
      const td = el("td", "muted", "Nothing was dropped.");
      td.colSpan = 2;
      tr.append(td);
      tbody.append(tr);
    }
    for (const [reason, n] of dropRows) {
      const tr = el("tr");
      tr.append(el("td", "", reason), el("td", "num", count(n)));
      tbody.append(tr);
    }
    table.append(tbody);

    const right = panel("Drop reasons", table, el("p", "panel-note",
      "Counts overlap on purpose: a market failing three checks is counted in all "
      + "three, which is what tells you whether one filter or several is emptying "
      + "the pipeline."));

    const settings = el("div");
    const t = meta.thresholds || {};
    const w = meta.weights || {};
    const grid = el("div", "stat-grid");
    grid.append(
      statTile("min conf", num(t.min_confidence)),
      statTile("min rules", num(t.min_rules_sim)),
      statTile("max edge", pp(t.max_plausible_edge, 0) + "pp"),
      statTile("min edge", pp(t.min_edge, 0) + "pp"),
      statTile("size", count(t.size)),
      statTile("depth", count(t.depth)),
      statTile("w title", num(w.title)),
      statTile("w rules", num(w.rules)),
      statTile("w date", num(w.date)),
    );
    settings.append(grid);

    const two = el("div", "grid2");
    two.append(left, right);
    view.append(two, panel("Settings this run used", settings));
  };

  /* ── threshold lab ───────────────────────────────────────────────────── */

  const KNOBS = [
    ["min_confidence", "Confidence floor", 0, 1, 0.01,
     "Text-similarity score a pair must reach before a human is asked to read it."],
    ["min_rules_sim", "Rules-agreement floor", 0, 1, 0.01,
     "Applied only when both venues published rules. An absent text is never held to it."],
    ["max_plausible_edge", "Implausible-edge cap", 0.01, 1, 0.01,
     "Edges above this are treated as evidence of a mismatch, not an opportunity."],
    ["min_edge", "Minimum edge", 0, 0.2, 0.005,
     "Pairs below this on both edge and mid gap are not worth the screen space."],
  ];

  function precisionRecall() {
    const byKey = new Map();
    for (const pair of App.pairs) byKey.set(App.labelKey(pair), pair);

    let tp = 0, fp = 0, tn = 0, fn = 0;
    for (const row of App.labels) {
      const pair = byKey.get(row.key);
      // Labels may refer to pairs from an older scan. Fall back to the values
      // stored on the label itself, which is what `score` does.
      const confidence = pair ? pair.confidence : row.confidence;
      const rulesSim = pair ? pair.rules_similarity
                            : (row.rules_sim === undefined ? null : row.rules_sim);
      const edge = pair ? pair.edge : (row.edge || 0);
      if (confidence === undefined || confidence === null) continue;

      const [kept] = App.passesGates(confidence, rulesSim, edge, App.thresholds);
      const isMatch = row.label === "match";
      if (kept && isMatch) tp++;
      else if (kept && !isMatch) fp++;
      else if (!kept && isMatch) fn++;
      else tn++;
    }
    return {
      tp, fp, tn, fn,
      precision: tp + fp ? tp / (tp + fp) : null,
      recall: tp + fn ? tp / (tp + fn) : null,
    };
  }

  App.renderLab = function renderLab() {
    const view = $("view-lab");
    view.replaceChildren();

    const controls = el("div");
    for (const [key, name, min, max, step, hint] of KNOBS) {
      const box = el("div", "slider");
      const head = el("div", "slider-head");
      const changed = Math.abs(App.thresholds[key] - App.shipped[key]) > 1e-9;
      const value = el("span", "slider-val" + (changed ? " changed" : ""), num(App.thresholds[key], 3));
      head.append(el("span", "slider-name", name), value);

      const input = el("input");
      input.type = "range";
      input.min = min; input.max = max; input.step = step;
      input.value = App.thresholds[key];
      input.addEventListener("input", () => {
        App.thresholds[key] = Number(input.value);
        App.renderKpis();
        App.renderBoard();
        App.renderLab();
      });

      box.append(head, input, el("div", "slider-hint", hint));
      controls.append(box);
    }

    const reset = el("button", "btn", "Reset to the values this scan ran with");
    reset.addEventListener("click", () => {
      App.thresholds = { ...App.shipped };
      App.renderKpis();
      App.renderBoard();
      App.renderLab();
    });
    controls.append(reset);

    const t = App.thresholds;
    let kept = 0, cut = 0;
    const reasons = new Map();
    for (const pair of App.pairs) {
      const [ok, why] = App.passesGates(pair.confidence, pair.rules_similarity, pair.edge, t);
      const survives = ok && App.passesEdgeFloor(pair, t);
      if (survives) { kept++; continue; }
      cut++;
      const label = ok ? "below the edge floor" : why;
      reasons.set(label, (reasons.get(label) || 0) + 1);
    }

    const impact = el("div");
    const grid = el("div", "stat-grid");
    grid.append(statTile("kept", String(kept), "pos"),
                statTile("dropped", String(cut), cut ? "neg" : ""));
    impact.append(grid);
    if (reasons.size) {
      const table = el("table", "plain");
      table.style.marginTop = "14px";
      const tbody = el("tbody");
      for (const [reason, n] of [...reasons].sort((a, b) => b[1] - a[1])) {
        const tr = el("tr");
        tr.append(el("td", "", reason), el("td", "num", String(n)));
        tbody.append(tr);
      }
      table.append(tbody);
      impact.append(table);
    }
    impact.append(el("p", "panel-note",
      "The board updates live, and pairs these thresholds would drop stay on it, struck "
      + "through — so you can see what a stricter setting costs you, not just what it keeps."));

    const scoreBox = el("div");
    if (!App.labels.length) {
      scoreBox.append(el("p", "empty-body",
        "No labels loaded. Label some pairs — or import an existing labels.json — and "
        + "precision and recall appear here, computed with the same gate the scanner uses."));
    } else {
      const s = precisionRecall();
      const grid2 = el("div", "stat-grid");
      grid2.append(
        statTile("precision", s.precision === null ? "—" : pct(s.precision), "pos"),
        statTile("recall", s.recall === null ? "—" : pct(s.recall), "pos"),
        statTile("tp", String(s.tp)), statTile("fp", String(s.fp), s.fp ? "neg" : ""),
        statTile("fn", String(s.fn), s.fn ? "neg" : ""), statTile("tn", String(s.tn)),
      );
      scoreBox.append(grid2, el("p", "panel-note",
        `Across ${App.labels.length} hand labels. False negatives are real matches these `
        + "thresholds would throw away; false positives are mismatches still getting through."));
    }

    const two = el("div", "grid2");
    two.append(panel("Thresholds", controls), panel("What this keeps", impact));
    view.append(two, panel("Scored against your labels", scoreBox));
  };

  /* ── labels ──────────────────────────────────────────────────────────── */

  let cursor = 0;

  function labelled() {
    return new Set(App.labels.map((row) => row.key));
  }

  function unlabelledPairs() {
    const done = labelled();
    return App.pairs.filter((pair) => !done.has(App.labelKey(pair)));
  }

  function recordLabel(pair, label, note) {
    App.labels.push({
      key: App.labelKey(pair),
      a_venue: pair.a.venue, a_id: pair.a.id, a_question: pair.a.question,
      b_venue: pair.b.venue, b_id: pair.b.id, b_question: pair.b.question,
      confidence: pair.confidence,
      rules_sim: pair.rules_similarity,
      edge: pair.edge,
      label,
      note: note || "",
    });
    App.saveLabels();
    App.renderLabels();
  }

  function reviewCard(pair) {
    const card = el("div", "review-card");
    const queue = unlabelledPairs();

    const head = el("div", "review-head");
    head.append(el("div", "panel-title", "Do these two contracts resolve on the same event?"));
    head.append(el("div", "review-progress",
      `${App.labels.length} labelled · ${queue.length} left`));
    card.append(head);

    for (const side of [pair.a, pair.b]) {
      const box = el("div", "side venue-" + side.venue);
      const top = el("div", "side-head");
      top.append(el("span", "badge venue-" + side.venue, side.venue.toUpperCase()));
      const link = el("a", "", "open ↗");
      link.href = side.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.style.fontSize = "11px";
      top.append(link);
      box.append(top, el("div", "side-q", side.question));
      const rules = el("div", "rules-pane", side.rules || "No resolution text published.");
      rules.style.marginTop = "8px";
      box.append(rules);
      card.append(box);
    }

    const facts = el("div", "stat-grid");
    facts.append(statTile("confidence", num(pair.confidence)),
                 statTile("rules sim", pair.rules_similarity === null ? "n/a" : num(pair.rules_similarity)),
                 statTile("edge", pp(pair.edge) + "pp"),
                 statTile("dates apart", pair.days_apart === null ? "—" : num(pair.days_apart, 1) + "d"));
    card.append(facts);

    const note = el("input", "review-note");
    note.placeholder = "Why? e.g. primary nominee vs general election";
    card.append(note);

    const actions = el("div", "review-actions");
    const match = el("button", "btn primary");
    match.append(document.createTextNode("Same event"), el("span", "kbd", "M"));
    match.addEventListener("click", () => recordLabel(pair, "match", note.value));

    const mismatch = el("button", "btn danger");
    mismatch.append(document.createTextNode("Different events"), el("span", "kbd", "X"));
    mismatch.addEventListener("click", () => recordLabel(pair, "mismatch", note.value));

    const skip = el("button", "btn ghost");
    skip.append(document.createTextNode("Skip"), el("span", "kbd", "S"));
    skip.addEventListener("click", () => { cursor++; App.renderLabels(); });

    actions.append(match, mismatch, skip);
    card.append(actions);
    return card;
  }

  function labelToolbar() {
    const bar = el("div", "review-actions");
    bar.style.marginTop = "0";

    const importBtn = el("button", "btn ghost", "Import labels.json…");
    const picker = el("input");
    picker.type = "file";
    picker.accept = ".json";
    picker.hidden = true;
    picker.addEventListener("change", () => {
      const file = picker.files[0];
      if (!file) return;
      const reader = new FileReader();
      reader.onload = () => {
        let rows;
        try { rows = JSON.parse(reader.result); } catch (_) { return; }
        if (!Array.isArray(rows)) return;
        const seen = labelled();
        for (const row of rows) {
          if (!seen.has(row.key)) App.labels.push(row);
        }
        App.saveLabels();
        App.renderLabels();
      };
      reader.readAsText(file);
    });
    importBtn.addEventListener("click", () => picker.click());

    const exportBtn = el("button", "btn", "Export labels.json");
    exportBtn.addEventListener("click", () => {
      App.download("labels.json", JSON.stringify(App.labels, null, 2));
    });

    const clearBtn = el("button", "btn ghost", "Clear");
    clearBtn.addEventListener("click", () => {
      if (!confirm("Discard every label stored in this browser?")) return;
      App.labels = [];
      App.saveLabels();
      App.renderLabels();
    });

    bar.append(importBtn, picker, exportBtn, clearBtn);
    return bar;
  }

  App.renderLabels = function renderLabels() {
    const view = $("view-labels");
    view.replaceChildren();
    $("tab-count-labels").textContent = App.labels.length ? String(App.labels.length) : "";

    const queue = unlabelledPairs();
    if (cursor >= queue.length) cursor = 0;

    const body = el("div");
    if (!queue.length) {
      body.append(el("p", "empty-body",
        App.pairs.length
          ? "Every pair in this scan is labelled. Export the file and commit it, then "
          + "`python scanner.py score` measures the thresholds against it."
          : "No pairs loaded."));
    } else {
      body.append(reviewCard(queue[cursor]));
    }

    const counts = App.labels.reduce((acc, row) => {
      acc[row.label] = (acc[row.label] || 0) + 1;
      return acc;
    }, {});
    const summary = el("div", "stat-grid");
    summary.append(statTile("labelled", String(App.labels.length)),
                   statTile("match", String(counts.match || 0), "pos"),
                   statTile("mismatch", String(counts.mismatch || 0), "neg"),
                   statTile("unlabelled", String(queue.length)));

    view.append(panel("Review", body),
                panel("Label store", summary, labelToolbar(), el("p", "panel-note",
                  "Labels live in this browser's local storage — there is no server to write "
                  + "to. Export the file into the repo as labels.json and the score and sweep "
                  + "commands pick it up unchanged.")));
  };

  // The CLI's keystrokes, kept.
  document.addEventListener("keydown", (event) => {
    if (App.view !== "labels") return;
    if (event.target.matches("input, textarea")) return;
    const queue = unlabelledPairs();
    if (!queue.length) return;
    const pair = queue[cursor % queue.length];
    if (event.key === "m") recordLabel(pair, "match", "");
    else if (event.key === "x") recordLabel(pair, "mismatch", "");
    else if (event.key === "s") { cursor++; App.renderLabels(); }
  });

  /* ── trends ──────────────────────────────────────────────────────────── */

  function lineChart(points, format) {
    const W = 520, H = 130, padL = 40, padB = 18, padT = 8;
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", "chart");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("preserveAspectRatio", "none");

    const make = (tag, attrs) => {
      const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
      return node;
    };

    const values = points.map((p) => p.value).filter((v) => v !== null && v !== undefined);
    if (!values.length) return svg;
    const lo = Math.min(...values), hi = Math.max(...values);
    // A series that never moved has no range to scale against. Draw it down
    // the middle with one label, rather than pinned to the top with two
    // identical numbers stacked on each other.
    const flat = hi === lo;
    const midY = padT + (H - padT - padB) / 2;
    const x = (i) => padL + (points.length === 1 ? (W - padL) / 2
                     : (i / (points.length - 1)) * (W - padL - 6));
    const y = (v) => flat ? midY : padT + (1 - (v - lo) / (hi - lo)) * (H - padT - padB);

    svg.append(make("line", { class: "axis", x1: padL, y1: H - padB, x2: W, y2: H - padB }));

    const path = points.map((p, i) => `${i ? "L" : "M"}${x(i)},${y(p.value)}`).join(" ");
    svg.append(make("path", {
      class: "area",
      d: `${path} L${x(points.length - 1)},${H - padB} L${x(0)},${H - padB} Z`,
    }));
    svg.append(make("path", { class: "line", d: path }));

    points.forEach((p, i) => {
      const dot = make("circle", { class: "dot", cx: x(i), cy: y(p.value), r: 2.5 });
      const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
      title.textContent = `${p.label}: ${format(p.value)}`;
      dot.append(title);
      svg.append(dot);
    });

    // Two bounds unless they would print the same string.
    const bounds = flat || format(hi) === format(lo)
      ? [[hi, "middle"]] : [[hi, "hanging"], [lo, "auto"]];
    for (const [v, baseline] of bounds) {
      const text = make("text", { x: padL - 6, y: y(v), "text-anchor": "end",
                                  "dominant-baseline": baseline });
      text.textContent = format(v);
      svg.append(text);
    }
    const first = make("text", { x: padL, y: H - 5 });
    first.textContent = points[0].label;
    const last = make("text", { x: W, y: H - 5, "text-anchor": "end" });
    last.textContent = points[points.length - 1].label;
    svg.append(first, last);
    return svg;
  }

  // The Bot tab draws its equity curve with this too — one chart, one set of
  // edge cases (flat series, single point) already worked out.
  App.lineChart = lineChart;
  App.panel = panel;
  App.statTile = statTile;

  const SERIES = [
    ["Opportunities kept", "kept", (v) => String(Math.round(v))],
    ["Best edge (pp)", "best_edge", (v) => pp(v)],
    ["Best annualised", "best_annualised", (v) => pct(v, 0)],
    ["Median confidence", "median_confidence", (v) => num(v)],
    ["Profit at size", "profit_at_size", (v) => "$" + Number(v).toFixed(0)],
  ];

  App.renderTrends = function renderTrends() {
    const view = $("view-trends");
    view.replaceChildren();

    const history = App.history || [];
    if (history.length < 2) {
      view.append(panel("Trends", el("p", "empty-body",
        history.length === 1
          ? "One snapshot so far. A second scheduled run gives these charts a line to draw."
          : "No history yet. Every scan appends a dated snapshot to results/history/, so "
          + "these charts fill in from the next scheduled run onward.")));
      return;
    }

    const grid = el("div", "grid2");
    for (const [title, key, format] of SERIES) {
      const points = history
        .filter((row) => row[key] !== null && row[key] !== undefined)
        .map((row) => ({ label: (row.generated_at || "").slice(5, 10), value: row[key] }));
      if (points.length < 2) continue;
      const latest = points[points.length - 1].value;
      const previous = points[points.length - 2].value;
      const delta = latest - previous;
      const head = el("div", "kpi-note");
      head.textContent = `now ${format(latest)} · ${delta >= 0 ? "+" : ""}${format(delta)} vs previous`;
      grid.append(panel(title, lineChart(points, format), head));
    }
    view.append(grid);
  };

}(window.App));
