#!/usr/bin/env python3
"""Offline tests for bot.py. No network required: python test_bot.py"""

import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import allocator as a
import bot as b

SOON = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
PAST = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def pair(key="K1", edge=0.03, cost=0.95, days=10, conf=0.8, rules=0.6,
         settle=SOON, warnings=None):
    """A scanner-shaped record. Same contract test_allocator.py encodes."""
    return {
        "edge": edge, "cost_per_contract": cost, "days_to_settle": days,
        "edge_leg": "buy YES kalshi / NO polymarket", "confidence": conf,
        "rules_similarity": rules, "warnings": warnings or [],
        "priced_at_top_of_book": False,
        "a": {"venue": "kalshi", "id": key, "question": f"will {key} happen",
              "url": f"https://kalshi.com/markets/{key}", "settle_time": settle},
        "b": {"venue": "polymarket", "id": f"poly-{key}",
              "url": f"https://poly/{key}", "question": f"{key}?",
              "settle_time": settle},
    }


class BotCase(unittest.TestCase):
    """Redirects both state files into a temp dir, like TestPaperFileIsolation."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._paper, self._bot = a.PAPER_PATH, b.BOT_PATH
        a.PAPER_PATH = os.path.join(self.dir, "paper.json")
        b.BOT_PATH = os.path.join(self.dir, "bot.json")
        self.addCleanup(self._restore)

    def _restore(self):
        a.PAPER_PATH, b.BOT_PATH = self._paper, self._bot

    def started(self, **overrides):
        state = b.load_state()
        state["running"] = True
        state["config"].update(overrides)
        return state


class TestState(BotCase):
    def test_defaults_assume_the_measured_mismatch_rate(self):
        # The flattering default is 0.0, which reports a profit by defining
        # away the largest risk in the strategy. See README "Why it is paper
        # only": at the seed base rate the same trades return -58% to -99%.
        self.assertEqual(b.DEFAULT_CONFIG["mismatch_loss_rate"], 0.75)
        self.assertTrue(b.DEFAULT_CONFIG["require_rules_match"])

    def test_missing_file_gives_defaults(self):
        state = b.load_state()
        self.assertFalse(state["running"])
        self.assertEqual(state["cycle_log"], [])
        self.assertEqual(state["config"]["bankroll"], a.CONFIG["bankroll"])

    def test_corrupt_file_does_not_stop_the_bot(self):
        with open(b.BOT_PATH, "w", encoding="utf-8") as fh:
            fh.write("{ not json")
        self.assertFalse(b.load_state()["running"])

    def test_config_written_by_an_older_version_gains_new_keys(self):
        b.write_json(b.BOT_PATH, {"running": True, "config": {"bankroll": 50.0}})
        state = b.load_state()
        self.assertEqual(state["config"]["bankroll"], 50.0)
        self.assertIn("mismatch_loss_rate", state["config"])  # merged, not replaced

    def test_writes_are_atomic(self):
        b.write_json(b.BOT_PATH, {"running": False, "config": {}, "cycle_log": []})
        # A failure mid-write leaves the previous file intact, not a stub.
        class Boom:
            def __repr__(self): raise RuntimeError("boom")
        with self.assertRaises((TypeError, RuntimeError)):
            b.write_json(b.BOT_PATH, {"bad": Boom()})
        with open(b.BOT_PATH, encoding="utf-8") as fh:
            self.assertIn("config", json.load(fh))
        leftovers = [n for n in os.listdir(self.dir) if n.startswith(".tmp-")]
        self.assertEqual(leftovers, [])

    def test_cycle_log_is_capped(self):
        state = b.load_state()
        state["cycle_log"] = [{"at": str(i)} for i in range(b.CYCLE_LOG_LIMIT + 50)]
        b.save_state(state)
        self.assertEqual(len(b.load_state()["cycle_log"]), b.CYCLE_LOG_LIMIT)


class TestCleanConfig(BotCase):
    def test_coerces_browser_strings(self):
        cleaned = b.clean_config({"bankroll": "250", "positions": "3",
                                  "deploy_frac": "0.25"})
        self.assertEqual(cleaned, {"bankroll": 250.0, "positions": 3,
                                   "deploy_frac": 0.25})
        self.assertIsInstance(cleaned["positions"], int)

    def test_drops_unknown_and_unparseable_keys(self):
        cleaned = b.clean_config({"bogus": 1, "bankroll": "not a number",
                                  "positions": 4})
        self.assertEqual(cleaned, {"positions": 4})

    def test_empty_seed_means_random(self):
        self.assertEqual(b.clean_config({"settle_seed": ""}), {"settle_seed": None})
        self.assertEqual(b.clean_config({"settle_seed": "7"}), {"settle_seed": 7})


class TestCycle(BotCase):
    def test_a_cycle_opens_positions_and_logs_a_point(self):
        state = self.started()
        portfolio = b.load_portfolio(state)
        cycle = b.run_cycle([pair("A"), pair("B", edge=0.02, cost=0.90)],
                            state, portfolio)

        self.assertEqual(cycle["opened"], 2)
        self.assertEqual(len(portfolio["open"]), 2)
        self.assertLess(portfolio["cash"], 1000.0)
        self.assertEqual(state["cycle_log"][-1], cycle)
        # The portfolio is on disk, not only in memory.
        with open(a.PAPER_PATH, encoding="utf-8") as fh:
            self.assertEqual(len(json.load(fh)["open"]), 2)

    def test_the_same_pair_is_not_bought_twice(self):
        state = self.started()
        portfolio = b.load_portfolio(state)
        b.run_cycle([pair("A")], state, portfolio)
        second = b.run_cycle([pair("A")], state, portfolio)

        self.assertEqual(second["opened"], 0)
        self.assertEqual(len(portfolio["open"]), 1)
        self.assertIn("no eligible new pairs in this scan", second["notes"])

    def test_unverified_rules_are_refused_by_default(self):
        state = self.started()
        portfolio = b.load_portfolio(state)
        cycle = b.run_cycle([pair("A", rules=None)], state, portfolio)
        self.assertEqual(cycle["opened"], 0)

        state["config"]["require_rules_match"] = False
        cycle = b.run_cycle([pair("A", rules=None)], state, portfolio)
        self.assertEqual(cycle["opened"], 1)

    def test_due_positions_settle_before_the_next_allocation(self):
        state = self.started(mismatch_loss_rate=0.0, settle_seed=1)
        portfolio = b.load_portfolio(state)
        b.run_cycle([pair("A", settle=PAST)], state, portfolio)
        self.assertEqual(len(portfolio["open"]), 1)

        cycle = b.run_cycle([pair("B")], state, portfolio)
        self.assertEqual(cycle["settled"], 1)
        self.assertEqual(portfolio["closed"][0]["status"], "hedged-win")

    def test_a_paused_bot_is_the_servers_business_not_the_cycles(self):
        # run_cycle always trades; the server decides whether to call it. Kept
        # that way so "Trade this scan" works while paused.
        state = b.load_state()  # running is False
        portfolio = b.load_portfolio(state)
        self.assertEqual(b.run_cycle([pair("A")], state, portfolio)["opened"], 1)


class TestMarkToMarket(BotCase):
    def setUp(self):
        super().setUp()
        self.state = self.started()
        self.portfolio = b.load_portfolio(self.state)
        b.run_cycle([pair("A", cost=0.95)], self.state, self.portfolio)

    def test_a_cheaper_hedge_marks_the_position_down(self):
        # Unwinding now returns what the hedge currently costs, not what we
        # paid for it.
        marked = b.mark_to_market(self.portfolio, [pair("A", cost=0.80)])
        held = marked[0]
        contracts = held["contracts"]
        self.assertAlmostEqual(held["mark"], round(contracts * 0.80, 4))
        self.assertAlmostEqual(held["unrealized"], round(held["mark"] - held["stake"], 4))
        self.assertLess(held["unrealized"], 0)

    def test_a_pair_that_left_the_scan_gets_no_mark_rather_than_a_stale_one(self):
        marked = b.mark_to_market(self.portfolio, [pair("SOMETHING-ELSE")])
        self.assertIsNone(marked[0]["mark"])
        self.assertIsNone(marked[0]["unrealized"])
        self.assertEqual(marked[0]["mark_note"], "no current quote")
        # An unmarkable position contributes nothing rather than counting as 0.
        self.assertEqual(b.unrealized_pnl(marked), 0.0)

    def test_an_unusable_price_is_not_a_mark(self):
        for bad in (None, 0.0, 1.0, 1.5):
            marked = b.mark_to_market(self.portfolio, [pair("A", cost=bad)])
            self.assertIsNone(marked[0]["mark"], f"cost {bad!r} should not mark")


class TestManualActions(BotCase):
    def test_flatten_returns_the_stake_and_books_nothing(self):
        state = self.started()
        portfolio = b.load_portfolio(state)
        b.run_cycle([pair("A"), pair("B")], state, portfolio)
        before = portfolio["cash"] + a.open_stake(portfolio)

        closed = b.flatten(portfolio)
        self.assertEqual(len(closed), 2)
        self.assertEqual(portfolio["open"], [])
        self.assertAlmostEqual(portfolio["cash"], before, places=4)
        self.assertEqual(a.realized_pnl(portfolio), 0.0)
        self.assertTrue(all(c["status"] == "flattened" for c in closed))

    def test_reset_starts_over(self):
        state = self.started()
        portfolio = b.load_portfolio(state)
        b.run_cycle([pair("A")], state, portfolio)

        fresh = b.reset(250.0)
        self.assertEqual(fresh["cash"], 250.0)
        self.assertEqual(fresh["open"], [])
        with open(a.PAPER_PATH, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["starting_bankroll"], 250.0)


class TestMismatchRate(BotCase):
    """The number the README warns about, exercised at both ends."""

    def outcome(self, rate):
        state = self.started(mismatch_loss_rate=rate, settle_seed=7,
                             positions=8, deploy_frac=1.0)
        portfolio = b.load_portfolio(state)
        b.run_cycle([pair(f"K{i}", settle=PAST) for i in range(8)],
                    state, portfolio)
        b.run_cycle([], state, portfolio)  # settles what the first cycle opened
        return portfolio

    def test_optimistic_rate_profits(self):
        portfolio = self.outcome(0.0)
        self.assertGreater(a.realized_pnl(portfolio), 0)

    def test_the_seed_base_rate_destroys_the_bankroll(self):
        portfolio = self.outcome(0.75)
        self.assertLess(a.realized_pnl(portfolio), 0)
        self.assertLess(a.equity(portfolio), portfolio["starting_bankroll"])


class TestSnapshot(BotCase):
    def test_snapshot_carries_what_the_tab_draws(self):
        state = self.started()
        portfolio = b.load_portfolio(state)
        records = [pair("A")]
        b.run_cycle(records, state, portfolio)

        snap = b.snapshot(state, portfolio, records)
        for key in ("running", "config", "equity", "cash", "exposure", "realized",
                    "unrealized", "open", "closed", "closed_total", "last_cycle",
                    "cycle_log", "starting_bankroll"):
            self.assertIn(key, snap)
        self.assertTrue(snap["running"])
        self.assertEqual(len(snap["open"]), 1)
        self.assertIn("mark", snap["open"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
