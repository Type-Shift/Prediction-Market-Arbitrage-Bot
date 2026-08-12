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


if __name__ == "__main__":
    unittest.main(verbosity=2)
