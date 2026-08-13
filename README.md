# Kalshi / Polymarket divergence scanner

Finds markets that ask the same question on both venues and quote different
odds, then prices the difference against real order-book depth and both venues'
fee schedules — and paper-trades the result so you can see what the strategy
would actually have done.

Python 3.10+, standard library only. No install, no dependencies, no build step.

## Start here

Double-click **`run.bat`**. It finds Python, starts the server, and opens the
dashboard in your browser. Leave the window open — it *is* the server, and the
scanner's progress prints there as it runs.

The same thing from a terminal, if you prefer one:

```
python app.py                      # then open http://localhost:8000
python app.py --offline fixtures   # no network, for a demo or a look around
```

One process serves the whole loop: press **Run scan**, watch the scanner work
line by line, and see the board, the funnel and the bot update when it lands.
No reload, no second terminal, nothing to install.

## What you are looking at

Six tabs over the last scan:

- **Opportunities** — the pairs as a sortable, filterable board. Click a row for
  the detail drawer: the confidence decomposition as a stacked bar, both venues'
  quotes on one probability axis, the fee-to-net economics at your size, and both
  rule texts side by side with their shared terms highlighted. Any row is
  linkable: `?pair=<kalshi id>` reopens its drawer.
- **Pipeline** — the attrition funnel, on a log scale because the first stage is
  30,000 markets and the last is 25.
- **Threshold lab** — drag `min_confidence`, `min_rules_sim`, `max_plausible_edge`
  and `min_edge` and watch the kept set change. Pairs the new thresholds would
  drop stay on the board struck through, so a stricter setting shows you its cost
  and not just its benefit. With labels loaded it reports live precision and recall.
- **Labels** — the `review` keystroke loop in a browser, `m` / `x` / `s` and all.
  With the server running these go straight into `labels.json`, the file `score`
  and `sweep` already read.
- **Trends** — how the kept count, best edge and median confidence move run to
  run, from the dated snapshots in `results/history/`.
- **Bot** — the paper-trading loop. Equity, open positions marked against the
  newest scan, the trade log, and the sizing rules as sliders.

## How a match is decided

Text similarity alone marries markets that are not the same bet, so the score
combines signals and then subtracts structural penalties:

| Signal | Weight | What it measures |
| --- | --- | --- |
| Question text | 0.55 | TF-IDF cosine plus a character ratio over the titles |
| Resolution rules | 0.28 | Token-vector cosine, Kalshi rules vs Polymarket description |
| Settlement date | 0.17 | Kalshi `expiration_time` vs Polymarket `endDate` |

Penalties, each earned by a real false positive from live runs:

- **Thresholds differ** (−0.40): "S&P above 7000" vs "above 7500". The largest
  magnitude on each side is compared with 1% tolerance.
- **Years differ** (−0.45): compared exactly, never with tolerance — 2026 and
  2028 are 0.1% apart numerically and are different questions.
- **Direction disagrees** (−0.35): above vs below.
- **Polarity flip** (−0.15): "enter a recession" vs "avoid a recession" are
  complements, so YES on one is NO on the other. When this fires the scanner
  refuses to quote an edge at all.
- **Ladder mismatch** (hard skip): Kalshi splits "who will X" into one contract
  per outcome. A member's distinguishing tokens (the ones its siblings lack)
  must appear on the Polymarket side, or the pair is dropped — this is what
  stops "Will Letitia James be arrested" pairing with "Obama arrested".
- **Implausible edge** (hidden by default): an edge above `max_plausible_edge`
  (10pp) is treated as evidence the questions differ, not as free money. On the
  first live run, all 25 top "opportunities" sat between 25 and 87pp, and every
  single one was a mismatch.

Polymarket books whose outcomes are not literally Yes/No (e.g. Trump/Newsom) are
refused rather than guessed at — assuming outcome[0] is YES silently inverts the
contract, the most expensive mistake this program can make.

## How the edge is priced

Three numbers per pair:

- **Mid gap** — raw difference in implied probability. The "difference in odds"
  signal; ignores spreads, not tradeable as-is.
