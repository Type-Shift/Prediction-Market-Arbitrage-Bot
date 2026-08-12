#!/usr/bin/env python3
"""Paper-trading allocator for the Kalshi/Polymarket scanner.

Reads the pairs the scanner flagged as profitable and spreads a simulated
bankroll across them. It favours the pairs that resolve soonest and never
stakes too much on any single one, so no one bad pair can take a big bite.

It places NO real orders and needs NO account, wallet, or API key. Every fill
is simulated against a local balance in paper.json.

Why paper only, spelled out because it matters:

    The scanner finds CANDIDATES, not confirmed arbitrages. Every line of its
    output says so -- "confidence is a text-similarity estimate". On the seed
    labels that ship with it, 18 of 24 candidates were not the same question
    at all: a primary-nominee market against a general-election one, Israel-
    Colombia against Israel-Lebanon, a gas price of $4.20 against XRP at $4.20.

    A "risk-free" cross-venue arb is only risk-free if the two markets truly
    resolve on identical terms. When they do not, the hedge is really two
    directional bets, and you can lose both. Automating real money against the
    candidate list -- without a human reading both rule sets first -- does not
    collect an edge. It collects the mismatches.

    So paper-trade it. Run `settle --mismatch-loss-rate 0.5` and watch a
    bankroll that "should" only ever grow shrink instead. That number is the
    whole argument, and it costs nothing to see.

Standard library only. Python 3.10+.

Commands:
    allocate   read a scan, pick and size paper positions, open them
    status     show open positions, exposure, and profit/loss so far
    settle     resolve positions whose settlement date has passed
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))

# ===========================================================================
#  CONFIG -- sensible defaults; every one is also a --flag.
# ===========================================================================

CONFIG = {
    "bankroll": 1000.0,        # starting simulated cash, first run only
    "positions": 8,            # how many pairs to hold at once (the spread)
    "deploy_frac": 0.50,       # fraction of equity to put to work each cycle
    "max_position_frac": 0.15, # hard cap on any single pair, as a fraction of equity
    "weighting": "equal",      # "equal" split, or "capped" score-weighted with a cap
    "time_pref": 1.0,          # >0 prejudices sooner-resolving pairs; 0 ignores time

    # A pair must clear all of these to be eligible. These are deliberately
    # stricter than the scanner's display thresholds: display is for a human to
    # look at, this is money (pretend money, but still).
    "min_edge": 0.01,          # locked profit per $1, after fees
    "min_confidence": 0.60,    # text-match confidence floor
    "require_rules_match": True,   # skip pairs whose rule texts were not compared or disagree
    "min_rules_sim": 0.35,
    "skip_top_of_book_only": False,  # if True, skip pairs whose size was not depth-verified
}

# ===========================================================================

PAPER_PATH = os.path.join(HERE, "paper.json")
IMPLAUSIBLE_MARK = "exceeds any plausible cross-venue dislocation"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def pair_key(pair: dict) -> str:
    """Stable identity for a pair, so re-running does not double-allocate."""
    k = pair.get("kalshi", {}).get("id", "")
    p = pair.get("polymarket", {}).get("url", "")
    return f"{k}|{p}"


def money(x: float) -> str:
    return f"${x:,.2f}"


# ---------------------------------------------------------------------------
# Portfolio persistence
# ---------------------------------------------------------------------------

def load_portfolio(bankroll: float) -> dict:
    if os.path.exists(PAPER_PATH):
        with open(PAPER_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    portfolio = {
        "starting_bankroll": bankroll,
        "cash": bankroll,
        "open": [],
        "closed": [],
        "created": now_utc().isoformat(),
    }
    save_portfolio(portfolio)
    return portfolio


def save_portfolio(portfolio: dict) -> None:
    with open(PAPER_PATH, "w", encoding="utf-8") as fh:
        json.dump(portfolio, fh, indent=2)


def open_stake(portfolio: dict) -> float:
    return sum(pos["stake"] for pos in portfolio["open"])


def equity(portfolio: dict) -> float:
    """Cash plus the cost basis of everything still open.

    Cost basis, not mark-to-market: paper positions are held to settlement, so
    their value is not known until they resolve. This keeps the cap honest --
    it is a fraction of what you have actually committed plus what is free.
    """
    return portfolio["cash"] + open_stake(portfolio)


# ---------------------------------------------------------------------------
# Eligibility and scoring
# ---------------------------------------------------------------------------

def is_tradeable(pair: dict, cfg: dict) -> tuple[bool, str]:
    """Would a careful person let money touch this pair? One reason back."""
    edge = pair.get("edge")
    cost = pair.get("cost_per_contract")
    days = pair.get("days_to_settle")
    leg = pair.get("edge_leg", "") or ""

    if edge is None or edge < cfg["min_edge"]:
        return False, "edge below floor"
    if cost is None or not (0.0 < cost < 1.0):
        return False, "no usable fill price"
    if days is None or days <= 0:
        return False, "no settlement date"
    if leg.startswith("not computed") or leg.startswith("no two-sided"):
        return False, "not executable (polarity or liquidity)"
    if pair.get("confidence", 0.0) < cfg["min_confidence"]:
        return False, "confidence below floor"

    rules_sim = pair.get("rules_similarity")
    if cfg["require_rules_match"]:
        if rules_sim is None:
            return False, "rule texts were not compared"
        if rules_sim < cfg["min_rules_sim"]:
            return False, "rule texts disagree"

    for warning in pair.get("warnings", []):
        if IMPLAUSIBLE_MARK in warning:
            return False, "edge implausibly large (mismatch evidence)"

    if cfg["skip_top_of_book_only"] and pair.get("priced_at_top_of_book"):
        return False, "size not verified against real depth"

    return True, "eligible"


def score(pair: dict, time_pref: float) -> float:
    """Higher is better. edge per dollar, discounted by time to settle.

    edge / days**time_pref is 'profit per dollar per unit of waiting'. With
    time_pref = 1 that is a daily rate, so a 1% edge resolving in 5 days beats
    a 3% edge resolving in a year -- which is exactly 'prejudice sooner'.
    Raise time_pref to prefer soon even more strongly; set it to 0 to size on
    edge alone.
    """
    days = max(pair.get("days_to_settle") or 1.0, 0.5)
    return pair["edge"] / (days ** time_pref)


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------

def waterfill(scores: list[float], budget: float, cap: float) -> list[float]:
    """Split `budget` proportional to `scores`, but no share above `cap`.

    Whatever a capped share cannot hold spills back to the others and is shared
    out again, until nothing is over the cap or the money runs out. This lets
    the better pairs draw more without any single one becoming a position big
    enough to hurt.
    """
    n = len(scores)
    alloc = [0.0] * n
    active = [i for i in range(n) if scores[i] > 0]
    remaining = min(budget, cap * n)  # cannot place more than n caps in total

    while active and remaining > 1e-9:
        total = sum(scores[i] for i in active)
        if total <= 0:
            break
        capped_this_round = []
        for i in active:
            share = remaining * scores[i] / total
            if alloc[i] + share >= cap - 1e-12:
                remaining -= (cap - alloc[i])
                alloc[i] = cap
                capped_this_round.append(i)
        if capped_this_round:
            active = [i for i in active if i not in capped_this_round]
            continue
        for i in active:
            alloc[i] += remaining * scores[i] / total
        remaining = 0.0
    return alloc


def plan_allocation(portfolio: dict, pairs: list[dict], cfg: dict) -> tuple[list[dict], list[str]]:
    """Decide what to buy this cycle. Pure: does not touch the portfolio.

    Returns (orders, notes). Each order is a dict ready for apply_orders.
    """
    notes: list[str] = []
    held = {pos["key"] for pos in portfolio["open"]}

    eligible = []
    for pair in pairs:
        key = pair_key(pair)
        if key in held:
            continue  # already have it; do not stack the same pair
        ok, _ = is_tradeable(pair, cfg)
        if ok:
            eligible.append(pair)

    if not eligible:
        notes.append("no eligible new pairs in this scan")
        return [], notes

    eligible.sort(key=lambda p: score(p, cfg["time_pref"]), reverse=True)

    total_equity = equity(portfolio)
    cash = portfolio["cash"]
    deployable = min(cash, cfg["deploy_frac"] * total_equity)
    cap = cfg["max_position_frac"] * total_equity
    if deployable <= 0 or cap <= 0:
        notes.append("no cash free to deploy")
        return [], notes

    # Room for at most this many equal, capped slices.
    slots = min(cfg["positions"], len(eligible))
    # If an equal split would breach the cap, we need more slots (or accept the
    # cap and leave cash idle). Selecting the top `slots` by score keeps the
    # sooner-resolving pairs.
    selected = eligible[:slots]

    if cfg["weighting"] == "capped":
        budgets = waterfill([score(p, cfg["time_pref"]) for p in selected],
                            deployable, cap)
    else:  # "equal": the literal even spread, each slice capped
        per = min(deployable / len(selected), cap)
        budgets = [per] * len(selected)

    orders: list[dict] = []
    for pair, budget in zip(selected, budgets):
        cost = pair["cost_per_contract"]
        contracts = math.floor(budget / cost)
        if contracts < 1:
            continue  # slice too small to buy even one contract
        stake = round(contracts * cost, 4)
        orders.append({
            "key": pair_key(pair),
            "kalshi_id": pair["kalshi"]["id"],
            "kalshi_q": pair["kalshi"]["question"],
            "poly_url": pair["polymarket"]["url"],
            "poly_q": pair["polymarket"]["question"],
            "leg": pair.get("edge_leg", ""),
            "cost_per_contract": cost,
            "contracts": contracts,
            "stake": stake,
            "edge": pair["edge"],
            "confidence": pair.get("confidence"),
            "days_to_settle": pair.get("days_to_settle"),
            "settle_time": (pair.get("kalshi", {}).get("settle_time")
                            or pair.get("polymarket", {}).get("settle_time")),
            "payoff_if_hedged": contracts * 1.0,
            "profit_if_hedged": round(contracts * (1.0 - cost), 4),
        })
    if not orders:
        notes.append("eligible pairs found, but each slice was too small to buy a contract")
    return orders, notes


def apply_orders(portfolio: dict, orders: list[dict]) -> list[dict]:
    """Commit orders that fit the remaining cash. Mutates the portfolio."""
    placed = []
    for order in orders:
        if order["stake"] > portfolio["cash"] + 1e-9:
            continue
        portfolio["cash"] = round(portfolio["cash"] - order["stake"], 4)
        position = dict(order, opened=now_utc().isoformat(), status="open")
        portfolio["open"].append(position)
        placed.append(position)
    return placed


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def settle(portfolio: dict, mismatch_loss_rate: float, seed: int | None) -> list[dict]:
    """Resolve every open position whose settlement date has passed.

    With mismatch_loss_rate = 0 every hedge is assumed to have held, so each
    due position pays out its contracts. That is the optimistic world where
    every candidate really was the same question.

    With mismatch_loss_rate > 0, each due position independently has that
    probability of being a mismatch that went against you -- stake lost, no
    payout. Set it to the base rate you actually believe (the seed labels put
    it near 0.75 for unfiltered candidates) to see what automation without
    human rule-checking really does to a bankroll.
    """
    rng = random.Random(seed)
    now = now_utc()
    still_open, resolved = [], []
    for pos in portfolio["open"]:
        settle_at = parse_time(pos.get("settle_time"))
        if settle_at is not None and settle_at > now:
            still_open.append(pos)
            continue
        mismatched = rng.random() < mismatch_loss_rate
        payoff = 0.0 if mismatched else pos["contracts"] * 1.0
        pnl = round(payoff - pos["stake"], 4)
        portfolio["cash"] = round(portfolio["cash"] + payoff, 4)
        resolved.append(dict(pos, status=("mismatch-loss" if mismatched else "hedged-win"),
                             payoff=round(payoff, 4), pnl=pnl,
                             settled=now.isoformat()))
    portfolio["open"] = still_open
    portfolio["closed"].extend(resolved)
    return resolved


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def realized_pnl(portfolio: dict) -> float:
    return round(sum(pos.get("pnl", 0.0) for pos in portfolio["closed"]), 4)


def print_status(portfolio: dict) -> None:
    eq = equity(portfolio)
    start = portfolio["starting_bankroll"]
    opened = portfolio["open"]
    exposure = open_stake(portfolio)
    print(f"paper portfolio  (started {money(start)})")
    print(f"  cash free       {money(portfolio['cash'])}")
    print(f"  at risk (open)  {money(exposure)}  across {len(opened)} position(s)")
    print(f"  equity at cost  {money(eq)}")
    realized = realized_pnl(portfolio)
    settled_n = len(portfolio["closed"])
    if settled_n:
        wins = sum(1 for p in portfolio["closed"] if p["status"] == "hedged-win")
        print(f"  realized P&L    {money(realized)}  over {settled_n} settled "
              f"({wins} held, {settled_n - wins} broke)")
    ret = (realized / start * 100) if start else 0.0
    print(f"  return so far   {ret:+.1f}% of starting bankroll (realized only)")

    if opened:
        biggest = max(p["stake"] for p in opened)
        print(f"\n  largest single position: {money(biggest)} "
              f"({biggest / eq * 100:.0f}% of equity) -- your worst-case single loss")
        print("\n  open positions (soonest first):")
        for pos in sorted(opened, key=lambda p: p.get("days_to_settle") or 9e9):
            days = pos.get("days_to_settle")
            days_s = f"{days:.0f}d" if days is not None else "  ?"
            print(f"    [{days_s:>4}]  stake {money(pos['stake']):>10}  "
                  f"edge {pos['edge'] * 100:+.1f}pp  conf {pos.get('confidence', 0):.2f}")
            print(f"            K: {pos['kalshi_q'][:70]}")
            print(f"            P: {pos['poly_q'][:70]}")


def print_plan(orders: list[dict], notes: list[str], portfolio: dict) -> None:
    for note in notes:
        print(f"note: {note}")
    if not orders:
        return
    total = sum(o["stake"] for o in orders)
    print(f"\nallocation plan: {len(orders)} position(s), {money(total)} deployed, "
          f"{money(portfolio['cash'])} cash before")
    print("=" * 78)
    for o in sorted(orders, key=lambda x: x.get("days_to_settle") or 9e9):
        days = o.get("days_to_settle")
        days_s = f"{days:.0f}d" if days is not None else "?"
        print(f"  [{days_s:>4}]  {o['contracts']:>4} contracts @ {o['cost_per_contract']:.3f}  "
              f"= stake {money(o['stake'])}  -> {money(o['payoff_if_hedged'])} if hedge holds "
              f"({o['edge'] * 100:+.1f}pp)")
        print(f"          {o['leg']}")
        print(f"          K: {o['kalshi_q'][:68]}")
        print(f"          P: {o['poly_q'][:68]}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def load_pairs(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("pairs") or data.get("results") or []
    return data


def cmd_allocate(args, cfg: dict) -> int:
    portfolio = load_portfolio(cfg["bankroll"])
    try:
        pairs = load_pairs(args.source)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read scan file {args.source!r}: {exc}", file=sys.stderr)
        print("hint: produce one with `python scanner.py scan --json results/latest.json`,",
              file=sys.stderr)
        print("      or on a filtered network let GitHub Actions produce results/latest.json.",
              file=sys.stderr)
        return 2

    orders, notes = plan_allocation(portfolio, pairs, cfg)
    print_plan(orders, notes, portfolio)

    if args.dry_run:
        print("\n(dry run -- nothing committed)")
        return 0

    placed = apply_orders(portfolio, orders)
    if placed:
        save_portfolio(portfolio)
        print(f"\nopened {len(placed)} paper position(s). "
              f"cash now {money(portfolio['cash'])}.")
        print("no real orders were placed; this is simulated. run `status` any time.")
    return 0


def cmd_status(args, cfg: dict) -> int:
    portfolio = load_portfolio(cfg["bankroll"])
    print_status(portfolio)
    return 0


def cmd_settle(args, cfg: dict) -> int:
    portfolio = load_portfolio(cfg["bankroll"])
    resolved = settle(portfolio, args.mismatch_loss_rate, args.seed)
    if not resolved:
        print("nothing due to settle yet.")
        return 0
    save_portfolio(portfolio)
    won = sum(1 for r in resolved if r["status"] == "hedged-win")
    pnl = round(sum(r["pnl"] for r in resolved), 4)
    print(f"settled {len(resolved)} position(s): {won} held as hedged, "
          f"{len(resolved) - won} resolved against you.")
    print(f"net from this settlement: {money(pnl)}")
    if args.mismatch_loss_rate > 0:
        print(f"(simulated at a {args.mismatch_loss_rate:.0%} mismatch-loss rate)")
    print()
    print_status(portfolio)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Paper-trade the scanner's profitable pairs: even spread, "
                    "sooner-resolving favoured. Simulated money only.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("allocate", help="pick and size positions from a scan")
    a.add_argument("--source", default=os.path.join(HERE, "results", "latest.json"),
                   help="scanner JSON (from `scanner.py scan --json ...`)")
    a.add_argument("--dry-run", action="store_true", help="show the plan, commit nothing")
    a.add_argument("--positions", type=int, default=CONFIG["positions"])
    a.add_argument("--deploy-frac", type=float, default=CONFIG["deploy_frac"])
    a.add_argument("--max-position-frac", type=float, default=CONFIG["max_position_frac"])
    a.add_argument("--weighting", choices=["equal", "capped"], default=CONFIG["weighting"])
    a.add_argument("--time-pref", type=float, default=CONFIG["time_pref"])
    a.add_argument("--min-edge", type=float, default=CONFIG["min_edge"])
    a.add_argument("--min-confidence", type=float, default=CONFIG["min_confidence"])
    a.add_argument("--allow-unverified-rules", action="store_true",
                   help="allocate even when rule texts were not compared (NOT advised)")
    a.add_argument("--skip-top-of-book-only", action="store_true",
                   help="skip pairs whose size was not confirmed against real depth")

    s = sub.add_parser("status", help="show the paper portfolio")

    t = sub.add_parser("settle", help="resolve positions past their settlement date")
    t.add_argument("--mismatch-loss-rate", type=float, default=0.0,
                   help="probability each due position was a mismatch and lost its stake; "
                        "try 0.75 to model unverified candidates")
    t.add_argument("--seed", type=int, default=None, help="seed the mismatch draw for repeatability")

    for parser in (a, s, t):
        parser.add_argument("--bankroll", type=float, default=CONFIG["bankroll"],
                            help="starting simulated cash (first run only)")
    return p


def resolve_config(args) -> dict:
    cfg = dict(CONFIG)
    cfg["bankroll"] = getattr(args, "bankroll", cfg["bankroll"])
    if args.command == "allocate":
        cfg.update({
            "positions": args.positions,
            "deploy_frac": args.deploy_frac,
            "max_position_frac": args.max_position_frac,
            "weighting": args.weighting,
            "time_pref": args.time_pref,
            "min_edge": args.min_edge,
            "min_confidence": args.min_confidence,
            "require_rules_match": not args.allow_unverified_rules,
            "skip_top_of_book_only": args.skip_top_of_book_only,
        })
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = resolve_config(args)
    if args.command == "allocate":
        return cmd_allocate(args, cfg)
    if args.command == "status":
        return cmd_status(args, cfg)
    if args.command == "settle":
        return cmd_settle(args, cfg)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
