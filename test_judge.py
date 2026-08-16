#!/usr/bin/env python3
"""Offline tests for judge.py — no network, no API key, no spend.

Every test stubs judge.call_api, so nothing is ever sent to Anthropic.
Run: python test_judge.py
"""

import json
import os
import tempfile
import unittest

import judge


def fake_api_response(verdict: dict, stop="end_turn", model="claude-opus-5",
                      with_text=True):
    """Shape a Messages API response the way parse_response expects."""
    content = []
    content.append({"type": "thinking", "thinking": ""})  # Opus 5 emits these first
    if with_text:
        content.append({"type": "text", "text": json.dumps(verdict)})
    return {"content": content, "stop_reason": stop, "model": model,
            "usage": {"input_tokens": 2400, "output_tokens": 300}}


GOOD_VERDICT = {
    "verdict": "different", "confidence": 0.82,
    "resolution_source": "same", "timing": "same",
    "threshold_and_polarity": "differs",
    "divergence": "Kalshi requires the formal head of state; Polymarket resolves "
                  "on de-facto control.",
    "reasoning": "Same person and date, but the two resolve on different offices.",
}


def a_pair(kq="Will X be head of state of Y on Dec 31?",
           pq="Will X be the leader of Y end of year?"):
    return {
        "kalshi": {"question": kq, "category": "world", "settle_time": "2026-12-31",
                   "rules": "Resolves YES if X is the head of state on that date."},
        "polymarket": {"question": pq, "category": "world", "settle_time": "2026-12-31",
                       "rules": "Resolves YES if X is the leader at year end."},
    }


class TestPromptBuilding(unittest.TestCase):
    def test_user_content_includes_both_sides_and_rules(self):
        text = judge.build_user_content(a_pair())
        self.assertIn("KALSHI", text)
        self.assertIn("POLYMARKET", text)
        self.assertIn("head of state", text)
        self.assertIn("leader", text)

    def test_missing_rules_are_labelled_not_blank(self):
        pair = {"kalshi": {"question": "q"}, "polymarket": {"question": "q"}}
        text = judge.build_user_content(pair)
        self.assertIn("no resolution text provided", text)

    def test_rules_are_capped(self):
        big = {"kalshi": {"question": "q", "rules": "x" * 10000},
               "polymarket": {"question": "q", "rules": "y" * 10000}}
        text = judge.build_user_content(big)
        self.assertLess(len(text), 2 * judge.RULES_CHAR_CAP + 500)

    def test_payload_requests_structured_output_and_caches_system(self):
        captured = {}

        def stub(payload, api_key, **kw):
            captured.update(payload)
            return fake_api_response(GOOD_VERDICT)

        original = judge.call_api
        judge.call_api = stub
        try:
            judge.judge_pair(a_pair(), "sk-test", "claude-opus-5", 6000)
        finally:
            judge.call_api = original
        self.assertEqual(captured["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(captured["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(captured["model"], "claude-opus-5")


class TestParsing(unittest.TestCase):
    def test_parses_a_clean_verdict(self):
        v = judge.parse_response(fake_api_response(GOOD_VERDICT))
        self.assertEqual(v["verdict"], "different")
        self.assertEqual(v["model"], "claude-opus-5")
        self.assertIn("head of state", v["divergence"])

    def test_refusal_is_an_error_not_a_crash(self):
        v = judge.parse_response({"content": [], "stop_reason": "refusal"})
        self.assertIn("error", v)
        self.assertIn("refus", v["error"])

    def test_truncation_is_reported(self):
        v = judge.parse_response(fake_api_response(GOOD_VERDICT, stop="max_tokens",
                                                   with_text=False))
        self.assertIn("error", v)
        self.assertIn("truncat", v["error"])

    def test_bad_json_is_caught(self):
        resp = {"content": [{"type": "text", "text": "not json"}],
                "stop_reason": "end_turn"}
        v = judge.parse_response(resp)
        self.assertIn("error", v)


class TestJudgePairErrorHandling(unittest.TestCase):
    def test_http_failure_returns_error_not_exception(self):
        def boom(payload, api_key, **kw):
            raise judge.JudgeError("HTTP 401: invalid x-api-key")

        original = judge.call_api
        judge.call_api = boom
        try:
            v = judge.judge_pair(a_pair(), "sk-bad", "claude-opus-5", 6000)
        finally:
            judge.call_api = original
        self.assertIn("error", v)
        self.assertIn("401", v["error"])


class TestRun(unittest.TestCase):
    def test_run_writes_verdicts_and_survives_a_bad_pair(self):
        pairs = [a_pair(kq=f"q{i}", pq=f"q{i}") for i in range(3)]
        calls = {"n": 0}

        def stub(payload, api_key, **kw):
            calls["n"] += 1
            if calls["n"] == 2:                      # second pair errors
                raise judge.JudgeError("HTTP 529: overloaded")
            return fake_api_response(GOOD_VERDICT)

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "latest.json")
            out = os.path.join(d, "judged.json")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump(pairs, fh)

            args = judge.build_parser().parse_args(
                ["run", "--source", src, "--out", out, "-c", "3"])
            original = judge.call_api
            judge.call_api = stub
            try:
                rc = judge.cmd_run(args, "sk-test")
            finally:
                judge.call_api = original

            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as fh:
                judged = json.load(fh)
        self.assertEqual(len(judged), 3)                       # all three written
        self.assertIn("error", judged[1]["llm_verdict"])       # the bad one flagged
        self.assertEqual(judged[0]["llm_verdict"]["verdict"], "different")

    def test_count_limits_the_shortlist(self):
        pairs = [a_pair(kq=f"q{i}", pq=f"q{i}") for i in range(20)]
        seen = {"n": 0}

        def stub(payload, api_key, **kw):
            seen["n"] += 1
            return fake_api_response(GOOD_VERDICT)

        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "latest.json")
            with open(src, "w", encoding="utf-8") as fh:
                json.dump(pairs, fh)
            args = judge.build_parser().parse_args(
                ["run", "--source", src, "--out", os.path.join(d, "o.json"), "-c", "5"])
            original = judge.call_api
            judge.call_api = stub
            try:
                judge.cmd_run(args, "sk-test")
            finally:
                judge.call_api = original
        self.assertEqual(seen["n"], 5)                         # only the top 5 judged


