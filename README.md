# Kalshi / Polymarket divergence scanner

Finds markets that ask the same question on both venues and quote different
odds, then prices the difference against real order-book depth and both
venues' fee schedules. Python 3.10+, standard library only, no install.

```
python scanner.py                       # scan with the CONFIG defaults
python scanner.py review                # label the last scan's pairs by hand
python scanner.py score                 # precision/recall of current thresholds
python scanner.py sweep                 # find better thresholds from your labels
```

Every knob also lives in the `CONFIG` block at the top of `scanner.py` — edit
it and press Run; no command line needed. Flags override CONFIG.

Run the tests with `python tests.py` (35 tests, no network needed).

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
  (10pp) is treated as evidence the questions differ, not as free money. On
  the first live run, all 25 top "opportunities" sat between 25 and 87pp, and
  every single one was a mismatch.

Polymarket books whose outcomes are not literally Yes/No (e.g. Trump/Newsom)
are refused rather than guessed at — assuming outcome[0] is YES silently
inverts the contract, the most expensive mistake this program can make.

## How the edge is priced

Three numbers per pair:

- **Mid gap** — raw difference in implied probability. The "difference in
  odds" signal; ignores spreads, not tradeable as-is.
- **Edge** — what buying YES on one venue and NO on the other actually locks
  in at your requested `size`, walking both order books level by level and
  paying both venues' fees. Kalshi's taker fee is `0.07 · P·(1−P)` per
  contract, rounded **up** to the next cent per order; Polymarket's varies by
  category (0% geopolitics … 7% crypto) under the March 2026 V2 schedule.
- **Annualised** — that edge compounded over the days until the later leg
  settles, since capital is fully collateralised on both legs until then.

Pairs are priced twice: once on top-of-book to build a shortlist, then again
after real depth arrives for the top `depth` pairs. Kalshi publishes resting
bids on both sides, so its book is inverted into asks (a NO bid at 55c is a
YES ask at 45c). On Polymarket, YES and NO are separate tokens with
independent books — the complement of the YES bid is only a placeholder until
the real NO book is fetched.

## Tuning with your own labels

`review` steps through the last scan one keystroke per pair; `score` reports
precision and recall of the current gates against everything labelled;
`sweep` shows what each threshold costs and grid-searches the best corner.
24 labelled pairs from live runs ship as seeds — 18 of them mismatches, which
is the honest base rate. Labels accumulate in `labels.json`.

The gate logic (`passes_gates`) is deliberately one function shared by the
scanner and the harness, so the thresholds you measure are the thresholds you
run.

## When it finds nothing

The attrition table on stderr counts every reason a market was dropped
(overlapping counts intended — they show whether one filter or four emptied
the pipeline). If a venue yields zero usable markets, the scanner prints the
schema it actually received with sample values, so a renamed API field is
visible on sight rather than a silent zero.

## Blocked networks

Some filtered networks (schools, workplaces) block both venues' APIs and
serve an HTML block page instead. The scanner detects this and says so rather
than dying on a JSON parse error. Options: run it elsewhere, set
`$env:HTTPS_PROXY`, or point `--offline` at a directory containing
`kalshi.json` and `polymarket.json` (see `fixtures/` for the format).

Both APIs are public and read-only; scanning needs no account or keys.

## Files

- `scanner.py` — the whole tool, CONFIG block at the top
- `tests.py` — offline test suite
- `fixtures/` — sample data covering a true arb, a threshold mismatch, and a
  polarity flip
- `labels.json`, `last_run.json`, `cache/` — created at runtime, gitignored

## Before trading a pair

"Locked profit" holds only if both contracts resolve identically. Confidence
is a text-similarity estimate, not a proof. Check, in order: the resolution
source (Coinbase 5pm ET and Binance VWAP are different bets), settlement
timing (Polymarket's UMA oracle can take days and be disputed), cancellation
and postponement clauses, and whether the venues are even legal for you to
trade — both restrict by jurisdiction. The quoted edge is also a snapshot;
the second leg often moves before you can fill it.
