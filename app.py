#!/usr/bin/env python3
"""One app: the dashboard, the scanner and the bot in a single process.

    python app.py                 # then open http://localhost:8000
    python app.py --offline fixtures/   # no network, for a demo or a test

Until now the three parts of this repo did not know about each other. The
scanner was a command that wrote JSON and exited; the dashboard read that JSON
once when the page loaded and had no way to ask for more; the allocator was a
second command you ran by hand afterwards. The loop was: run a command, refresh
a browser tab, run another command, refresh again.

This serves the dashboard *and* exposes the scanner and the bot behind it, so
the browser can start a scan, watch it happen line by line, and see the bot
trade the result -- without a reload and without a terminal.

Standard library only, like the rest of the project. That constraint is not
aesthetic: CI has no dependency-install step, and the whole point of the
GitHub Actions path is that it runs anywhere with a bare Python.

The static path still works. Serve this directory with any web server (or open
the dashboard off GitHub Pages) and the page falls back to reading whatever is
committed in results/, read-only, exactly as before. `/api/ping` is what the
page probes to tell the two apart.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import queue
import sys
import threading
import time
import traceback
import webbrowser
from collections import Counter
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import allocator
import bot
import scanner

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(HERE, "results")

# Long enough that an idle browser does not reconnect constantly, short enough
# that a proxy holding the connection open does not decide it is dead.
HEARTBEAT_SECONDS = 15.0

# A slow consumer must not be able to grow the server's memory without bound.
# Progress lines are the disposable part of the stream: if a subscriber falls
# this far behind, its oldest lines are dropped rather than queued forever.
SUBSCRIBER_BACKLOG = 1000


# ---------------------------------------------------------------------------
# Event bus
#
# One queue per connected browser. Publishers never block on a subscriber, so
# a stalled reader can slow itself down but not the scan.
# ---------------------------------------------------------------------------

class EventBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_BACKLOG)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: str, data) -> None:
        message = (event, data)
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(message)
            except queue.Full:
                # Drop the oldest and retry once. Losing a progress line from a
                # backlogged client is fine; blocking the scanner is not.
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except (queue.Empty, queue.Full):
                    pass


BUS = EventBus()


# ---------------------------------------------------------------------------
# Progress capture
#
# The scanner reports progress by printing to stderr -- no logging module, no
# callback hook. Rather than thread a callback through a dozen call sites, the
# stream itself is swapped for the duration of a scan. Every one of those calls
# passes `file=sys.stderr`, which is looked up at call time, so replacing
# sys.stderr catches all of them without touching scanner.py.
#
# The tee still writes through to the real stderr, so running the server in a
# terminal looks exactly like running the CLI.
# ---------------------------------------------------------------------------

class StderrTee(io.TextIOBase):
    def __init__(self, passthrough, publish) -> None:
        self._passthrough = passthrough
        self._publish = publish
        self._buffer = ""

    def write(self, text: str) -> int:
        try:
            self._passthrough.write(text)
        except (OSError, ValueError):
            pass  # a closed or redirected terminal must not kill the scan
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._publish(line.rstrip())
        return len(text)

    def flush(self) -> None:
        # A partial line at the end of a stage is still worth showing.
        if self._buffer.strip():
            self._publish(self._buffer.rstrip())
            self._buffer = ""
        try:
            self._passthrough.flush()
        except (OSError, ValueError):
            pass

    def writable(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# The scan runner
# ---------------------------------------------------------------------------

class Runner:
    """Runs one scan at a time and feeds the results to the bot.

    Strictly one at a time, and a second request is refused rather than queued.
    Three things in the process are global and would corrupt each other if two
    scans overlapped: scanner.RUN_STATS (cleared at the start of every scan),
    the shared cache/ directory, and the paper portfolio the bot writes.
    Refusing is honest; queueing would hide a scan that never seems to start.
    """

    def __init__(self, base_flags: list[str]) -> None:
        self.base_flags = base_flags
        self.lock = threading.Lock()
        self.running = False
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.log: list[str] = []
        self.state = bot.load_state()
        self.records: list[dict] = load_committed_pairs()
        self.meta: dict | None = load_committed_meta()

    # -- state the browser reads ------------------------------------------

    def status(self) -> dict:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "elapsed": round(time.time() - self.started_at, 1) if self.started_at else None,
            "last_error": self.last_error,
            "log": self.log[-200:],
        }

    def bot_snapshot(self) -> dict:
        portfolio = bot.load_portfolio(self.state)
        return bot.snapshot(self.state, portfolio, self.records)

    # -- starting a scan ---------------------------------------------------

    def start(self, flags: list[str]) -> tuple[bool, str]:
        with self.lock:
            if self.running:
                return False, "a scan is already running"
            self.running = True
            self.started_at = time.time()
            self.last_error = None
            self.log = []
        threading.Thread(target=self._run, args=(flags,), daemon=True).start()
        return True, "started"

    def _publish_line(self, line: str) -> None:
        self.log.append(line)
        del self.log[:-500]
        BUS.publish("progress", {"line": line})

    def _run(self, flags: list[str]) -> None:
        BUS.publish("run-start", {"at": iso_now()})
        tee = StderrTee(sys.__stderr__, self._publish_line)
        try:
            args = scanner.build_parser().parse_args(
                ["scan", *self.base_flags, *flags])
            with contextlib.redirect_stderr(tee):
                try:
                    pairs, reject = scanner.scan(args)
                finally:
                    tee.flush()

            records = [scanner.pair_to_dict(p) for p in pairs]
            meta = scanner.run_meta(pairs, reject, args)
            publish_results(pairs, records, meta, args)

            self.records = records
            self.meta = meta
            cycle = self._run_bot(records)
            BUS.publish("run-done", {"meta": meta, "kept": len(records),
                                     "cycle": cycle})

        except scanner.FetchError as exc:
            # The scanner's own failure mode: a venue is unreachable, or a
            # network filter served an HTML block page. Publish the metadata
            # anyway so the page can say the run failed and why, rather than
            # keep showing yesterday's numbers as if they were current.
            self.last_error = str(exc)
            meta = scanner.run_meta([], Counter(),
                                    scanner.build_parser().parse_args(["scan"]),
                                    error=str(exc))
            with contextlib.suppress(OSError):
                scanner.write_meta(os.path.join(RESULTS_DIR, "meta.json"), meta)
            self.meta = meta
            BUS.publish("run-failed", {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - the thread must not die silently
            self.last_error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc(file=sys.__stderr__)
            BUS.publish("run-failed", {"error": self.last_error})
        finally:
            with self.lock:
                self.running = False
                self.started_at = None

    def _run_bot(self, records: list[dict]) -> dict | None:
        if not self.state["running"]:
            return None
        try:
            portfolio = bot.load_portfolio(self.state)
            cycle = bot.run_cycle(records, self.state, portfolio)
            BUS.publish("bot", {"cycle": cycle})
            return cycle
        except Exception as exc:  # noqa: BLE001
            # A broken bot must not make the scan itself look failed -- the
            # scan results are already saved and are worth keeping.
            traceback.print_exc(file=sys.__stderr__)
            BUS.publish("bot-failed", {"error": f"{type(exc).__name__}: {exc}"})
            return None

    # -- bot actions -------------------------------------------------------

    def bot_action(self, action: str, config: dict | None) -> tuple[bool, str]:
        if config:
            self.state["config"].update(bot.clean_config(config))

        if action == "start":
            self.state["running"] = True
        elif action == "pause":
            self.state["running"] = False
        elif action == "config":
            pass  # the update above was the whole request
        elif action == "settle":
            portfolio = bot.load_portfolio(self.state)
            cfg = self.state["config"]
            allocator.settle(portfolio, cfg["mismatch_loss_rate"], cfg["settle_seed"])
            bot.save_portfolio(portfolio)
            self._record(portfolio, "manual settle")
        elif action == "flatten":
            portfolio = bot.load_portfolio(self.state)
            bot.flatten(portfolio)
            self._record(portfolio, "flattened by hand")
        elif action == "reset":
            portfolio = bot.reset(self.state["config"]["bankroll"])
            self.state["cycle_log"] = []
            self.state["last_cycle"] = None
            self._record(portfolio, "portfolio reset")
        elif action == "allocate":
            # Trade the scan already in memory, without waiting for the next
            # one. Useful right after switching the bot on.
            portfolio = bot.load_portfolio(self.state)
            bot.run_cycle(self.records, self.state, portfolio)
        else:
            return False, f"unknown action {action!r}"

        bot.save_state(self.state)
        BUS.publish("bot", {"action": action})
        return True, action

    def _record(self, portfolio: dict, note: str) -> None:
        cycle = bot.summarise(portfolio, self.records, notes=[note])
        self.state["last_cycle"] = cycle
        self.state["cycle_log"].append(cycle)


# ---------------------------------------------------------------------------
# Reading and writing the results directory
# ---------------------------------------------------------------------------

def read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return fallback


def load_committed_pairs() -> list[dict]:
    """Whatever the last scan left behind, so a fresh server is not empty."""
    pairs = read_json(os.path.join(RESULTS_DIR, "latest.json"), None)
    if pairs is None:
        pairs = read_json(scanner.LAST_RUN_PATH, [])
    return pairs if isinstance(pairs, list) else []


def load_committed_meta():
    return read_json(os.path.join(RESULTS_DIR, "meta.json"), None)


def load_history() -> list:
    index = read_json(os.path.join(RESULTS_DIR, "history", "index.json"), [])
    return index if isinstance(index, list) else []


def verdict_key(record: dict) -> str:
    """Pair identity — the same key judge.py, review and score all join on."""
    k = record.get("kalshi") or {}
    p = record.get("polymarket") or {}
    return f"{k.get('id', '')}|{p.get('url', '')}"


def load_verdicts() -> dict:
    """Map pair identity → the model's verdict, from results/judged.json.

    judge.py writes that file after a scan (in CI, or by hand) with each of the
    top pairs carrying an `llm_verdict`. It is optional: with no key configured
    the judge never runs, the file is absent, and this returns {} so the board
    is exactly what it was before the model was ever involved.
    """
    judged = read_json(os.path.join(RESULTS_DIR, "judged.json"), [])
    out: dict = {}
    if isinstance(judged, list):
        for pair in judged:
            if isinstance(pair, dict) and pair.get("llm_verdict"):
                out[verdict_key(pair)] = pair["llm_verdict"]
    return out


def with_verdicts(records: list[dict]) -> list[dict]:
    """Records with each pair's model verdict merged in, keyed by identity.

    A fresh copy per record — the shared list the bot also reads is never
    mutated. Pairs the judge did not review (new since its last run, or beyond
    its count) carry llm_verdict = None, shown as 'unreviewed' rather than
    silently blank. With no judged.json, the records pass through untouched.
    """
    verdicts = load_verdicts()
    if not verdicts:
        return records
    return [{**r, "llm_verdict": verdicts.get(verdict_key(r))} for r in records]


def publish_results(pairs, records: list[dict], meta: dict, args) -> None:
    """Write the same files the CLI writes.

    The server is not a second source of truth. It calls the scanner's own
    writers so that a scan started from the browser leaves results/ in exactly
    the state a scan started from the terminal or from CI would -- which is
    what keeps the static fallback, and the committed history, working.
    """
    os.makedirs(RESULTS_DIR, exist_ok=True)
    path = lambda name: os.path.join(RESULTS_DIR, name)  # noqa: E731

    with contextlib.suppress(OSError):
        with open(scanner.LAST_RUN_PATH, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
    with contextlib.suppress(OSError):
        with open(path("latest.json"), "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
    with contextlib.suppress(OSError):
        scanner.write_csv(path("latest.csv"), pairs)
    with contextlib.suppress(OSError):
        with open(path("latest.txt"), "w", encoding="utf-8") as fh:
            fh.write(scanner.render(pairs, args))
    with contextlib.suppress(OSError):
        scanner.write_meta(path("meta.json"), meta)
    with contextlib.suppress(OSError):
        scanner.write_history(path("history"), records, meta)
    with contextlib.suppress(OSError):
        with open(path("last_run_time.txt"), "w", encoding="utf-8") as fh:
            fh.write(f"last run (UTC): {meta['generated_at']}\n")


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(SimpleHTTPRequestHandler):
    """Static files, plus the /api surface the dashboard's live mode uses."""

    runner: Runner  # set on the class before the server starts
    server_version = "arb-dashboard/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    # -- helpers -----------------------------------------------------------

    def send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Results change while the page is open; a cached /api/state would show
        # the browser a scan it already replaced.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if length <= 0:
            return {}
        # Refuse a body big enough to be a memory problem. Nothing the
        # dashboard posts is anywhere near this.
        if length > 8 * 1024 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def log_message(self, fmt, *args) -> None:
        # The default logs every static asset to stderr, which would drown the
        # scanner's progress in the same terminal.
        pass

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        route = self.path.split("?", 1)[0]

        if route == "/":
            self.send_response(302)
            self.send_header("Location", "/dashboard/")
            self.end_headers()
            return
        if route == "/api/ping":
            return self.send_json({"live": True, "version": self.server_version})
        if route == "/api/state":
            return self.send_json(self.api_state())
        if route == "/api/bot":
            return self.send_json(self.runner.bot_snapshot())
        if route == "/api/events":
            return self.stream_events()
        if route.startswith("/api/"):
            return self.send_json({"error": "no such endpoint"}, 404)

        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        route = self.path.split("?", 1)[0]
        body = self.read_json_body()
        if body is None:
            return self.send_json({"error": "malformed JSON body"}, 400)

        if route == "/api/scan":
            flags = [str(f) for f in (body.get("flags") or [])]
            ok, message = self.runner.start(flags)
            return self.send_json({"ok": ok, "message": message},
                                  200 if ok else 409)

        if route == "/api/bot":
            action = str(body.get("action") or "config")
            try:
                ok, message = self.runner.bot_action(action, body.get("config"))
            except Exception as exc:  # noqa: BLE001
                return self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            if not ok:
                return self.send_json({"error": message}, 400)
            return self.send_json(self.runner.bot_snapshot())

        if route == "/api/labels":
            rows = body.get("labels")
            if not isinstance(rows, list):
                return self.send_json({"error": "expected a list of labels"}, 400)
            try:
                scanner.save_labels(rows)
            except OSError as exc:
                return self.send_json({"error": str(exc)}, 500)
            return self.send_json({"ok": True, "saved": len(rows)})

        return self.send_json({"error": "no such endpoint"}, 404)

    # -- endpoints ---------------------------------------------------------

    def api_state(self) -> dict:
        runner = self.runner
        # Read the run status *before* the results, never after. The scan
        # thread publishes its records and only then clears the running flag,
        # so this order can at worst report "still running" alongside results
        # that are already final -- the next poll corrects it. The other order
        # produces the lie that matters: "finished" next to the previous run's
        # pairs, which a poller reads as a completed scan that found nothing.
        run = runner.status()
        return {
            "live": True,
            "run": run,
            "pairs": with_verdicts(runner.records),
            "meta": runner.meta,
            "history": load_history(),
            "labels": scanner.load_labels(),
            "bot": runner.bot_snapshot(),
        }

    def stream_events(self) -> None:
        """Server-sent events: one long-lived response per open dashboard."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        # Tells nginx and friends not to buffer, which would defeat the point.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q = BUS.subscribe()
        try:
            self.write_event("hello", {"at": iso_now(),
                                       "run": self.runner.status()})
            while True:
                try:
                    event, data = q.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    # A comment line keeps the connection (and any proxy in
                    # front of it) from going quiet long enough to be dropped.
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue
                self.write_event(event, data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass  # the tab was closed or reloaded; nothing to clean up but us
        finally:
            BUS.unsubscribe(q)

    def write_event(self, event: str, data) -> None:
        payload = json.dumps(data)
        self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Serve the dashboard, the scanner and the paper-trading bot.")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 to accept connections from other machines")
    p.add_argument("--offline", metavar="DIR",
                   help="scan from cached JSON instead of the network "
                        "(see fixtures/ for the format)")
    p.add_argument("--scan-on-start", action="store_true",
                   help="run one scan as soon as the server is up")
    p.add_argument("--open", action="store_true",
                   help="open the dashboard in a browser once the port is bound")
    return p


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer that actually refuses a port already in use.

    http.server sets allow_reuse_address, which means SO_REUSEADDR. On Unix
    that only lets a new server take over a port left in TIME_WAIT, which is
    what you want when restarting. On Windows it means something else entirely:
    a second process can bind a port another process is *actively listening
    on*, and which of the two receives a given connection is undefined.

    Starting the server twice is an ordinary mistake -- a second double-click
    of run.bat -- and it should say "that port is taken", not silently start a
    second server that appears to work and then answers at random.
    """

    allow_reuse_address = os.name != "nt"


