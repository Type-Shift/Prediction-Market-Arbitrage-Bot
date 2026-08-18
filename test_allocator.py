#!/usr/bin/env python3
"""Offline tests for allocator.py. No network, no scanner run: python test_allocator.py

Portfolio state lives in a temp file so a test run never touches paper.json.
"""

import os
import tempfile
import unittest
from datetime import timedelta, timezone

import allocator as a

CFG = dict(a.CONFIG)


def pair(key="K1", edge=0.03, cost=0.95, days=10, conf=0.8, rules=0.6,
         leg="buy YES kalshi / NO polymarket", warnings=None, top_only=False,
         settle=None):
    kid = f"KX-{key}"
    now = a.now_utc()
    settle_iso = (settle or (now + timedelta(days=days))).isoformat()
    return {
        "confidence": conf, "rules_similarity": rules, "edge": edge,
        "cost_per_contract": cost, "days_to_settle": days, "edge_leg": leg,
        "warnings": warnings or [], "priced_at_top_of_book": top_only,
        "a": {"venue": "kalshi", "id": kid, "question": f"Q {key}?",
              "url": f"https://kalshi.com/markets/{key}", "settle_time": settle_iso},
        "b": {"venue": "polymarket", "id": f"poly-{key}",
              "url": f"https://polymarket.com/market/{key}",
              "question": f"Q {key}?", "settle_time": settle_iso},
    }


def fresh_portfolio(cash=1000.0):
    return {"starting_bankroll": cash, "cash": cash, "open": [], "closed": [],
            "created": a.now_utc().isoformat()}


class TestEligibility(unittest.TestCase):
    def test_accepts_a_clean_pair(self):
        ok, _ = a.is_tradeable(pair(), CFG)
        self.assertTrue(ok)

    def test_rejects_negative_or_low_edge(self):
        self.assertFalse(a.is_tradeable(pair(edge=0.0), CFG)[0])
        self.assertFalse(a.is_tradeable(pair(edge=-0.05), CFG)[0])

    def test_rejects_polarity_and_illiquid(self):
        self.assertFalse(a.is_tradeable(pair(leg="not computed — check polarity by hand"), CFG)[0])
        self.assertFalse(a.is_tradeable(pair(leg="no two-sided liquidity at requested size"), CFG)[0])

    def test_rejects_unverified_rules_by_default(self):
        self.assertFalse(a.is_tradeable(pair(rules=None), CFG)[0])
        self.assertFalse(a.is_tradeable(pair(rules=0.10), CFG)[0])

    def test_can_allow_unverified_rules(self):
        cfg = dict(CFG, require_rules_match=False)
        self.assertTrue(a.is_tradeable(pair(rules=None), cfg)[0])

    def test_rejects_implausible_edge_warning(self):
        p = pair(warnings=[f"edge of 80pp {a.IMPLAUSIBLE_MARK} — treat as evidence"])
        self.assertFalse(a.is_tradeable(p, CFG)[0])

    def test_rejects_low_confidence(self):
        self.assertFalse(a.is_tradeable(pair(conf=0.40), CFG)[0])


class TestScoreTimePreference(unittest.TestCase):
    def test_sooner_scores_higher_at_equal_edge(self):
        soon = pair("A", edge=0.02, days=5)
        late = pair("B", edge=0.02, days=200)
        self.assertGreater(a.score(soon, 1.0), a.score(late, 1.0))

    def test_time_pref_zero_sizes_on_edge_only(self):
        soon_small = pair("A", edge=0.01, days=5)
        late_big = pair("B", edge=0.05, days=200)
        self.assertGreater(a.score(late_big, 0.0), a.score(soon_small, 0.0))
        # but with time preference on, the soon one wins
        self.assertGreater(a.score(soon_small, 1.0), a.score(late_big, 1.0))


class TestAllocation(unittest.TestCase):
    def test_equal_spread_and_cap(self):
        port = fresh_portfolio(1000)
        cfg = dict(CFG, positions=4, deploy_frac=1.0, max_position_frac=0.15,
                   weighting="equal", min_confidence=0.6)
        pairs = [pair(f"P{i}", cost=0.90, days=10 + i) for i in range(4)]
        orders, _ = a.plan_allocation(port, pairs, cfg)
        self.assertEqual(len(orders), 4)
        cap = 0.15 * a.equity(port)
        for o in orders:
            self.assertLessEqual(o["stake"], cap + o["cost_per_contract"])

    def test_sooner_pairs_are_selected_first(self):
        port = fresh_portfolio(1000)
        cfg = dict(CFG, positions=2, deploy_frac=1.0, max_position_frac=0.25,
                   weighting="equal", min_confidence=0.6, time_pref=1.0)
        pairs = [pair("SOON", edge=0.02, days=3), pair("MID", edge=0.02, days=50),
                 pair("LATE", edge=0.02, days=300)]
        orders, _ = a.plan_allocation(port, pairs, cfg)
        picked = {o["key"] for o in orders}
        self.assertIn("kalshi:KX-SOON|polymarket:poly-SOON", picked)
        self.assertNotIn("kalshi:KX-LATE|polymarket:poly-LATE", picked)

    def test_dedupe_against_open_positions(self):
        port = fresh_portfolio(1000)
        cfg = dict(CFG, positions=4, deploy_frac=1.0, min_confidence=0.6)
        p = pair("DUP")
        first, _ = a.plan_allocation(port, [p], cfg)
        a.apply_orders(port, first)
        second, notes = a.plan_allocation(port, [p], cfg)
        self.assertEqual(second, [])

    def test_capped_weighting_never_exceeds_cap(self):
        port = fresh_portfolio(1000)
        cfg = dict(CFG, positions=5, deploy_frac=1.0, max_position_frac=0.20,
                   weighting="capped", min_confidence=0.6)
        # One very attractive pair that would hog the budget without a cap.
        pairs = [pair("HOG", edge=0.10, days=2)] + [pair(f"P{i}", edge=0.01, days=30)
                                                    for i in range(4)]
        orders, _ = a.plan_allocation(port, pairs, cfg)
        cap = 0.20 * a.equity(port)
        for o in orders:
            self.assertLessEqual(o["stake"], cap + o["cost_per_contract"])

    def test_cash_conservation(self):
        port = fresh_portfolio(1000)
        cfg = dict(CFG, positions=8, deploy_frac=0.5, min_confidence=0.6)
        pairs = [pair(f"P{i}", days=10 + i) for i in range(8)]
        orders, _ = a.plan_allocation(port, pairs, cfg)
        placed = a.apply_orders(port, orders)
        spent = sum(o["stake"] for o in placed)
        self.assertAlmostEqual(port["cash"], 1000 - spent, places=4)
        self.assertGreaterEqual(port["cash"], 0)