- **Edge** — what buying YES on one venue and NO on the other actually locks in
  at your requested `size`, walking both order books level by level and paying
  both venues' fees. Kalshi's taker fee is `0.07 · P·(1−P)` per contract, rounded
  **up** to the next cent per order; Polymarket's varies by category (0%
  geopolitics … 7% crypto) under the March 2026 V2 schedule.
- **Annualised** — that edge compounded over the days until the later leg
  settles, since capital is fully collateralised on both legs until then.

Pairs are priced twice: once on top-of-book to build a shortlist, then again
after real depth arrives for the top `depth` pairs. Kalshi publishes resting bids
on both sides, so its book is inverted into asks (a NO bid at 55c is a YES ask at
45c). On Polymarket, YES and NO are separate tokens with independent books — the
complement of the YES bid is only a placeholder until the real NO book is fetched.

## Paper trading

`allocator.py` spreads a **simulated** bankroll across the pairs the scanner
flags. It places no real orders and needs no account, wallet, or API key — every
fill is booked against `paper.json`. `bot.py` puts that on a loop, so every scan
that finishes gets the same treatment you would give it by hand: settle whatever
came due, plan an allocation, book it.

Three things the sizing does:

- **Prejudice sooner-resolving markets.** Each pair scores `edge / days**time_pref`
  — profit per dollar per day of waiting. A 1% edge settling in a week beats a 3%
  edge settling in a year. Raise `time_pref` to lean harder on soon; set it to 0
  to size on edge alone.
- **Spread evenly, so no one pair can hurt much.** `equal` weighting splits the
  deployable budget across the top N pairs, and `max_position_frac` (default 15%)
  is a hard ceiling on any single one. `capped` sizes by score but still obeys
  the ceiling.
- **Never bet the whole roll at once.** `deploy_frac` (default 0.5) is the share
  of equity put to work per cycle; the rest stays in reserve.

The Bot tab adds what a thing that runs on its own needs and a one-shot command
does not:

- **Marked to market.** The allocator values open positions at cost, which is
  right for sizing and useless for watching — every row would read zero until it
  settled. Each position is re-priced against the newest scan, so *unrealized*
  P&L is a real number and stays labelled as unrealized. A pair that has dropped
  out of the scan shows "no quote" rather than a stale mark.
- **An equity curve**, one point per cycle.
- **Flatten**, which closes everything at cost and books nothing — distinct from
  **settle**, which resolves what is due and pays out or wipes out.

State lives in `paper.json` (the portfolio) and `bot.json` (on/off, settings,
cycle log). Both are gitignored and written atomically, since the server writes
them while it is also serving pages.

### Why it is paper only, and why you should care

The scanner finds **candidates, not confirmed arbitrages** — it says exactly that
on every line of its output. A cross-venue "arb" is only risk-free if the two
markets resolve on identical terms; when they don't, your hedge is really two
directional bets and you can lose both. On the 24 seed labels, **18 were
mismatches** — different questions that merely read alike.

Settlement lets you see what that costs. It resolves due positions either
optimistically (every hedge held) or at a mismatch rate you choose. Eight pairs,
a healthy 3pp edge each, full bankroll, evenly spread and capped at 15%:

| assumed mismatch rate | realized return |
| --- | --- |
| 0% (every candidate truly identical) | **+11%** |
| 75% (the seed-label base rate, unverified) | **−58% to −99%** |

That gap is the reason a human must read both rule sets before real money moves —
which is the one step an auto-trader skips.

**So the bot's defaults are the pessimistic ones, on purpose.** Rule texts must
have been compared and must agree (`require_rules_match`), and the assumed
mismatch rate starts at **0.75**, not at zero. A simulation that assumes every
hedge holds will happily report +11% while the same trades lose most of the
bankroll at the rate this repo actually measured. Both are sliders in the tab.
Move them knowingly.

### How often should the scan run?

Often enough to catch a dislocation, not so often it's noise. These gaps come
from news and liquidity shifts and persist for **hours, not seconds** — and the
real bottleneck isn't scan speed, it's a human checking each candidate's rules,
which happens on human time. Sub-minute polling buys nothing and just burns API
limits and Actions minutes.