class TestReviewCore(unittest.TestCase):
    """review_pairs is the shared core the CLI and the server both call."""

    def _run(self, pairs, count=0):
        seen = []
        original = judge.call_api
        judge.call_api = lambda payload, api_key, **kw: fake_api_response(GOOD_VERDICT)
        try:
            judged = judge.review_pairs(pairs, "sk-test", count=count,
                                        progress=lambda i, n, p: seen.append((i, n)))
        finally:
            judge.call_api = original
        return judged, seen

    def test_count_zero_reviews_all(self):
        judged, seen = self._run([a_pair(f"P{i}", f"P{i}") for i in range(6)], count=0)
        self.assertEqual(len(judged), 6)
        self.assertEqual(seen[-1], (6, 6))
        self.assertEqual(judged[0]["llm_verdict"]["verdict"], "different")

    def test_count_caps_the_shortlist(self):
        judged, seen = self._run([a_pair(f"P{i}", f"P{i}") for i in range(6)], count=2)
        self.assertEqual(len(judged), 2)
        self.assertEqual(seen, [(1, 2), (2, 2)])

    def test_write_judged_drops_internal_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "judged.json")
            judge.write_judged(path, [dict(a_pair("A", "A"),
                llm_verdict={"verdict": "equivalent", "_tokens": {"input": 1}})])
            with open(path, encoding="utf-8") as fh:
                out = json.load(fh)
        self.assertNotIn("_tokens", out[0]["llm_verdict"])
        self.assertEqual(out[0]["llm_verdict"]["verdict"], "equivalent")


class TestKeyHandling(unittest.TestCase):
    def test_missing_key_skips_cleanly(self):
        saved = os.environ.pop("ANTHROPIC_API_KEY", None)
        try:
            rc = judge.main(["run"])
        finally:
            if saved is not None:
                os.environ["ANTHROPIC_API_KEY"] = saved
        self.assertEqual(rc, 0)                                # skip, don't fail

    def test_blank_key_is_treated_as_missing(self):
        saved = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "   "
        try:
            self.assertIsNone(judge.get_api_key())
        finally:
            if saved is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