class TestWaterfill(unittest.TestCase):
    def test_respects_cap_and_budget(self):
        alloc = a.waterfill([5, 1, 1], budget=100, cap=40)
        self.assertLessEqual(max(alloc), 40 + 1e-9)
        self.assertLessEqual(sum(alloc), 100 + 1e-9)

    def test_spills_to_others_when_capped(self):
        # The big score wants everything but is capped, so the others get the rest.
        alloc = a.waterfill([100, 1, 1], budget=90, cap=30)
        self.assertAlmostEqual(alloc[0], 30, places=6)
        self.assertGreater(alloc[1], 0)
        self.assertGreater(alloc[2], 0)


class TestSettlement(unittest.TestCase):
    def _one_open(self, days_ago=1):
        port = fresh_portfolio(1000)
        settle = (a.now_utc() - timedelta(days=days_ago)).isoformat()
        p = pair("S", cost=0.90, days=1, settle=None)
        p["a"]["settle_time"] = settle
        p["b"]["settle_time"] = settle
        orders, _ = a.plan_allocation(port, [p], dict(CFG, positions=1,
                                                     deploy_frac=1.0, min_confidence=0.6))
        a.apply_orders(port, orders)
        return port

    def test_hedged_win_pays_out(self):
        port = self._one_open()
        stake = port["open"][0]["stake"]
        contracts = port["open"][0]["contracts"]
        resolved = a.settle(port, mismatch_loss_rate=0.0, seed=1)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["status"], "hedged-win")
        self.assertAlmostEqual(resolved[0]["pnl"], contracts - stake, places=4)
        self.assertGreater(resolved[0]["pnl"], 0)

    def test_mismatch_loses_the_whole_stake(self):
        port = self._one_open()
        stake = port["open"][0]["stake"]
        resolved = a.settle(port, mismatch_loss_rate=1.0, seed=1)
        self.assertEqual(resolved[0]["status"], "mismatch-loss")
        self.assertAlmostEqual(resolved[0]["pnl"], -stake, places=4)

    def test_future_positions_are_left_open(self):
        port = fresh_portfolio(1000)
        p = pair("FUT", days=30)
        orders, _ = a.plan_allocation(port, [p], dict(CFG, positions=1,
                                                     deploy_frac=1.0, min_confidence=0.6))
        a.apply_orders(port, orders)
        resolved = a.settle(port, 0.0, seed=1)
        self.assertEqual(resolved, [])
        self.assertEqual(len(port["open"]), 1)

    def test_high_mismatch_rate_destroys_bankroll(self):
        """The whole point: unverified auto-allocation bleeds out."""
        port = fresh_portfolio(1000)
        cfg = dict(CFG, positions=8, deploy_frac=1.0, max_position_frac=0.15,
                   min_confidence=0.6, require_rules_match=False)
        settle = (a.now_utc() - timedelta(days=1)).isoformat()
        pairs = []
        for i in range(8):
            p = pair(f"M{i}", edge=0.03, cost=0.90, days=1)
            p["a"]["settle_time"] = settle
            p["b"]["settle_time"] = settle
            pairs.append(p)
        a.apply_orders(port, a.plan_allocation(port, pairs, cfg)[0])
        a.settle(port, mismatch_loss_rate=0.75, seed=7)
        # At a 3pp edge you make 3c per losing... no: a mismatch loses ~100%
        # of stake while a win makes ~3%. 75% losers cannot be paid for by 25%
        # winners earning 3%. Equity must fall below the start.
        self.assertLess(a.equity(port), 1000)


class TestPaperFileIsolation(unittest.TestCase):
    def test_load_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            original = a.PAPER_PATH
            a.PAPER_PATH = os.path.join(d, "paper.json")
            try:
                port = a.load_portfolio(500)
                self.assertEqual(port["cash"], 500)
                port["cash"] = 123
                a.save_portfolio(port)
                again = a.load_portfolio(500)
                self.assertEqual(again["cash"], 123)
            finally:
                a.PAPER_PATH = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
