/* ===========================================================================
   Divergence scanner dashboard — the pair detail drawer.

   Loaded after app.js and uses its helpers ($, el, num, pp, pct, money, days)
   from the shared script scope. This is the view the terminal report cannot
   give you: why the two markets were matched, and what the trade actually
   costs after both venues take their cut.
   =========================================================================== */

/* ── drawer ────────────────────────────────────────────────────────────── */

const WARNING_GLOSS = [
  [/^years differ/, "The two contracts name different years. Almost always a different event."],
  [/names a year the other does not/, "Only one side is dated. The undated one may cover a different window."],
  [/numeric threshold, the other does not/, "One side is a threshold contract and the other is not — check they ask the same thing."],
  [/^thresholds differ/, "Both sides quote a number and the numbers do not match."],
  [/^secondary figures differ/, "Minor figures in the two texts disagree; usually harmless, sometimes not."],
  [/direction disagrees/, "One contract pays above a level and the other below it. Not the same bet."],
  [/polarity flip/, "One side may be the complement of the other, so YES is not comparable to YES. No edge was computed."],
  [/^settlement dates/, "The venues settle on materially different dates, so this is not a clean hedge."],
  [/resolution text missing/, "One venue published no rules, so the criteria could not be compared at all."],
  [/exceeds any plausible/, "An edge this large is far more likely a mismatch than a real dislocation."],
];

const glossFor = (text) => (WARNING_GLOSS.find(([re]) => re.test(text)) || [null, ""])[1];

function sideBlock(side, cls, pair) {
  const box = el("div", "side " + cls);
  const head = el("div", "side-head");
  head.append(el("span", "badge " + side.venue, side.venue.toUpperCase()));
  if (side.category) head.append(el("span", "badge plain", side.category));
  box.append(head);
  box.append(el("div", "side-q", side.question));

  const meta = el("div", "side-meta");
  meta.innerHTML =
    `bid <b>${num(side.yes_bid, 3)}</b> · ask <b>${num(side.yes_ask, 3)}</b> · `
    + `mid <b>${num(side.mid, 3)}</b> · vol <b>${Math.round(side.volume).toLocaleString()}</b>`
    + (side.settle_time ? ` · settles <b>${side.settle_time.slice(0, 10)}</b>` : "");
  box.append(meta);
  return box;
}

/** Stacked bar of the weighted score, with the penalty as a red overhang. */
function decomposition(pair) {
  const w = (App.meta && App.meta.weights) || { title: 0.55, rules: 0.28, date: 0.17 };
  const parts = [
    ["title", w.title * pair.title_similarity, `title ${num(pair.title_similarity)} × ${w.title}`],
    ["rules", w.rules * (pair.rules_similarity ?? 0),
      pair.rules_similarity === null ? "rules not compared" : `rules ${num(pair.rules_similarity)} × ${w.rules}`],
    ["date", w.date * pair.date_similarity, `date ${num(pair.date_similarity)} × ${w.date}`],
  ];
  const base = parts.reduce((sum, [, v]) => sum + v, 0);
  const penalty = Math.max(0, base - pair.confidence);

  const bar = el("div", "decomp");
  for (const [cls, value, label] of parts) {
    const seg = el("i", cls);
    seg.style.width = (value * 100) + "%";
    seg.title = label;
    bar.append(seg);
  }
  if (penalty > 0.001) {
    const seg = el("i", "pen");
    seg.style.width = (penalty * 100) + "%";
    seg.title = `penalty −${num(penalty)}`;
    bar.append(seg);
  }

  const wrap = el("div");
  wrap.append(bar);
  const legend = el("div", "legend");
  const entries = [
    ["var(--kalshi)", `title ${num(parts[0][1])}`],
    ["var(--poly)", pair.rules_similarity === null ? "rules not compared" : `rules ${num(parts[1][1])}`],
    ["var(--pos)", `date ${num(parts[2][1])}`],
  ];
  if (penalty > 0.001) entries.push(["var(--neg)", `penalty −${num(penalty)}`]);
  for (const [color, text] of entries) {
    const item = el("span");
    const sw = el("span", "swatch");
    sw.style.background = color;
    item.append(sw, document.createTextNode(text));
    legend.append(item);
  }
  wrap.append(legend);
  return wrap;
}

/** Both venues' quoted spreads on one shared 0–1 probability axis. */
function probabilityAxis(pair) {
  const wrap = el("div", "axisline");
  wrap.append(el("div", "rail"));
  for (const [cls, side] of [["k", pair.kalshi], ["p", pair.polymarket]]) {
    if (side.yes_bid === null || side.yes_ask === null) continue;
    const span = el("div", "span " + cls);
    span.style.left = (side.yes_bid * 100) + "%";
    span.style.width = Math.max(0.7, (side.yes_ask - side.yes_bid) * 100) + "%";
    span.style.top = cls === "k" ? "6px" : "18px";
    span.title = `${side.venue}: ${num(side.yes_bid, 3)} – ${num(side.yes_ask, 3)}`;
    wrap.append(span);
  }
  for (const t of [0, 0.25, 0.5, 0.75, 1]) {
    const mark = el("div", "tickmark", (t * 100) + "%");
    mark.style.left = (t * 100) + "%";
    wrap.append(mark);
  }
  return wrap;
}

