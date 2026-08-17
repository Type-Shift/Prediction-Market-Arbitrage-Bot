#!/usr/bin/env python3
"""Offline tests for app.py. No network required: python test_app.py

The server is exercised over a real socket rather than by calling handler
methods directly -- the parts most likely to break (SSE framing, status codes,
the static fallthrough) only exist at the HTTP layer.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

import allocator as a
import app
import bot as b
import judge
import scanner as s

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


class ServerCase(unittest.TestCase):
    """A live server on an ephemeral port, scanning from fixtures/.

    Every path the server writes is redirected into a temp directory first, so
    a test run cannot overwrite the committed results/ or the real portfolio.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

        # No accidental spend: a scan only reviews with the model when a key is
        # present, so every test that is not about that runs without one.
        self._saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        self.addCleanup(self._restore_key)

        # No accidental dispatch: a stray ARB_REMOTE_SCAN in the environment
        # must not turn these local-scan tests into GitHub Actions runs.
        self._saved_remote = os.environ.pop("ARB_REMOTE_SCAN", None)
        self.addCleanup(self._restore_remote)

        self._saved = (app.RESULTS_DIR, a.PAPER_PATH, b.BOT_PATH,
                       s.LAST_RUN_PATH, s.LABELS_PATH)
        app.RESULTS_DIR = os.path.join(self.dir, "results")
        a.PAPER_PATH = os.path.join(self.dir, "paper.json")
        b.BOT_PATH = os.path.join(self.dir, "bot.json")
        s.LAST_RUN_PATH = os.path.join(self.dir, "last_run.json")
        s.LABELS_PATH = os.path.join(self.dir, "labels.json")
        os.makedirs(app.RESULTS_DIR, exist_ok=True)
        self.addCleanup(self._restore)

        # --depth 0 matters: --offline only replaces the *market list*, it does
        # not stop the depth pass from fetching real order books over the
        # network. Left on, these tests would touch the internet and their
        # results would depend on what two live venues happened to be quoting.
        self.httpd = app.serve("127.0.0.1", 0,
                               ["--offline", FIXTURES, "--depth", "0",
                                "--cache-dir", os.path.join(self.dir, "cache")])
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)

    def _restore(self):
        (app.RESULTS_DIR, a.PAPER_PATH, b.BOT_PATH,
         s.LAST_RUN_PATH, s.LABELS_PATH) = self._saved

    def _restore_key(self):
        if self._saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._saved_key
        else:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def _restore_remote(self):
        if self._saved_remote is not None:
            os.environ["ARB_REMOTE_SCAN"] = self._saved_remote
        else:
            os.environ.pop("ARB_REMOTE_SCAN", None)

    # -- helpers -----------------------------------------------------------

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as res:
            return res.status, json.loads(res.read().decode("utf-8"))

    def post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def wait_for_idle(self, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, state = self.get("/api/state")
            if not state["run"]["running"]:
                return state
            time.sleep(0.1)
        self.fail("scan did not finish in time")

    def scan_and_wait(self, timeout=60):
        self.wait_for_idle(timeout)  # a previous scan may still be finishing
        status, _ = self.post("/api/scan", {})
        self.assertEqual(status, 200)
        return self.wait_for_idle(timeout)


class TestProbeAndState(ServerCase):
    def test_ping_is_what_the_page_probes(self):
        status, body = self.get("/api/ping")
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])

    def test_state_carries_every_section_in_one_request(self):
        _, state = self.get("/api/state")
        for key in ("live", "pairs", "meta", "history", "labels", "run", "bot"):
            self.assertIn(key, state)
        self.assertIsInstance(state["pairs"], list)
        self.assertIn("running", state["bot"])

    def test_unknown_api_route_is_a_404_not_a_static_lookup(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/api/nope")
        self.assertEqual(caught.exception.code, 404)


class TestStatic(ServerCase):
    def test_the_dashboard_is_still_served(self):
        with urllib.request.urlopen(self.base + "/dashboard/index.html",
                                    timeout=10) as res:
            body = res.read().decode("utf-8")
        self.assertEqual(res.status, 200)
        self.assertIn("view-bot", body)
        self.assertIn("live.js", body)

    def test_root_redirects_to_the_dashboard(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as res:
            self.assertTrue(res.url.endswith("/dashboard/"))


class TestScanning(ServerCase):
    def test_a_scan_produces_pairs_and_the_usual_files(self):
        state = self.scan_and_wait()
        self.assertIsNone(state["run"]["last_error"])
        self.assertTrue(state["pairs"])
        self.assertTrue(state["meta"]["generated_at"])
        # The server is not a second source of truth: it writes what the CLI
        # writes, which is what keeps the static fallback fed.
        for name in ("latest.json", "latest.csv", "latest.txt", "meta.json",
                     "last_run_time.txt"):
            self.assertTrue(os.path.exists(os.path.join(app.RESULTS_DIR, name)), name)
        self.assertTrue(os.path.exists(
            os.path.join(app.RESULTS_DIR, "history", "index.json")))

    def test_progress_reaches_the_log(self):
        state = self.scan_and_wait()
        joined = "\n".join(state["run"]["log"])
        self.assertIn("scoring", joined)

    def test_a_second_scan_while_one_runs_is_refused(self):
        first, _ = self.post("/api/scan", {})
        self.assertEqual(first, 200)
        # Whether the first is still running is a race, so accept either the
        # refusal or a clean start -- but never a crash, and never two at once.
        second, body = self.post("/api/scan", {})
        self.assertIn(second, (200, 409))
        if second == 409:
            self.assertIn("already running", body["message"])
        self.scan_and_wait()

    def test_malformed_body_is_a_400(self):
        req = urllib.request.Request(
            self.base + "/api/scan", data=b"{not json",
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(caught.exception.code, 400)


class TestEvents(ServerCase):
    def test_the_stream_frames_events_and_reports_a_run(self):
        stream = urllib.request.urlopen(self.base + "/api/events", timeout=30)
        self.addCleanup(stream.close)

        first = stream.readline().decode("utf-8")
        self.assertEqual(first.strip(), "event: hello")
        self.assertTrue(stream.readline().decode("utf-8").startswith("data: "))

        self.post("/api/scan", {})
        seen = set()
        deadline = time.time() + 60
        while time.time() < deadline and "run-done" not in seen:
            line = stream.readline().decode("utf-8").strip()
            if line.startswith("event: "):
                seen.add(line.split(": ", 1)[1])
        self.assertIn("run-start", seen)
        self.assertIn("progress", seen)
        self.assertIn("run-done", seen)


class TestBotEndpoint(ServerCase):
    def test_start_and_pause_round_trip(self):
        status, snap = self.post("/api/bot", {"action": "start"})
        self.assertEqual(status, 200)
        self.assertTrue(snap["running"])
        _, snap = self.post("/api/bot", {"action": "pause"})
        self.assertFalse(snap["running"])

    def test_config_is_validated_at_the_boundary(self):
        _, snap = self.post("/api/bot", {"action": "config",
                                         "config": {"positions": "4",
                                                    "bankroll": "junk",
                                                    "nonsense": 1}})
        self.assertEqual(snap["config"]["positions"], 4)
        self.assertEqual(snap["config"]["bankroll"], b.DEFAULT_CONFIG["bankroll"])
        self.assertNotIn("nonsense", snap["config"])

    def test_unknown_action_is_a_400(self):
        status, body = self.post("/api/bot", {"action": "sell everything"})
        self.assertEqual(status, 400)
        self.assertIn("unknown action", body["error"])

    def test_a_running_bot_trades_the_scan(self):
        self.post("/api/bot", {"action": "start",
                               "config": {"min_confidence": 0.3, "min_edge": 0.0,
                                          "require_rules_match": False}})
        self.scan_and_wait()
        _, snap = self.get("/api/bot")
        self.assertTrue(snap["last_cycle"], "the bot should have run a cycle")
        self.assertTrue(snap["cycle_log"])

    def test_a_paused_bot_leaves_the_portfolio_alone(self):
        self.scan_and_wait()
        _, snap = self.get("/api/bot")
        self.assertEqual(snap["open"], [])
        self.assertIsNone(snap["last_cycle"])

    def test_reset_clears_the_history(self):
        self.post("/api/bot", {"action": "start",
                               "config": {"min_confidence": 0.3, "min_edge": 0.0,
                                          "require_rules_match": False}})
        self.scan_and_wait()
        _, snap = self.post("/api/bot", {"action": "reset",
                                         "config": {"bankroll": 500.0}})
        self.assertEqual(snap["cash"], 500.0)
        self.assertEqual(snap["open"], [])
        # The old curve is gone, replaced by a single point marking the reset
        # so the new curve starts from the new bankroll rather than nowhere.
        self.assertEqual(len(snap["cycle_log"]), 1)
        self.assertEqual(snap["cycle_log"][0]["notes"], ["portfolio reset"])
        self.assertEqual(snap["cycle_log"][0]["equity"], 500.0)


class TestLabels(ServerCase):
    def test_labels_are_written_to_the_file_score_reads(self):
        rows = [{"kalshi_id": "K1", "poly_url": "https://poly/1", "label": "match",
                 "kalshi_question": "q", "poly_question": "p", "confidence": 0.8,
                 "rules_similarity": 0.6, "edge": 0.02, "note": ""}]
        status, body = self.post("/api/labels", {"labels": rows})
        self.assertEqual(status, 200)
        self.assertEqual(body["saved"], 1)
        with open(s.LABELS_PATH, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)[0]["kalshi_id"], "K1")

    def test_a_non_list_payload_is_refused(self):
        status, body = self.post("/api/labels", {"labels": {"nope": True}})
        self.assertEqual(status, 400)
        self.assertIn("expected a list", body["error"])


class TestVerdictMerge(unittest.TestCase):
    """with_verdicts joins results/judged.json onto records by pair identity."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._saved = app.RESULTS_DIR
        app.RESULTS_DIR = os.path.join(self.dir, "results")
        os.makedirs(app.RESULTS_DIR, exist_ok=True)
        self.addCleanup(lambda: setattr(app, "RESULTS_DIR", self._saved))

    @staticmethod
    def _record(kid, url):
        return {"kalshi": {"id": kid}, "polymarket": {"url": url}, "edge": 0.02}

    def _write_judged(self, rows):
        with open(os.path.join(app.RESULTS_DIR, "judged.json"), "w", encoding="utf-8") as fh:
            json.dump(rows, fh)

    def test_no_file_leaves_records_untouched(self):
        recs = [self._record("K1", "U1")]
        out = app.with_verdicts(recs)
        self.assertIs(out, recs)
        self.assertNotIn("llm_verdict", out[0])

    def test_verdict_merged_by_identity_unmatched_is_none(self):
        self._write_judged([dict(self._record("K1", "U1"),
                                 llm_verdict={"verdict": "different", "confidence": 0.9})])
        out = app.with_verdicts([self._record("K1", "U1"), self._record("K2", "U2")])
        self.assertEqual(out[0]["llm_verdict"]["verdict"], "different")
        self.assertIsNone(out[1]["llm_verdict"])  # present but empty, not silently absent

    def test_merge_does_not_mutate_the_shared_records(self):
        self._write_judged([dict(self._record("K1", "U1"),
                                 llm_verdict={"verdict": "equivalent"})])
        recs = [self._record("K1", "U1")]
        app.with_verdicts(recs)
        self.assertNotIn("llm_verdict", recs[0])  # the bot's copy stays clean

    def test_pair_without_verdict_key_is_ignored(self):
        self._write_judged([{"kalshi": {"id": "K1"}, "polymarket": {"url": "U1"}}])  # no llm_verdict
        out = app.with_verdicts([self._record("K1", "U1")])
        self.assertNotIn("llm_verdict", out[0])  # empty map → passthrough, untouched


class TestVerdictInState(ServerCase):
    def test_state_merges_verdicts_from_judged_json(self):
        state = self.scan_and_wait()
        self.assertTrue(state["pairs"], "need at least one pair to tag")
        target = state["pairs"][0]
        judged = [{
            "kalshi": {"id": target["kalshi"]["id"]},
            "polymarket": {"url": target["polymarket"]["url"]},
            "llm_verdict": {"verdict": "equivalent", "confidence": 0.88,
                            "divergence": "none found"},
        }]
        with open(os.path.join(app.RESULTS_DIR, "judged.json"), "w", encoding="utf-8") as fh:
            json.dump(judged, fh)

        _, state = self.get("/api/state")
        tagged = next(p for p in state["pairs"]
                      if p["kalshi"]["id"] == target["kalshi"]["id"])
        self.assertEqual(tagged["llm_verdict"]["verdict"], "equivalent")
        # A different pair carries the key as None, not missing.
        others = [p for p in state["pairs"]
                  if p["kalshi"]["id"] != target["kalshi"]["id"]]
        if others:
            self.assertIsNone(others[0]["llm_verdict"])


class TestJudgeOnScan(ServerCase):
    """A key in the environment makes a scan review its own results.

    judge.call_api is stubbed, so this exercises the whole scan → review →
    /api/state path without any network call or spend.
    """

    def setUp(self):
        super().setUp()  # clears any real key first
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-not-real"
        self.addCleanup(os.environ.pop, "ANTHROPIC_API_KEY", None)
        self._orig_call = judge.call_api
        judge.call_api = self._fake_call
        self.addCleanup(lambda: setattr(judge, "call_api", self._orig_call))

    @staticmethod
    def _fake_call(payload, api_key, **kw):
        return {
            "content": [{"type": "text", "text": json.dumps({
                "verdict": "likely_equivalent", "confidence": 0.7,
                "resolution_source": "same", "timing": "same",
                "threshold_and_polarity": "same", "divergence": "none found",
                "reasoning": "stub"})}],
            "stop_reason": "end_turn", "model": "claude-opus-5",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    def test_state_reports_the_judge_is_enabled(self):
        _, state = self.get("/api/state")
        self.assertTrue(state["judge"]["enabled"])
        self.assertTrue(state["judge"]["model"])

    def wait_for_verdicts(self, timeout=30):
        """The review runs on its own thread after the scan is marked idle, so
        poll /api/state until the verdicts land rather than reading once."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            _, state = self.get("/api/state")
            if state["pairs"] and all(p.get("llm_verdict") for p in state["pairs"]):
                return state
            time.sleep(0.1)
        self.fail("verdicts did not reach /api/state in time")

    def test_a_scan_reviews_its_pairs_and_writes_judged(self):
        self.scan_and_wait()
        state = self.wait_for_verdicts()
        self.assertTrue(os.path.exists(os.path.join(app.RESULTS_DIR, "judged.json")))
        self.assertTrue(state["pairs"])
        self.assertEqual(state["pairs"][0]["llm_verdict"]["verdict"], "likely_equivalent")


class TestRemoteScan(ServerCase):
    """A remote scan dispatches the workflow and pulls the result back.

    gh and git are stubbed through app.run_cmd, so this exercises the whole
    dispatch -> poll -> pull -> /api/state path with nothing leaving the machine
    and no dependency on a real login.
    """

    def setUp(self):
        super().setUp()  # clears any stray ARB_REMOTE_SCAN first
        os.environ["ARB_REMOTE_SCAN"] = "1"
        self.addCleanup(os.environ.pop, "ARB_REMOTE_SCAN", None)
        self._orig_cmd, self._orig_gh = app.run_cmd, app.gh_path
        app.gh_path = lambda: "C:/fake/gh.exe"
        app.run_cmd = self._fake_cmd
        self.addCleanup(lambda: setattr(app, "run_cmd", self._orig_cmd))
        self.addCleanup(lambda: setattr(app, "gh_path", self._orig_gh))
        self._list_calls = 0
        self.conclusion = "success"

    def _fake_cmd(self, args, timeout=60):
        head = args[:3]
        if head == ["gh", "workflow", "run"]:
            return (0, "", "")
        if head == ["gh", "run", "list"]:
            self._list_calls += 1
            # The first list (before dispatch) is the prior "latest" id; after
            # that a new run id appears, which is the one we dispatched.
            rid = 100 if self._list_calls == 1 else 101
            return (0, json.dumps([{"databaseId": rid, "status": "queued",
                                    "conclusion": None, "url": f"https://gh/{rid}",
                                    "createdAt": "2026-08-17T00:00:00Z"}]), "")
        if head == ["gh", "run", "view"]:
            return (0, json.dumps({"status": "completed",
                                   "conclusion": self.conclusion,
                                   "url": "https://gh/101"}), "")
        if args[:2] == ["git", "-C"]:
            # The workflow commits results; a pull/checkout lands them on disk.
            if "pull" in args or "checkout" in args:
                self._write_results()
            return (0, "", "")
        return (0, "", "")

    def _write_results(self):
        rec = [{"kalshi": {"id": "RK1"}, "polymarket": {"url": "https://poly/rk1"},
                "edge": 0.05, "annualised_edge": 0.2}]
        with open(os.path.join(app.RESULTS_DIR, "latest.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(rec, fh)
        with open(os.path.join(app.RESULTS_DIR, "meta.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"generated_at": "2026-08-17T00:00:00Z"}, fh)

    def test_state_reports_remote_default(self):
        _, state = self.get("/api/state")
        self.assertTrue(state["remote"]["default"])
        self.assertTrue(state["remote"]["available"])

    def test_remote_scan_dispatches_and_pulls_results(self):
        state = self.scan_and_wait()
        self.assertIsNone(state["run"]["last_error"])
        self.assertTrue(state["pairs"])
        self.assertEqual(state["pairs"][0]["kalshi"]["id"], "RK1")

    def test_a_failed_run_surfaces_as_an_error(self):
        self.conclusion = "failure"
        state = self.scan_and_wait()
        self.assertIsNotNone(state["run"]["last_error"])
        self.assertIn("failure", state["run"]["last_error"])

    def test_gh_missing_is_a_clear_refusal(self):
        app.gh_path = lambda: None
        status, body = self.post("/api/scan", {})   # remote default, gh gone
        self.assertEqual(status, 409)
        self.assertIn("gh", body["message"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