A sensible rhythm: scan **every 15–30 minutes** during waking hours. To change
the cadence for the scheduled runs, edit the `cron` line in
`.github/workflows/scan.yml` (e.g. `*/30 * * * *` for every half hour).

## Tuning with your own labels

The Labels tab is the fast way; the command line does the same thing:

```
python scanner.py review     # label the last scan's pairs by hand
python scanner.py score      # precision/recall of the current thresholds
python scanner.py sweep      # find better thresholds from your labels
```

24 labelled pairs from live runs ship as seeds — 18 of them mismatches, which is
the honest base rate. Labels accumulate in `labels.json`.

The gate logic (`passes_gates`) is deliberately one function shared by the
scanner and the evaluation harness, so the thresholds you measure are the
thresholds you run. It exists twice in the whole project — mirrored into
`dashboard/app.js` so the Threshold lab works with no server behind it. It is a
dozen lines and marked as a mirror in both files; change one and change the other.

## Without the browser

Everything works headless, and the CLI is unchanged:

```
python scanner.py                                           # scan, print a report
python allocator.py allocate --source results/latest.json   # open paper positions
python allocator.py status                                  # cash, exposure, P&L
python allocator.py settle                                  # resolve what's due
```

Every knob also lives in a `CONFIG` block at the top of `scanner.py` and
`allocator.py`. In `scanner.py` that includes which command to run, so you can
edit the block and press Run with no command line at all. Flags override CONFIG,
CONFIG overrides the built-in defaults.

The dashboard is static files, so it also works with no backend at all:
`python -m http.server 8000`, then `localhost:8000/dashboard/`. It probes for
`app.py` once at startup; finding nothing, it reads the committed `results/`
read-only and hides the Run button and the Bot tab. That is what makes GitHub
Pages work (Settings → Pages → deploy from `main`, root), and it is deliberate —
see **Blocked networks**.

Run the tests with `python tests.py` (41, no network). `test_allocator.py`,
`test_bot.py` and `test_app.py` sit beside it; CI runs all four.

## When it finds nothing

The attrition table on stderr counts every reason a market was dropped
(overlapping counts intended — they show whether one filter or four emptied the
pipeline). If a venue yields zero usable markets, the scanner prints the schema
it actually received with sample values, so a renamed API field is visible on
sight rather than a silent zero.

## Blocked networks

Some filtered networks (schools, workplaces) block both venues' APIs and serve an
HTML block page instead. The scanner detects this and says so rather than dying
on a JSON parse error.

If the machine you use is permanently behind such a filter, don't fight it — run
the scan somewhere else and read the results here. **Run on GitHub Actions**
below does exactly that: the code runs on GitHub's servers, which aren't behind
the filter, and commits the report back to this repo where the browser can read it.

For a one-off local run on an unfiltered machine you can also set
`$env:HTTPS_PROXY`, or point `--offline` at a directory of `kalshi.json` and
`polymarket.json` (see `fixtures/` for the format). Note that `--offline`
replaces the *market list* only; the depth pass still fetches order books, so add
`--depth 0` for a run that touches no network at all.

Both APIs are public and read-only; scanning needs no account or keys.

## LLM review of the top pairs (`judge.py`)

The scanner matches *words*; it can't tell that "redistrict" and "use a new
congressional map" mean the same event, or that Rubio as "leader" vs "head of
state" might resolve *differently*. `judge.py` sends the most promising pairs
from a scan to Claude and asks, adversarially, whether a YES on one venue
really pays out under exactly the same circumstances as a YES on the other —
returning a verdict, a confidence, and the specific divergence it found.

```
python judge.py run --source results/latest.json -c 10   # review the top 10 pairs
python judge.py score                                     # score the model vs labels.json
python test_judge.py                                      # 13 tests, no network, no key, no spend
```

It writes `results/judged.json` — each pair with an `llm_verdict` block — plus
a readable summary. Default model is `claude-opus-5`; pass `--model
claude-sonnet-5` for a cheaper pass, and use `score` to race the two on your
own labels before trusting either.