def serve(host: str, port: int, base_flags: list[str],
          scan_on_start: bool = False) -> ThreadingHTTPServer:
    Handler.runner = Runner(base_flags)
    httpd = Server((host, port), Handler)
    httpd.daemon_threads = True  # a hung SSE stream must not block shutdown
    if scan_on_start:
        Handler.runner.start([])
    return httpd


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_flags = ["--offline", args.offline] if args.offline else []

    try:
        httpd = serve(args.host, args.port, base_flags, args.scan_on_start)
    except OSError as exc:
        # Almost always "port already in use" — a second copy of the server, or
        # something else on 8000. Worth a sentence rather than a traceback,
        # since run.bat shows this in a window the reader did not ask for.
        # Plain ASCII: a stock cmd.exe window is not UTF-8, and this is the one
        # message most likely to be read there by someone who double-clicked.
        print(f"error: could not listen on {args.host}:{args.port} - {exc}",
              file=sys.stderr)
        print(f"hint: something else is using port {args.port}. Close it, or "
              f"start on another: python app.py --port {args.port + 1}",
              file=sys.stderr)
        return 2

    where = f"http://{'localhost' if args.host == '127.0.0.1' else args.host}:{httpd.server_port}"
    print(f"dashboard, scanner and bot on {where}", file=sys.stderr)
    if args.offline:
        print(f"offline mode: scanning from {args.offline}", file=sys.stderr)
    print("ctrl-c to stop", file=sys.stderr)

    if args.open:
        # The port is bound by now, so the tab cannot land on a dead page --
        # which is the whole reason this lives here and not in a sleep inside
        # the batch file.
        webbrowser.open(where)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping", file=sys.stderr)
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
