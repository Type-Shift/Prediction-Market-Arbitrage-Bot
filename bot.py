#!/usr/bin/env python3
"""The paper-trading bot: a scheduling layer over allocator.py.

allocator.py already knows how to spread a simulated bankroll across a scan's
pairs -- what it does not do is run itself. It is a command you type once per
scan you have reviewed. This module is what turns that into a loop: after every
scan the server finishes, `run_cycle` settles what is due, plans an allocation,
and books it, then records one point on an equity curve.

Everything here is deliberately thin. `plan_allocation`, `apply_orders` and
`settle` are the allocator's and stay the allocator's, so the sizing rules the
Bot tab obeys are the same ones `python allocator.py allocate` obeys. What this
module adds is the three things a running bot needs and a one-shot command does
not: persistence of its own on/off state, mark-to-market on open positions, and
writes that survive being interrupted.

A word on what this bot is. The scanner reports *candidates*, not confirmed
arbitrages, and README.md spends a section on why: a cross-venue hedge is only
risk-free if both markets resolve on identical terms, and on the seed labels 18
of 24 candidates did not. Reading both rule texts is the step that catches that,
and it is exactly the step an auto-trader skips. So the defaults here are the
cautious ones -- rules must be compared and must agree, and settlement assumes
the seed base rate of mismatches rather than the flattering zero. Both are
settings; see DEFAULT_CONFIG.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

import allocator

HERE = os.path.dirname(os.path.abspath(__file__))
BOT_PATH = os.path.join(HERE, "bot.json")

# How many cycles of history to keep. One per scan; at a scan every 15 minutes
# through waking hours that is a couple of months of equity curve.
CYCLE_LOG_LIMIT = 500

# The allocator's config plus the three settings that only mean something to a
# thing that runs on its own.
DEFAULT_CONFIG = dict(
    allocator.CONFIG,

    # What fraction of settling positions turn out to have been mismatches --
    # two different questions that merely read alike, where the "hedge" was
    # really two directional bets and both can lose.
    #
    # 0.0 is the optimistic world where every candidate was genuinely the same
    # question. The seed labels put the real rate near 0.75 for unfiltered
    # candidates, and that is the default here on purpose: a bot that assumes
    # zero reports a healthy profit by defining away the single largest risk in
    # the strategy. Lower it if you are filtering candidates by hand.
    mismatch_loss_rate=0.75,

    # Seed for the settlement draw. None is a genuinely random draw; set an
    # integer to make a run reproducible.
    settle_seed=None,

    # Settle due positions at the start of each cycle.
    auto_settle=True,
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Persistence
#
# Both files are written atomically. The allocator writes paper.json straight
# over the old one (allocator.save_portfolio), which is fine for a command that
# runs and exits, but this process serves HTTP while it writes: a crash or a
# read landing mid-write would leave a truncated portfolio and lose the whole
# simulated history. Write to a temp file in the same directory, then rename --
# os.replace is atomic, so a reader sees either the old file or the new one.
# ---------------------------------------------------------------------------

def write_json(path: str, payload) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)
    except BaseException:
        # Leave the existing file untouched rather than half-written.
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def default_state() -> dict:
    return {
        "running": False,
        "config": dict(DEFAULT_CONFIG),
        "last_cycle": None,
        "cycle_log": [],
    }


def load_state() -> dict:
    """The bot's own state. A missing or corrupt file is not fatal.

    A bot that refuses to start because its settings file was truncated is
    worse than one that starts from defaults and says so -- the portfolio, the
    part that actually matters, lives in paper.json and is loaded separately.
    """
    state = default_state()
    try:
        with open(BOT_PATH, encoding="utf-8") as fh:
            stored = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return state

    if not isinstance(stored, dict):
        return state
    state["running"] = bool(stored.get("running", False))
    state["last_cycle"] = stored.get("last_cycle")
    log = stored.get("cycle_log")
    state["cycle_log"] = log[-CYCLE_LOG_LIMIT:] if isinstance(log, list) else []
    # Merge rather than replace, so a config written by an older version picks
    # up new keys with their defaults instead of raising a KeyError deep in the
    # allocator.
    if isinstance(stored.get("config"), dict):
        state["config"].update(stored["config"])
    return state


def save_state(state: dict) -> None:
    state["cycle_log"] = state["cycle_log"][-CYCLE_LOG_LIMIT:]
    write_json(BOT_PATH, state)


def load_portfolio(state: dict) -> dict:
    return allocator.load_portfolio(state["config"]["bankroll"])


def save_portfolio(portfolio: dict) -> None:
    write_json(allocator.PAPER_PATH, portfolio)


def clean_config(incoming: dict) -> dict:
    """Keep only known keys, coerced to the type of the default.

    This is the boundary where browser JSON becomes numbers the allocator
    divides by. An unvalidated string here surfaces as a TypeError halfway
    through sizing a position, with the portfolio already half-mutated.
    """
    cleaned = {}
    for key, value in (incoming or {}).items():
        if key not in DEFAULT_CONFIG:
            continue
        default = DEFAULT_CONFIG[key]
        try:
            if key == "settle_seed":
                cleaned[key] = None if value in (None, "") else int(value)
            elif isinstance(default, bool):
                cleaned[key] = bool(value)
            elif isinstance(default, float):
                cleaned[key] = float(value)
            elif isinstance(default, int):
                cleaned[key] = int(value)
            else:
                cleaned[key] = str(value)
        except (TypeError, ValueError):
            continue  # unparseable: keep the existing setting
    return cleaned


# ---------------------------------------------------------------------------
# Mark to market
# ---------------------------------------------------------------------------

def mark_to_market(portfolio: dict, records: list[dict]) -> list[dict]:
    """Open positions re-priced against the newest scan.

    The allocator values open positions at cost (see its `equity`), because a
    paper position is held to settlement and its true value is not known until
    it resolves. That is the right basis for sizing, but it makes a monitor
    useless: every open row would read zero forever.

    So each open position is matched back to its pair in the current scan by
    key, and valued at what the same hedge costs now. A pair that has dropped
    out of the scan -- expired, or fell below a threshold -- gets no mark at
    all rather than a stale one, and says so.
    """
    current = {allocator.pair_key(rec): rec for rec in records or []}
    marked = []
    for pos in portfolio["open"]:
        row = dict(pos)
        pair = current.get(pos["key"])
        cost_now = (pair or {}).get("cost_per_contract")
        if pair is None or cost_now is None or not (0.0 < cost_now < 1.0):
            row["mark"] = None
            row["unrealized"] = None
            row["mark_note"] = "no current quote"
        else:
            value = round(pos["contracts"] * cost_now, 4)
            row["mark"] = value
            row["unrealized"] = round(value - pos["stake"], 4)
            row["mark_note"] = ""
            row["edge_now"] = pair.get("edge")
        marked.append(row)
    return marked


def unrealized_pnl(marked: list[dict]) -> float:
    return round(sum(row["unrealized"] for row in marked
                     if row.get("unrealized") is not None), 4)


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------

def run_cycle(records: list[dict], state: dict, portfolio: dict) -> dict:
    """One bot cycle against a fresh scan. Mutates and saves the portfolio.

    Settle first, then allocate: a position resolving today frees cash that the
    same cycle can put back to work, which is what a person doing this by hand
    would do.
    """
    cfg = state["config"]
    settled = []
    if cfg["auto_settle"]:
        settled = allocator.settle(portfolio, cfg["mismatch_loss_rate"],
                                   cfg["settle_seed"])

    orders, notes = allocator.plan_allocation(portfolio, records, cfg)
    placed = allocator.apply_orders(portfolio, orders)
    if orders and not placed:
        notes.append("orders planned but none fit the remaining cash")

    save_portfolio(portfolio)

    cycle = summarise(portfolio, records, opened=len(placed),
                      settled=len(settled), notes=notes)
    state["last_cycle"] = cycle
    state["cycle_log"].append(cycle)
    save_state(state)
    return cycle


def summarise(portfolio: dict, records: list[dict], opened: int = 0,
              settled: int = 0, notes: list[str] | None = None) -> dict:
    """One point on the equity curve, plus what happened to get there."""
    marked = mark_to_market(portfolio, records)
    return {
        "at": now_iso(),
        "equity": round(allocator.equity(portfolio), 2),
        "cash": round(portfolio["cash"], 2),
        "exposure": round(allocator.open_stake(portfolio), 2),
        "realized": allocator.realized_pnl(portfolio),
        "unrealized": unrealized_pnl(marked),
        "open": len(portfolio["open"]),
        "closed": len(portfolio["closed"]),
        "opened": opened,
        "settled": settled,
        "notes": notes or [],
    }


# ---------------------------------------------------------------------------
# Manual actions
# ---------------------------------------------------------------------------

def flatten(portfolio: dict) -> list[dict]:
    """Close every open position at cost. Nothing won, nothing lost.

    Distinct from `settle`, which resolves positions whose date has passed and
    pays out or wipes out accordingly. This is the "stop, I want my simulated
    cash back" button: it makes no claim about how those markets would have
    resolved, so it returns the stake and books zero P&L.
    """
    closed = []
    for pos in portfolio["open"]:
        portfolio["cash"] = round(portfolio["cash"] + pos["stake"], 4)
        closed.append(dict(pos, status="flattened", payoff=pos["stake"],
                           pnl=0.0, settled=now_iso()))
    portfolio["open"] = []
    portfolio["closed"].extend(closed)
    save_portfolio(portfolio)
    return closed


def reset(bankroll: float) -> dict:
    """Throw the portfolio away and start over at `bankroll`."""
    portfolio = {
        "starting_bankroll": bankroll,
        "cash": bankroll,
        "open": [],
        "closed": [],
        "created": now_iso(),
    }
    save_portfolio(portfolio)
    return portfolio


def snapshot(state: dict, portfolio: dict, records: list[dict]) -> dict:
    """Everything the Bot tab draws, in one payload."""
    marked = mark_to_market(portfolio, records)
    return {
        "running": state["running"],
        "config": state["config"],
        "starting_bankroll": portfolio["starting_bankroll"],
        "equity": round(allocator.equity(portfolio), 2),
        "cash": round(portfolio["cash"], 2),
        "exposure": round(allocator.open_stake(portfolio), 2),
        "realized": allocator.realized_pnl(portfolio),
        "unrealized": unrealized_pnl(marked),
        "open": marked,
        "closed": portfolio["closed"][-100:],
        "closed_total": len(portfolio["closed"]),
        "last_cycle": state["last_cycle"],
        "cycle_log": state["cycle_log"],
        "created": portfolio.get("created"),
    }