**The dashboard leads with it.** When `judged.json` is present, the
Opportunities board reorders around the model's call — confirmed pairs rise to
the top, the ones it flagged as mismatches sink to the bottom and are dimmed,
whatever their edge — and each row carries a MATCH / LIKELY / MISMATCH badge.
The detail drawer opens on the verdict: the divergence the model found, its
per-check breakdown (source / timing / threshold), and its reasoning, above the
rule texts. Two filter chips isolate the confirmed and the rejected. With no
`judged.json`, the board is exactly as it was — the whole feature stays dark
until the judge has run.

### The API key — never commit it

`judge.py` reads the key from the **`ANTHROPIC_API_KEY` environment variable**
only. It is never hardcoded and must never be committed — a key in a public
repo is scraped and drained within minutes.

- **Locally:** `$env:ANTHROPIC_API_KEY = "sk-ant-..."` (PowerShell) before running.
- **In GitHub Actions:** add it as a repo secret — Settings → Secrets and
  variables → Actions → New repository secret, named `ANTHROPIC_API_KEY`. The
  scan workflow then reviews the top 10 pairs automatically after each scan.
  The value never appears in the repo or the logs.

With no key set, `judge.py` prints a notice and exits cleanly — the scanner is
unaffected. This is a paid API: reviewing ~10 pairs on the default model runs a
few tens of cents per scan.

## Run on GitHub Actions

`.github/workflows/scan.yml` runs the scanner on GitHub's own servers and commits
the output to `results/`. Nothing runs on your machine, so a local network filter
never enters into it.

1. Push this repo to GitHub (already done if you cloned it from there).
2. Open the **Actions** tab, pick **scan**, and press **Run workflow** — or wait
   for the daily schedule.
3. When it finishes, read `results/latest.txt` in the repo, or open the dashboard.
   `latest.json` and `latest.csv` sit beside it, `meta.json` holds the run's
   thresholds and funnel counts as data, `run.log` holds the progress and any
   errors, and `history/` accumulates one dated snapshot per run (rule texts
   stripped, most recent 180 kept).

Public repositories get unlimited Actions minutes, so you can raise the schedule
or trigger it by hand as often as you like. One caveat worth a first test:
GitHub's runners live in a US datacentre, so if a venue geo-restricts its data
API the depth pass may fall back to top-of-book — `run.log` will show it if so.

CI runs the scanner, not the server. The bot is a local thing: `paper.json` and
`bot.json` are gitignored, so nothing about a simulated portfolio is committed.

## Files

| | |
| --- | --- |
| `run.bat` | Double-click to start everything |
| `app.py` | The server: dashboard, scan API, live progress, bot control |
| `scanner.py` | The scanner, CONFIG block at the top |
| `allocator.py` | Paper-trading allocation, as a CLI and as a library |
| `bot.py` | The allocator on a loop: cycles, mark-to-market, persistence |
| `dashboard/` | `index.html`, `styles.css`, `app.js` (loading, board), `drawer.js` (detail drawer), `views.js` (pipeline, lab, labels, trends), `live.js` (backend detection, live updates), `bot.js` (Bot tab) |
| `tests.py`, `test_allocator.py`, `test_bot.py`, `test_app.py` | Offline suites, no network |
| `fixtures/` | Sample data: a true arb, a threshold mismatch, a polarity flip |
| `results/` | Written by CI or `app.py`: `latest.{txt,json,csv}`, `meta.json`, `run.log`, `history/` |
| `labels.json`, `last_run.json`, `paper.json`, `bot.json`, `cache/` | Created at runtime, gitignored |

## Before trading a pair

"Locked profit" holds only if both contracts resolve identically. Confidence is a
text-similarity estimate, not a proof. Check, in order: the resolution source
(Coinbase 5pm ET and Binance VWAP are different bets), settlement timing
(Polymarket's UMA oracle can take days and be disputed), cancellation and
postponement clauses, and whether the venues are even legal for you to trade —
both restrict by jurisdiction. The quoted edge is also a snapshot; the second leg
often moves before you can fill it.
