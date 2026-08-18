/* ===========================================================================
   The Bot tab — the paper-trading loop, watched.

   Only reachable in live mode: the numbers here come from /api/bot, which is
   allocator.py's portfolio plus the bot's own state. Nothing on this page
   places a real order, holds a real position, or needs an account.

   The honest-reporting rule for this view: a simulated bankroll that only ever
   goes up is a bug in the simulation, not a strategy. The scanner reports
   *candidates*, and on the seed labels three quarters of them were different
   questions that merely read alike — so the mismatch rate is a first-class
   control here rather than a hidden assumption, and the panel says what it
   costs. See the "Why it is paper only" section of the README.
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
  const money = (v) => (v === null || v === undefined) ? "—" : "$" + Number(v).toFixed(2);
  const signed = (v) => (v === null || v === undefined) ? "—"
    : (v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(Number(v)).toFixed(2);
  const pct = (v, d = 1) => (v === null || v === undefined) ? "—" : (v * 100).toFixed(d) + "%";
  const tone = (v) => (v === null || v === undefined || Math.abs(v) < 0.005) ? ""
    : (v > 0 ? "pos" : "neg");
  const shortTime = (iso) => (iso || "").replace("T", " ").slice(0, 16);

  // key, label, min, max, step, hint. The order money flows through them.
  const KNOBS = [
    ["bankroll", "Starting bankroll", 100, 100000, 100,
     "Simulated cash. Takes effect on reset."],
    ["positions", "Positions held at once", 1, 30, 1,
     "The spread. More positions, less riding on any one being a real match."],
    ["deploy_frac", "Deployed per cycle", 0, 1, 0.05,
     "Share of equity put to work each scan; the rest stays in reserve."],
    ["max_position_frac", "Cap per position", 0.01, 1, 0.01,
     "Hard ceiling on any single pair, as a fraction of equity."],
    ["time_pref", "Time preference", 0, 3, 0.1,
     "Above 0 prejudices sooner-resolving pairs: edge ÷ days^pref. 0 sizes on edge alone."],
    ["min_edge", "Minimum edge", 0, 0.10, 0.005,
     "Locked profit per $1 after fees, below which a pair is not worth the capital."],
    ["min_confidence", "Minimum confidence", 0, 1, 0.05,
     "Text-match floor. Deliberately stricter than the board's — this is money, pretend or not."],
    ["mismatch_loss_rate", "Assumed mismatch rate", 0, 1, 0.05,
     "The share of settling positions that turn out to be two different questions, " +
     "where the hedge was really two bets and the stake is lost. The seed labels put " +
     "this near 0.75 for candidates nobody read by hand. At 0 the same eight pairs " +
     "return +11%; at 0.75, −58% to −99%. This is the single biggest number on the page."],
  ];

  /* ── header ──────────────────────────────────────────────────────────── */

  function header(snap) {
    const grid = el("div", "stat-grid");
    const start = snap.starting_bankroll || 0;
    const total = snap.equity + snap.unrealized;
    const ret = start ? (total - start) / start : null;

    grid.append(
      App.statTile("equity", money(snap.equity)),
      App.statTile("cash", money(snap.cash)),
      App.statTile("exposure", money(snap.exposure)),
      App.statTile("realized", signed(snap.realized), tone(snap.realized)),
      App.statTile("unrealized", signed(snap.unrealized), tone(snap.unrealized)),
      App.statTile("return", ret === null ? "—" : (ret > 0 ? "+" : "") + pct(ret),
                   tone(ret)),
      App.statTile("open", String(snap.open.length)),
      App.statTile("closed", String(snap.closed_total)));
    return grid;
  }

  function controls(snap) {
    const bar = el("div", "botbar");

    const state = el("span", "botstate " + (snap.running ? "on" : "off"));
    state.append(el("span", "dot " + (snap.running ? "fresh" : "stale")));
    state.append(el("span", undefined, snap.running ? "running" : "paused"));
    bar.append(state);

    bar.append(el("span", "muted",
      snap.running
        ? "Trades every scan that finishes."
        : "Scans still run; the bot just does not act on them."));

    const spacer = el("div", "toolbar-spacer");
    bar.append(spacer);

    const act = (label, action, cls, confirmText) => {
      const button = el("button", "btn " + (cls || ""), label);
      button.addEventListener("click", async () => {
        if (confirmText && !confirm(confirmText)) return;
        button.disabled = true;
        try {
          await App.post("/api/bot", { action });
          await App.reloadState();
          App.renderBot();
        } catch (err) {
          note("Could not " + label.toLowerCase() + ": " + err.message);
          button.disabled = false;
        }
      });
      return button;
    };

    bar.append(snap.running ? act("Pause", "pause", "ghost")
                            : act("Start", "start", "primary"));
    bar.append(act("Trade this scan", "allocate", "ghost"));
    bar.append(act("Settle what's due", "settle", "ghost"));
    bar.append(act("Flatten", "flatten", "ghost",
      "Close all " + snap.open.length + " open positions at cost, booking no profit or loss?"));
    bar.append(act("Reset", "reset", "danger",
      "Throw away the portfolio and its whole history, and start again at "
      + money(snap.config.bankroll) + "?"));
    return bar;
  }

  function note(text) {
    const banner = $("banner-error");
    banner.hidden = false;
    banner.textContent = text;
  }

  /* ── open positions ──────────────────────────────────────────────────── */

  function positions(snap) {
    if (!snap.open.length) {
      return App.panel("Open positions",
        el("p", "panel-note",
           snap.running
             ? "Nothing held. The next scan that turns up an eligible pair will open one."
             : "Nothing held, and the bot is paused."));
    }

    const table = el("table", "plain");
    const head = el("tr");
    for (const [label, cls] of [["Opened", ""], ["Pair", ""], ["Leg", ""],
                                ["Cost", "num"], ["Qty", "num"], ["Stake", "num"],
                                ["Mark", "num"], ["Unreal.", "num"],
                                ["Edge", "num"], ["Settles", "num"]]) {
      head.append(el("th", cls, label));
    }
    const thead = el("thead");
    thead.append(head);
    table.append(thead);

    const body = el("tbody");
    for (const pos of snap.open) {
      const row = el("tr");
      row.append(el("td", "muted", shortTime(pos.opened)));

      const pair = el("td");
      // Position side fields follow the pair's generic a/b; fall back to the
      // old kalshi/poly names so a position opened before the rename still shows.
      pair.append(el("div", undefined, pos.a_q || pos.a_id || pos.kalshi_q || pos.kalshi_id));
      pair.append(el("div", "faint", pos.b_q || pos.b_id || pos.poly_q || pos.poly_url));
      row.append(pair);

      row.append(el("td", "faint", pos.leg || "—"));
      row.append(el("td", "num", Number(pos.cost_per_contract).toFixed(3)));
      row.append(el("td", "num", String(pos.contracts)));
      row.append(el("td", "num", money(pos.stake)));
      row.append(el("td", "num" + (pos.mark === null ? " faint" : ""),
                    pos.mark === null ? "no quote" : money(pos.mark)));
      row.append(el("td", "num " + tone(pos.unrealized),
                    pos.unrealized === null ? "—" : signed(pos.unrealized)));
      row.append(el("td", "num", pos.edge === null || pos.edge === undefined
                    ? "—" : (pos.edge * 100).toFixed(2)));
      row.append(el("td", "num muted", shortTime(pos.settle_time) || "—"));
      body.append(row);
    }
    table.append(body);

    const wrap = el("div", "tablewrap");
    wrap.append(table);
    return App.panel("Open positions", wrap, el("p", "panel-note",
      "Mark is what the same hedge costs in the latest scan — what unwinding now " +
      "would return. A pair that has dropped out of the scan gets no mark rather " +
      "than a stale one."));
  }

  /* ── equity curve ────────────────────────────────────────────────────── */

  function curve(snap) {
    const log = snap.cycle_log || [];
    if (log.length < 2) {
      return App.panel("Equity",
        el("p", "panel-note",
           "Two cycles are needed before there is a line to draw. "
           + (log.length ? "One so far." : "None so far.")));
    }
    const points = log.map((c) => ({
      label: shortTime(c.at),
      value: c.equity + (c.unrealized || 0),
    }));
    return App.panel("Equity", App.lineChart(points, (v) => "$" + Number(v).toFixed(0)),
      el("p", "panel-note", "Cash plus cost basis plus the unrealized mark, one point per cycle."));
  }

  /* ── closed trades ───────────────────────────────────────────────────── */

  function tradeLog(snap) {
    if (!snap.closed.length) {
      return App.panel("Trade log", el("p", "panel-note", "Nothing has settled yet."));
    }
    const table = el("table", "plain");
    const head = el("tr");
    for (const [label, cls] of [["Settled", ""], ["Pair", ""], ["Outcome", ""],
                                ["Stake", "num"], ["Payoff", "num"], ["P&L", "num"]]) {
      head.append(el("th", cls, label));
    }
    const thead = el("thead");
    thead.append(head);
    table.append(thead);

    const body = el("tbody");
    for (const pos of snap.closed.slice().reverse()) {
      const row = el("tr");
      row.append(el("td", "muted", shortTime(pos.settled)));
      row.append(el("td", undefined, pos.a_q || pos.a_id || pos.kalshi_q || pos.kalshi_id));

      const label = { "hedged-win": "hedge held", "mismatch-loss": "mismatch",
                      "flattened": "flattened" }[pos.status] || pos.status;
      const cls = pos.status === "mismatch-loss" ? "flag warn"
                : pos.status === "hedged-win" ? "flag" : "flag";
      const cell = el("td");
      cell.append(el("span", cls, label));
      row.append(cell);

      row.append(el("td", "num", money(pos.stake)));
      row.append(el("td", "num", money(pos.payoff)));
      row.append(el("td", "num " + tone(pos.pnl), signed(pos.pnl)));
      body.append(row);
    }
    table.append(body);

    const wrap = el("div", "tablewrap");
    wrap.append(table);
    const shown = snap.closed.length < snap.closed_total
      ? `Showing the last ${snap.closed.length} of ${snap.closed_total}.` : "";
    return App.panel("Trade log", wrap, el("p", "panel-note", shown));
  }

  /* ── rules ───────────────────────────────────────────────────────────── */

  function rules(snap) {
    const cfg = snap.config;
    const box = el("div");

    for (const [key, label, min, max, step, hint] of KNOBS) {
      const slider = el("div", "slider");
      const head = el("div", "slider-head");
      const shown = key === "bankroll" ? money(cfg[key])
                  : key === "positions" ? String(cfg[key])
                  : Number(cfg[key]).toFixed(key === "min_edge" ? 3 : 2);
      const value = el("span", "slider-val"
        + (key === "mismatch_loss_rate" && cfg[key] < 0.2 ? " changed" : ""), shown);
      head.append(el("span", "slider-name", label), value);

      const input = el("input");
      input.type = "range";
      input.min = min; input.max = max; input.step = step;
      input.value = cfg[key];
      // Redraw the number as it drags, but only tell the server on release —
      // otherwise a single drag is fifty POSTs and fifty portfolio writes.
      input.addEventListener("input", () => {
        cfg[key] = Number(input.value);
        value.textContent = key === "bankroll" ? money(cfg[key])
                          : key === "positions" ? String(cfg[key])
                          : Number(cfg[key]).toFixed(key === "min_edge" ? 3 : 2);
      });
      input.addEventListener("change", () => save({ [key]: Number(input.value) }));

      slider.append(head, input, el("div", "slider-hint", hint));
      box.append(slider);
    }

    box.append(toggle("Require the rule texts to agree", "require_rules_match", cfg,
      "The one check that separates a hedge from two directional bets. Off, the bot " +
      "will trade pairs whose resolution terms were never compared."));
    box.append(toggle("Only depth-verified pairs", "skip_top_of_book_only", cfg,
      "Skip pairs whose size was estimated from top-of-book instead of a real order book."));
    box.append(toggle("Settle due positions automatically", "auto_settle", cfg,
      "Resolve positions whose date has passed at the start of each cycle."));

    return App.panel("Rules", box, el("p", "panel-note",
      "The same sizing rules `python allocator.py allocate` obeys — this panel just " +
      "sets them from the browser."));
  }

  function toggle(label, key, cfg, hint) {
    const wrap = el("label", "toggle");
    const input = el("input");
    input.type = "checkbox";
    input.checked = Boolean(cfg[key]);
    input.addEventListener("change", () => {
      cfg[key] = input.checked;
      save({ [key]: input.checked });
    });
    wrap.append(input, el("span", "toggle-label", label));
    const box = el("div", "slider");
    box.append(wrap, el("div", "slider-hint", hint));
    return box;
  }

  async function save(patch) {
    try {
      await App.post("/api/bot", { action: "config", config: patch });
      await App.reloadState();
    } catch (err) {
      note("Could not save that setting: " + err.message);
    }
  }

  /* ── last cycle ──────────────────────────────────────────────────────── */

  function lastCycle(snap) {
    const cycle = snap.last_cycle;
    if (!cycle) {
      return App.panel("Last cycle", el("p", "panel-note",
        "The bot has not run a cycle yet."));
    }
    const rows = el("div", "waterfall");
    const line = (label, value, cls) => {
      const row = el("div", "wf-row" + (cls ? " " + cls : ""));
      row.append(el("span", undefined, label), el("span", "num", value));
      rows.append(row);
    };
    line("ran", shortTime(cycle.at));
    line("positions opened", String(cycle.opened));
    line("positions settled", String(cycle.settled));
    line("equity", money(cycle.equity), "total");

    const children = [rows];
    if ((cycle.notes || []).length) {
      const list = el("ul", "notes");
      for (const text of cycle.notes) list.append(el("li", undefined, text));
      children.push(list);
    }
    return App.panel("Last cycle", ...children);
  }

  /* ── the run log ─────────────────────────────────────────────────────── */

  function runLog() {
    const lines = (App.run && App.run.log) || [];
    if (!lines.length) return null;
    const pre = el("pre", "runlog", lines.slice(-60).join("\n"));
    return App.panel("Scanner output", pre);
  }

  /* ── the view ────────────────────────────────────────────────────────── */

  App.renderBot = function renderBot() {
    const view = $("view-bot");
    view.replaceChildren();

    const snap = App.bot;
    if (!snap) {
      view.append(App.panel("Bot", el("p", "panel-note",
        "No backend. Run `python app.py` to trade the scans as they land.")));
      return;
    }

    view.append(controls(snap));
    view.append(header(snap));

    const banner = el("p", "panel-note bot-caveat");
    banner.textContent =
      "Simulated money. No account, no wallet, no order ever leaves this machine. "
      + "Settlement assumes " + pct(snap.config.mismatch_loss_rate, 0)
      + " of positions turn out to be mismatched questions"
      + (snap.config.require_rules_match ? ", and pairs whose rule texts disagree are refused."
                                         : " — and rule-text agreement is NOT being required.");
    view.append(banner);

    view.append(positions(snap));

    const grid = el("div", "grid2");
    grid.append(curve(snap), lastCycle(snap));
    view.append(grid);

    view.append(tradeLog(snap));
    view.append(rules(snap));

    const log = runLog();
    if (log) view.append(log);

    const count = $("tab-count-bot");
    if (count) count.textContent = snap.open.length ? String(snap.open.length) : "";
  };

}(window.App));
