#!/usr/bin/env python3
"""Offline tests for scanner.py. No network required: python tests.py"""

import unittest
from datetime import datetime, timedelta, timezone

import scanner as s

SOON = datetime.now(timezone.utc) + timedelta(days=30)


def make_market(venue="kalshi", question="q", rules="r", yes_ask=0.50, no_ask=0.52,
                category="politics", book=None, settle=SOON, **kw):
    m = s.Market(
        venue=venue, market_id=f"{venue}-x", question=question, rules=rules,
        url="", category=category,
        yes_bid=(yes_ask - 0.02) if yes_ask else None, yes_ask=yes_ask,
        no_bid=(no_ask - 0.02) if no_ask else None, no_ask=no_ask,
        last=yes_ask, volume=1000, liquidity=1000,
        close_time=settle, settle_time=settle, book=book, **kw,
    )
    m.prepare()
    return m


def make_pair(k, p, **kw):
    defaults = dict(title_sim=0.9, rules_sim=0.9, date_sim=1.0, penalty=0.0,
                    confidence=0.9, days_apart=0.0, warnings=[])
    defaults.update(kw)
    return s.Pair(kalshi=k, poly=p, **defaults)


class TestNumbers(unittest.TestCase):
    def test_years_split_from_magnitudes(self):
        years, mags = s.extract_numbers("Bitcoin above $150,000 on December 31, 2026")
        self.assertIn(2026.0, years)
        self.assertIn(150000.0, mags)
        self.assertNotIn(2026.0, mags)

    def test_year_mismatch_is_not_within_tolerance(self):
        k = make_market(question="Will X happen in 2026?")
        p = make_market(venue="polymarket", question="Will X happen in 2028?")
        penalty, notes = s.number_agreement(k, p)
        self.assertGreaterEqual(penalty, 0.45)
        self.assertTrue(any("years differ" in n for n in notes))

    def test_threshold_trap(self):
        k = make_market(question="Will the S&P 500 close above 7000 on December 31, 2026?")
        p = make_market(venue="polymarket",
                        question="Will the S&P 500 close above 7500 on December 31, 2026?")
        penalty, notes = s.number_agreement(k, p)
        self.assertGreaterEqual(penalty, 0.40)
        self.assertTrue(any("thresholds differ" in n for n in notes))

    def test_suffix_scaling(self):
        _, mags = s.extract_numbers("above 150k users and 2m dollars")
        self.assertIn(150000.0, mags)
        self.assertIn(2000000.0, mags)


class TestKalshiPrices(unittest.TestCase):
    def test_dollars_field_is_probability(self):
        self.assertEqual(s.probability("0.4500", "yes_bid_dollars"), 0.45)

    def test_legacy_field_is_cents(self):
        self.assertEqual(s.probability(45, "yes_bid"), 0.45)

    def test_zero_means_no_order_in_both(self):
        self.assertIsNone(s.probability(0, "yes_bid"))
        self.assertIsNone(s.probability("0", "yes_bid_dollars"))

    def test_pick_prefers_dollars(self):
        row = {"yes_bid_dollars": "0.61", "yes_bid": 61}
        self.assertEqual(s.kalshi_price(row, "yes_bid"), 0.61)

    def test_legacy_only_row(self):
        self.assertEqual(s.kalshi_price({"yes_bid": 61}, "yes_bid"), 0.61)


class TestBook(unittest.TestCase):
    def test_walk_across_levels(self):
        book = s.Book(yes_asks=[(0.40, 50), (0.45, 100)])
        # 50 @ .40 + 50 @ .45
        self.assertAlmostEqual(book.cost_to_buy("yes", 100), 50 * 0.40 + 50 * 0.45)

    def test_thin_book_returns_none(self):
        book = s.Book(yes_asks=[(0.40, 50)])
        self.assertIsNone(book.cost_to_buy("yes", 100))

    def test_kalshi_book_inverts_bids(self):
        # A NO bid at 55c is a YES ask at 45c; a YES bid at 30c is a NO ask at 70c.
        original = s.http_get_json
        s.http_get_json = lambda url, **kw: {
            "orderbook": {"yes": [[30, 10]], "no": [[55, 20]]}}
        try:
            book = s.kalshi_book("TEST")
        finally:
            s.http_get_json = original
        self.assertEqual(book.yes_asks, [(0.45, 20.0)])
        self.assertEqual(book.no_asks, [(0.70, 10.0)])

    def test_kalshi_book_dollar_levels(self):
        original = s.http_get_json
        s.http_get_json = lambda url, **kw: {
            "orderbook": {"yes": [], "no": [{"price_dollars": "0.55", "size": 20}]}}
        try:
            book = s.kalshi_book("TEST")
        finally:
            s.http_get_json = original
        self.assertEqual(book.yes_asks, [(0.45, 20.0)])


