/* ===========================================================================
   Live mode — the half of the dashboard that only exists when app.py is
   serving it.

   The page was built to read committed JSON once and never move again, and
   that has to keep working: opened off GitHub Pages, or from a checkout with
   `python -m http.server`, there is no backend and none of this should fire.
   So everything here is behind one probe of /api/ping. If it fails, `bootLive`
   hands straight back to the static loader and the page is exactly what it was
   before this file existed.

   When the probe succeeds we get three things the static page cannot have:
   one round trip instead of four, a scan the browser can start, and a stream
   of progress while it runs.
   =========================================================================== */

(function (App) {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // Long enough to notice a dead backend, short enough not to delay the static
  // fallback on a page that never had one.
  const PROBE_TIMEOUT_MS = 2500;

  // Reconnect backoff for the event stream, in ms. A dropped stream is usually
  // the server restarting, so the first retry is quick; past that, back off
  // rather than hammer a server that is genuinely gone.
  const RETRY_MS = [500, 1000, 2000, 5000, 10000];

  let retries = 0;
  let elapsedTimer = null;

  /* ── talking to the backend ──────────────────────────────────────────── */

  async function api(path, options) {
    const res = await fetch(path, Object.assign({ cache: "no-store" }, options));
    let body = null;
    try { body = await res.json(); } catch (_) { /* empty or non-JSON */ }
    if (!res.ok) {
      const message = (body && body.error) || (body && body.message) || `HTTP ${res.status}`;
      const err = new Error(message);
      err.status = res.status;
      throw err;
    }
    return body;
  }

  App.api = api;

  const post = (path, payload) => api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || {}),
  });

  App.post = post;

  /** Adopt a whole /api/state payload into App. */
  function adopt(state) {
    App.pairs = state.pairs || [];
    App.meta = state.meta || App.meta;
    App.history = state.history || [];
    App.bot = state.bot || null;
    App.run = state.run || null;
    App.judge = state.judge || null;
    // Labels come from labels.json on disk now, not localStorage — the file
    // score and sweep already read. The Labels view writes back through
    // /api/labels, so the export-and-copy-the-file step is gone.
    if (Array.isArray(state.labels)) App.labels = state.labels;
    reflectJudge();
  }

  /** Show whether a scan will be reviewed by the model — i.e. whether the
      server found an ANTHROPIC_API_KEY in its environment. */
  function reflectJudge() {
    const flag = $("judge-flag");
    if (!flag) return;
    const on = App.judge && App.judge.enabled;
    flag.hidden = !on;
    if (on) flag.textContent = "+ model review (" + (App.judge.model || "on") + ")";
  }

  App.adoptState = adopt;

  async function reload() {
    adopt(await api("/api/state"));
  }

  App.reloadState = reload;

  /* ── boot ────────────────────────────────────────────────────────────── */

  /**
   * Probe for a backend; fall back to the static loader if there is none.
   * app.js calls this instead of loadFromServer when this file is present.
   */
  App.bootLive = async function bootLive(staticLoader) {
    let ping = null;
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS);
      try {
        ping = await api("/api/ping", { signal: controller.signal });
      } finally {
        clearTimeout(timer);
      }
    } catch (_) {
      ping = null;
    }

    if (!ping || !ping.live) {
      App.live = false;
      return staticLoader();
    }

    App.live = true;

    // With a backend, labels belong in labels.json — the file `score` and
    // `sweep` already read. The static page keeps them in localStorage and
    // asks you to export and copy the file in by hand; that step is what a
    // server removes. Still mirrored to localStorage so switching back to a
    // static copy does not look like the labels were lost.
    const saveLocally = App.saveLabels;
    App.saveLabels = () => {
      saveLocally();
      post("/api/labels", { labels: App.labels }).catch((err) => {
        banner("Labels saved in this browser, but not to labels.json: " + err.message);
      });
    };

    try {
      await reload();
    } catch (err) {
      // The backend answered the probe and then failed on the real request.
      // Rather than show an error page, fall back to whatever is committed.
      App.live = false;
      return staticLoader();
    }

    wire();
    connect();
  };

  /* ── the run button ──────────────────────────────────────────────────── */

  function wire() {
    $("btn-scan").addEventListener("click", startScan);
  }

  async function startScan() {
    try {
      await post("/api/scan", {});
      setRunning(true, "starting…");
    } catch (err) {
      // 409 is the expected answer to a second click while a scan is running.
      // It is information, not a failure — say so in the status line rather
      // than throwing up an error banner.
      if (err.status === 409) {
        setRunning(true, "a scan is already running");
        return;
      }
      banner("Could not start a scan: " + err.message);
    }
  }

  function setRunning(running, line) {
    $("btn-scan").disabled = running;
    $("btn-scan").textContent = running ? "Scanning…" : "Run scan";
    $("run-status").hidden = !running;
    if (line) $("run-line").textContent = line;

    clearInterval(elapsedTimer);
    elapsedTimer = null;
    if (running) {
      const started = Date.now();
      elapsedTimer = setInterval(() => {
        $("run-elapsed").textContent = Math.round((Date.now() - started) / 1000) + "s";
      }, 1000);
    } else {
      $("run-elapsed").textContent = "";
    }
  }

  function banner(text) {
    const node = $("banner-error");
    node.hidden = false;
    node.textContent = text;
  }

  /* ── the event stream ────────────────────────────────────────────────── */

  function connect() {
    const source = new EventSource("/api/events");

    source.addEventListener("hello", (e) => {
      retries = 0;
      const data = parse(e);
      if (data && data.run) setRunning(data.run.running, "resuming…");
    });

    // The scanner's stderr, line by line. Showing only the newest keeps the
    // topbar one line tall; the whole log is on the Bot tab's run panel.
    source.addEventListener("progress", (e) => {
      const data = parse(e);
      if (data && data.line) $("run-line").textContent = data.line;
    });

    source.addEventListener("run-start", () => {
      $("banner-error").hidden = true;
      setRunning(true, "starting…");
    });

    source.addEventListener("run-done", async () => {
      setRunning(false);
      await refreshAll();
    });

    source.addEventListener("run-failed", async (e) => {
      const data = parse(e);
      setRunning(false);
      banner("Scan failed: " + ((data && data.error) || "unknown error"));
      // The metadata was still published, so the funnel and the freshness
      // stamp should update to say the run failed.
      await refreshAll();
    });

    // The review runs in the background after the scan finishes, so the board
    // shows results at once. Flag that it is going, then swap back to the
    // static tag once verdicts land (refreshAll → adopt → reflectJudge).
    source.addEventListener("judge-start", (e) => {
      const data = parse(e);
      const flag = $("judge-flag");
      if (!flag || !(App.judge && App.judge.enabled)) return;
      flag.hidden = false;
      flag.textContent = "reviewing " + ((data && data.count) || "") + " pair(s) with the model…";
    });

    // The model finished reviewing the scan; reload so the verdicts land on the
    // board (they were written to judged.json just before this fired).
    source.addEventListener("judged", refreshAll);
    source.addEventListener("judge-failed", (e) => {
      const data = parse(e);
      reflectJudge();
      banner("Model review failed: " + ((data && data.error) || "unknown error")
             + " — the scan results are unaffected.");
    });

    source.addEventListener("bot", refreshAll);
    source.addEventListener("bot-failed", (e) => {
      const data = parse(e);
      banner("The bot errored: " + ((data && data.error) || "unknown error"));
    });

    source.onerror = () => {
      // EventSource retries on its own, but with no backoff and no cap. Take
      // it over so a stopped server does not mean a request every second
      // forever.
      source.close();
      const wait = RETRY_MS[Math.min(retries, RETRY_MS.length - 1)];
      retries += 1;
      setTimeout(connect, wait);
    };
  }

  function parse(event) {
    try { return JSON.parse(event.data); } catch (_) { return null; }
  }

  async function refreshAll() {
    try {
      await reload();
      App.refresh();
    } catch (_) { /* the next event will try again */ }
  }

}(window.App));
