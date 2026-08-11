# Kalshi / Polymarket divergence scanner

Finds markets that ask the same question on both venues and quote different
odds. Python 3.11+, standard library only, no install and no admin rights.

```
python scanner.py                                   # default scan
python scanner.py --arb-only                        # only edges that survive fees
python scanner.py --min-confidence 0.7 --verbose    # stricter matching, print both rule sets
python scanner.py --json out.json --csv out.csv     # machine-readable output
python scanner.py --offline fixtures                # no network, sample data
```

## Your network blocks both venues

The wincollad proxy returns a block page for `api.elections.kalshi.com` and
`*.polymarket.com`. The scanner detects that and says so rather than failing
with a JSON parse error. To get live data, run it off that network, or point it
through a proxy:

```powershell
$env:HTTPS_PROXY = "http://host:port"
python scanner.py
```

Both APIs are public and read-only, so no keys or accounts are needed to scan.

## How a match is decided

Text similarity alone marries markets that are not the same bet, so the score
combines four signals:

| Signal | Weight | What it measures |
| --- | --- | --- |
| Question text | 0.55 | TF-IDF cosine plus a sequence ratio over the titles |
| Resolution rules | 0.28 | Same, over Kalshi `rules_primary` vs the Polymarket description |
| Settlement date | 0.17 | How close the two close times are |
| Numeric thresholds | penalty | Compares the largest figure on each side |

Penalties then subtract from that base:

- **Thresholds differ** (−0.40): "S&P 500 above 7000" vs "above 7500" shares
  every word and most of its numbers. This is the trap that catches out naive
  matchers, and it is the reason the largest magnitude is compared directly.
- **Direction disagrees** (−0.35): one side says above, the other below.
- **Polarity flip** (−0.15): "will the US enter a recession" vs "will the US
  avoid a recession". These are complements, so YES on one is NO on the other.
  When this fires the scanner refuses to quote an edge at all — a wrong sign
  here turns a 56-point "opportunity" into two losing legs.

Candidate generation uses an inverted index over discriminative tokens, so a
scan of ~4,000 markets per venue scores tens of thousands of plausible pairs
rather than sixteen million arbitrary ones.

## How the edge is calculated

Two numbers are reported per pair.

**Mid gap** is the difference in implied probability. It is the "difference in
odds" signal, and it ignores spreads. Useful for spotting stale books, not
directly tradeable.

**Executable edge** is what you would actually make. Both venues pay $1 per
contract at settlement, so buying YES on one and NO on the other for less than
$1 combined is locked profit:

```
edge = 1 − (yes_ask on venue A + no_ask on venue B + fees)
```

Both directions are tried and the better one is reported. Ask prices are used,
not mids, so you are paying the spread on both legs. On Polymarket the NO ask
is derived as `1 − best YES bid`, the standard complement identity for linked
binary tokens.

Fees: Kalshi takers pay `ceil(0.07 × contracts × P × (1−P))` cents, so the
per-contract cost is roughly `0.07 × P × (1−P)` — about 1.75c at even money,
which eats most small edges. Some series charge 0.035; use
`--kalshi-fee-coeff 0.035` for those. Polymarket currently charges no trading
fee, so `--poly-fee-bps` defaults to 0. Change it if that stops being true.

## Reading the output

```
[2] confidence 0.73   mid gap +7.5pp   executable edge +4.33pp  <-- RISKLESS EDGE
    trade: buy YES polymarket / NO kalshi — pay $0.9567 for $1.00 at settlement
```

"Riskless" only holds if the two markets really do resolve identically.
Confidence is a text-similarity estimate, not a proof. Before trading a pair,
read both rule sets in full (`--verbose` prints them) and check:

- **Resolution source.** "Coinbase spot at 5pm ET" and "Binance VWAP over the
  final hour" are different bets that print nearly identical questions.
- **Settlement timing.** Capital is locked until both legs pay out, and they
  rarely pay on the same day. Polymarket's UMA oracle can take days to finalise
  and can be disputed.
- **Edge cases.** What happens if the event is cancelled, postponed, or the data
  source stops publishing? Venues handle this differently.
- **Depth.** The quoted ask is the top of the book. Size beyond it moves the
  price and the edge disappears fast — often before you can fill the second leg.

Lowering `--min-confidence` below the 0.55 default surfaces far more pairs, and
most of the new ones are false. In the sample data, a 24.8pp "riskless edge"
appears at confidence 0.31 — it is the S&P 7000 vs 7500 mismatch, and taking it
would lose money on both legs.

## Useful flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--min-edge` | 0.03 | Minimum gap or edge, as a probability |
| `--min-confidence` | 0.55 | Match confidence floor |
| `--arb-only` | off | Hide pairs with no post-fee edge |
| `--sort` | arb | `arb`, `gap`, or `confidence` |
| `--min-volume-kalshi` | 100 | Contracts traded |
| `--min-volume-poly` | 5000 | USD traded |
| `--max-date-diff` | 45 | Reject pairs settling this many days apart |
| `--max-days-out` | 400 | Ignore far-dated markets |
| `--max-candidates` | 40 | Polymarket candidates scored per Kalshi market |
| `--cache-ttl` | 300 | Seconds before refetching; raw data is cached |
| `--offline DIR` | — | Read `kalshi.json` and `polymarket.json` from DIR |

Cached responses land in `cache/`. They hold unfiltered API data, so lowering a
volume threshold does not force a refetch.

## Files

- `scanner.py` — the whole tool
- `fixtures/` — sample data covering a true arb, a threshold mismatch, and a
  polarity flip; used by `--offline`

## Caveats

- Only binary markets are compared. Polymarket entries with more than two
  outcomes are skipped; Kalshi multi-candidate events work because each
  candidate is already its own binary contract.
- A Kalshi market is matched to at most one Polymarket market — its best
  candidate.
- Prices are snapshots. By the time you read the report they have moved.
- Trading either venue requires an account there, and both restrict access by
  jurisdiction. Scanning public data does not.