class TestFees(unittest.TestCase):
    def test_kalshi_rounds_up_per_order(self):
        # 0.07 * 3 * 0.45 * 0.55 = 0.051975 -> rounds up to 6 cents
        self.assertEqual(s.kalshi_fee_total(0.45, 3, 0.07), 0.06)

    def test_poly_no_roundup(self):
        self.assertAlmostEqual(s.poly_fee_total(0.45, 3, 0.04),
                               0.04 * 3 * 0.45 * 0.55, places=5)

    def test_category_coefficients(self):
        geo = make_market(venue="polymarket", category="Geopolitics")
        crypto = make_market(venue="polymarket", category="Crypto")
        self.assertEqual(geo.fee_coeff({}), 0.0)
        self.assertEqual(crypto.fee_coeff({}), 0.07)
        self.assertEqual(crypto.fee_coeff({"polymarket": 0.01}), 0.01)


class TestPolymarketAdapter(unittest.TestCase):
    BASE = {
        "conditionId": "0x1", "slug": "test", "question": "Will X happen?",
        "description": "rules", "outcomes": '["Yes", "No"]',
        "outcomePrices": '["0.60", "0.40"]', "bestBid": 0.59, "bestAsk": 0.61,
        "volumeNum": 10000, "endDate": SOON.isoformat(),
    }

    def test_yes_no_market_parses(self):
        m = s.polymarket_to_market(dict(self.BASE), s.Counter())
        self.assertEqual(m.yes_ask, 0.61)
        self.assertEqual(m.last, 0.60)

    def test_flipped_outcome_order(self):
        row = dict(self.BASE, outcomes='["No", "Yes"]', outcomePrices='["0.40", "0.60"]')
        m = s.polymarket_to_market(row, s.Counter())
        self.assertEqual(m.last, 0.60)                      # YES price, not NO
        self.assertAlmostEqual(m.yes_bid, 1 - 0.61)         # complement flip
        self.assertAlmostEqual(m.yes_ask, 1 - 0.59)

    def test_non_yes_no_binary_refused(self):
        reject = s.Counter()
        row = dict(self.BASE, outcomes='["Trump", "Newsom"]')
        self.assertIsNone(s.polymarket_to_market(row, reject))
        self.assertEqual(reject["poly: binary but not yes/no labelled"], 1)

    def test_disabled_book_refused(self):
        reject = s.Counter()
        row = dict(self.BASE, enableOrderBook=False)
        self.assertIsNone(s.polymarket_to_market(row, reject))

    def test_multi_outcome_refused(self):
        reject = s.Counter()
        row = dict(self.BASE, outcomes='["A", "B", "C"]')
        self.assertIsNone(s.polymarket_to_market(row, reject))


class TestGates(unittest.TestCase):
    def test_missing_rules_skip_the_floor(self):
        kept, _ = s.passes_gates(0.9, None, 0.02, 0.45, 0.35, 0.10)
        self.assertTrue(kept)

    def test_measured_zero_fails_the_floor(self):
        # Regression: `if rules_sim` treated a genuine 0.0 as unknown.
        kept, reason = s.passes_gates(0.9, 0.0, 0.02, 0.45, 0.35, 0.10)
        self.assertFalse(kept)
        self.assertEqual(reason, "rule texts disagree")

    def test_implausible_edge_fails(self):
        kept, _ = s.passes_gates(0.9, 0.9, 0.50, 0.45, 0.35, 0.10)
        self.assertFalse(kept)


class TestPricePairReset(unittest.TestCase):
    def test_depth_pass_clears_stale_implausible_flag(self):
        """Regression: implausible stuck True after real books arrived."""
        k = make_market("kalshi", yes_ask=0.20, no_ask=0.80)
        p = make_market("polymarket", yes_ask=0.80, no_ask=0.20)
        pair = make_pair(k, p)

        s.price_pair(pair, 100, {}, max_plausible=0.10)
        self.assertTrue(pair.implausible)
        self.assertEqual(sum(s.IMPLAUSIBLE_MARK in w for w in pair.warnings), 1)

        # Real depth: only 5 contracts at the silly price, then 49c.
        k.book = s.Book(yes_asks=[(0.20, 5), (0.49, 500)], no_asks=[(0.80, 500)])
        p.book = s.Book(yes_asks=[(0.80, 500)], no_asks=[(0.20, 5), (0.49, 500)])
        s.price_pair(pair, 100, {}, max_plausible=0.10)

        self.assertFalse(pair.implausible)
        self.assertLess(pair.edge, 0.10)
        self.assertGreater(pair.edge, 0.0)
        self.assertEqual(sum(s.IMPLAUSIBLE_MARK in w for w in pair.warnings), 0)

    def test_still_implausible_does_not_stack_warnings(self):
        k = make_market("kalshi", yes_ask=0.20, no_ask=0.80)
        p = make_market("polymarket", yes_ask=0.80, no_ask=0.20)
        pair = make_pair(k, p)
        s.price_pair(pair, 100, {}, max_plausible=0.10)
        s.price_pair(pair, 100, {}, max_plausible=0.10)
        s.price_pair(pair, 100, {}, max_plausible=0.10)
        self.assertTrue(pair.implausible)
        self.assertEqual(sum(s.IMPLAUSIBLE_MARK in w for w in pair.warnings), 1)

    def test_polarity_suspect_gets_no_edge(self):
        k = make_market("kalshi", question="Will the US enter a recession in 2026?")
        p = make_market("polymarket", question="Will the US avoid a recession in 2026?")
        pair = make_pair(k, p, polarity_suspect=True)
        s.price_pair(pair, 100, {})
        self.assertEqual(pair.edge, 0.0)
        self.assertIn("polarity", pair.edge_leg)

    def test_thin_book_reports_no_liquidity_not_stale_price(self):
        k = make_market("kalshi", yes_ask=0.40, no_ask=0.62)
        p = make_market("polymarket", yes_ask=0.40, no_ask=0.55)
        pair = make_pair(k, p)
        s.price_pair(pair, 100, {})
        self.assertGreater(pair.edge, 0.0)

        k.book = s.Book(yes_asks=[(0.40, 5)], no_asks=[(0.62, 5)])   # 5 < 100
        p.book = s.Book(yes_asks=[(0.40, 5)], no_asks=[(0.55, 5)])
        s.price_pair(pair, 100, {})
        self.assertEqual(pair.edge, 0.0)
        self.assertIsNone(pair.cost)
        self.assertEqual(pair.edge_leg, "no two-sided liquidity at requested size")