function economics(pair) {
  const size = pair.contracts || 100;
  const wf = el("div", "waterfall");
  const row = (label, value, cls) => {
    const r = el("div", "wf-row" + (cls ? " " + cls : ""));
    r.append(el("span", "", label), el("span", "", value));
    wf.append(r);
  };
  // Four decimals here on purpose: the whole trade lives in the last two of
  // them, and $0.99 vs $1.00 would round the edge away entirely.
  const cents = (v) => v === null || v === undefined ? "—" : "$" + Number(v).toFixed(4);
  row("Both legs, after both venues' fees", cents(pair.cost_per_contract) + " / contract");
  row("Locked in at settlement", "$1.0000 / contract");
  row("Edge", pp(pair.edge) + " pp", "total");
  row(`Profit on ${size} contracts`, money(pair.edge * size));
  row(`Capital at risk`, money((pair.cost_per_contract || 0) * size));
  row(`Annualised over ${days(pair.days_to_settle)}`, pct(pair.annualised));

  const note = el("p", "panel-note");
  note.textContent = "edge = 1 − cost. annualised = (1 / cost) ^ (365 / days) − 1. "
    + "Kalshi charges ceil(0.07 · n · P · (1−P)) rounded up to the cent per order; "
    + "Polymarket's coefficient varies by category.";
  const wrap = el("div");
  wrap.append(wf, note);
  return wrap;
}

/** Highlight the words the two rule texts share — where they agree is visible. */
function rulesPane(text, otherText) {
  const stop = new Set(["this", "that", "will", "market", "resolve", "resolves", "the", "and",
                        "for", "with", "then", "otherwise", "which", "have", "been", "from"]);
  const other = new Set((otherText || "").toLowerCase().match(/[a-z0-9]{4,}/g) || []);
  const pane = el("div", "rules-pane");
  if (!text) {
    pane.append(el("em", "faint", "This venue published no resolution text."));
    return pane;
  }
  for (const token of text.split(/(\s+)/)) {
    const bare = token.toLowerCase().replace(/[^a-z0-9]/g, "");
    if (bare.length >= 4 && !stop.has(bare) && other.has(bare)) {
      pane.append(el("mark", "", token));
    } else {
      pane.append(document.createTextNode(token));
    }
  }
  return pane;
}

function drawerSection(title, ...children) {
  const s = el("section");
  s.append(el("h3", "", title));
  s.append(...children);
  return s;
}

function openDrawer(pair) {
  const drawer = $("drawer");
  drawer.replaceChildren();

  const top = el("div", "drawer-top");
  const heading = el("div");
  heading.append(el("h2", "", `${pp(pair.edge)} pp edge · ${pct(pair.annualised)} annualised`));
  heading.append(el("div", "muted", pair.edge_leg || "no tradeable leg found"));
  const close = el("button", "drawer-close", "✕");
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", closeDrawer);
  top.append(heading, close);
  drawer.append(top);

  const links = el("div", "linkrow");
  for (const side of [pair.kalshi, pair.polymarket]) {
    const a = el("a", "", `${side.venue} ↗`);
    a.href = side.url;
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    links.append(a);
  }
  if (pair.days_apart !== null && pair.days_apart !== undefined) {
    links.append(el("span", "flag", `settle dates ${num(pair.days_apart, 1)}d apart`));
  }

  drawer.append(drawerSection("Head to head",
    sideBlock(pair.kalshi, "k", pair), sideBlock(pair.polymarket, "p", pair), links));

  drawer.append(drawerSection(`Confidence ${num(pair.confidence)}`, decomposition(pair)));

  const bookNote = el("p", "panel-note");
  bookNote.textContent = pair.priced_at_top_of_book
    ? "Priced at top of book only — depth was not fetched for this pair, so the size shown may not be available."
    : `Depth walked: ${pair.kalshi.yes_depth ?? "—"} contracts on Kalshi, `
      + `${pair.polymarket.yes_depth ?? "—"} on Polymarket.`;
  drawer.append(drawerSection("Quoted probability", probabilityAxis(pair), bookNote));

  drawer.append(drawerSection(`Economics at ${pair.contracts || 100} contracts`, economics(pair)));

  if (pair.warnings.length) {
    const list = el("ul", "warnlist");
    for (const w of pair.warnings) {
      const li = el("li");
      li.append(el("span", "warn", w));
      const gloss = glossFor(w);
      if (gloss) li.append(el("span", "gloss", gloss));
      list.append(li);
    }
    drawer.append(drawerSection(`Warnings (${pair.warnings.length})`, list));
  }

  const rules = el("div", "grid2");
  const left = el("div");
  left.append(el("div", "muted", "Kalshi"), rulesPane(pair.kalshi.rules, pair.polymarket.rules));
  const right = el("div");
  right.append(el("div", "muted", "Polymarket"), rulesPane(pair.polymarket.rules, pair.kalshi.rules));
  rules.append(left, right);
  drawer.append(drawerSection("Resolution criteria", rules,
    el("p", "caveat",
      "Confidence is a text-similarity estimate. It is not evidence that the two contracts "
      + "resolve on the same criteria, and a mismatch there turns a hedge into two directional "
      + "bets. Read both rule sets in full before trading.")));

  drawer.hidden = false;
  $("scrim").hidden = false;
  drawer.scrollTop = 0;
  // Deep-linkable: ?pair=<kalshi id> reopens this drawer, so a pair worth a
  // second opinion can be sent to someone as a URL.
  history.replaceState(null, "", "?pair=" + encodeURIComponent(pair.kalshi.id) + location.hash);
}

function closeDrawer() {
  $("drawer").hidden = true;
  $("scrim").hidden = true;
  history.replaceState(null, "", location.pathname + location.hash);
}