class TestLadders(unittest.TestCase):
    def _ladder(self):
        a = make_market(question="Will Letitia James be arrested before Jan 2027?",
                        group_key="KXARREST")
        b = make_market(question="Will Chuck Schumer be arrested before Jan 2027?",
                        group_key="KXARREST")
        s.mark_ladders([a, b])
        return a, b

    def test_distinguishing_tokens(self):
        a, b = self._ladder()
        self.assertEqual(a.ladder_size, 2)
        self.assertIn("letitia", a.distinguishing)
        self.assertNotIn("arrested", a.distinguishing)

    def test_mismatch_blocks_wrong_person(self):
        a, _ = self._ladder()
        obama = make_market(venue="polymarket", question="Obama arrested before 2027?")
        self.assertTrue(s.ladder_mismatch(a, obama))

    def test_right_person_passes(self):
        a, _ = self._ladder()
        james = make_market(venue="polymarket",
                            question="Letitia James arrested before 2027?")
        self.assertFalse(s.ladder_mismatch(a, james))


class TestPolymarketPagination(unittest.TestCase):
    def test_stalled_cursor_stops(self):
        calls = []

        def fake(url, **kw):
            calls.append(url)
            return {"markets": [{"id": f"m{len(calls)}", "volumeNum": 1}],
                    "next_cursor": "SAME"}

        original = s.http_get_json
        s.http_get_json = fake
        try:
            rows = s.fetch_polymarket(1000, 0, 0, verbose=False)
        finally:
            s.http_get_json = original
        # Page 1 gets cursor SAME; page 2 sends SAME and gets SAME back -> stop.
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(rows), 2)

    def test_duplicate_page_stops(self):
        def fake(url, **kw):
            return {"markets": [{"id": "always-the-same", "volumeNum": 1}],
                    "next_cursor": f"c{id(url)}"}

        original = s.http_get_json
        s.http_get_json = fake
        try:
            rows = s.fetch_polymarket(1000, 0, 0, verbose=False)
        finally:
            s.http_get_json = original
        self.assertEqual(len(rows), 1)


class TestScorePair(unittest.TestCase):
    def test_missing_rules_marks_pair_unknown(self):
        k = make_market(question="Will X win the cup in 2026?", rules="")
        p = make_market(venue="polymarket", question="Will X win the cup in 2026?")
        for m in (k, p):
            m.vector = s.tfidf_vector(m.tokens, {})
            m.rule_vector = s.tfidf_vector(m.rule_tokens, {})
        pair = s.score_pair(k, p, (0.55, 0.28, 0.17))
        self.assertFalse(pair.rules_known)
        self.assertTrue(any("rules not compared" in w for w in pair.warnings))

    def test_best_possible_is_a_ceiling(self):
        weights = (0.55, 0.28, 0.17)
        questions = [
            ("Will the Fed cut rates in September 2026?", "Fed cuts rates September 2026?"),
            ("Will Bitcoin be above $150,000 on December 31, 2026?", "Will ETH hit $10k?"),
            ("Will Arsenal win the league?", "Will Arsenal win the 2026-27 Premier League?"),
        ]
        for qk, qp in questions:
            k = make_market(question=qk, rules="")
            p = make_market(venue="polymarket", question=qp, rules="")
            docs = [k.tokens, p.tokens]
            idf = s.build_idf(docs)
            for m in (k, p):
                m.vector = s.tfidf_vector(m.tokens, idf)
                m.rule_vector = {}
            pair = s.score_pair(k, p, weights)
            ceiling = s.best_possible(k, p, weights)
            # The prefilter's contract: never skip a pair whose final
            # confidence could clear the threshold. (Not confidence+penalty:
            # confidence clamps at 0, so adding the penalty back can exceed
            # the pre-penalty base.)
            self.assertGreaterEqual(ceiling + 1e-9, pair.confidence,
                                    f"ceiling breached for {qk!r} / {qp!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
