#!/usr/bin/env python3
"""LLM review of the scanner's most promising pairs.

Sends the top pairs from a scan to Claude and asks, adversarially, whether a
YES on one venue really pays out under exactly the same circumstances as a YES
on the other — the semantic judgement the lexical scanner cannot make. Writes
a verdict, a confidence, and the specific divergence (if any) back beside each
pair, so a human reads one sentence instead of two full rule pages.

SECURITY — read this before anything else:
  * The API key is read from the ANTHROPIC_API_KEY environment variable. It is
    NEVER hardcoded here and must NEVER be committed. Set it locally with
    `$env:ANTHROPIC_API_KEY = "sk-ant-..."` (PowerShell) or store it as a
    GitHub Actions secret named ANTHROPIC_API_KEY so the scan workflow can use
    it without the value ever appearing in the repo.
  * If no key is set, this script prints a notice and exits cleanly (0) without
    calling anything — the scanner keeps working exactly as before.

This costs money: each judged pair is a paid Claude request. Reviewing the top
10 of a scan on the default model runs a few tens of cents. Standard library
only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-opus-5"          # best judgement at the money boundary
DEFAULT_COUNT = 10
DEFAULT_MAX_TOKENS = 6000                # headroom for thinking + the JSON verdict
RULES_CHAR_CAP = 3500                    # bound tokens per rule text
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 529}

HERE = os.path.dirname(os.path.abspath(__file__))


SYSTEM_PROMPT = """You are a meticulous resolution-rules auditor for prediction markets.

You are given one market from Kalshi and one from Polymarket that a keyword \
matcher flagged as possibly identical. Decide whether a YES position on one \
pays out under EXACTLY the same circumstances as a YES on the other.

Work adversarially. These two look alike on the surface — that is precisely \
why they were flagged, and surface similarity is not evidence they resolve the \
same way. Assume they differ until the rule texts prove otherwise. A single \
clause that makes them settle oppositely turns a hedge into two losing bets, so \
your job is to hunt for that clause, not to confirm the match.

Check, at minimum:
- Resolution source: the same data feed or authority, or two that could \
disagree at the margin (Coinbase vs Binance, one exchange's official close vs \
another, one polling source vs another).
- Settlement timing: the same moment and date — and whether one can be \
disputed, revised, or finalised by an oracle after the other has already paid.
- Numeric threshold and direction: the same number and the same above/below.
- Entity, ordinal, metric: the same person, team, rank, and measured quantity, \
not a substitution ("leader" vs "head of state", #1 vs #2, runs vs doubles, \
TSMC vs OpenAI).
- Polarity: a YES on one must correspond to a YES on the other, not to its \
complement ("enter a recession" vs "avoid a recession").
- Edge cases: cancellation, postponement, ties, abandonment, void and \
"none of the above" conditions — resolved the same way on both.

You are deliberately NOT told the odds or the potential profit. Judge only \
whether the two resolve identically. Do not let a confident tone or heavy \
overlap substitute for finding a concrete match or a concrete difference. If a \
rule text is missing, say so and judge on the questions alone with lower \
confidence.

Return your judgement in the required structured format. `divergence` names the \
single most important way they could resolve differently, or "none found"."""


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["equivalent", "likely_equivalent", "different"],
        },
        "confidence": {"type": "number"},
        "resolution_source": {"type": "string", "enum": ["same", "differs", "unclear"]},
        "timing": {"type": "string", "enum": ["same", "differs", "unclear"]},
        "threshold_and_polarity": {
            "type": "string", "enum": ["same", "differs", "unclear"],
        },
        "divergence": {"type": "string"},
        "reasoning": {"type": "string"},
    },
    "required": [
        "verdict", "confidence", "resolution_source", "timing",
        "threshold_and_polarity", "divergence", "reasoning",
    ],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Key handling
# ---------------------------------------------------------------------------

def get_api_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return key or None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class JudgeError(RuntimeError):
    pass


def call_api(payload: dict, api_key: str, timeout: int = 90, retries: int = 4) -> dict:
    """POST to the Messages API, retrying only transient failures.

    Isolated so the tests can stub it — no live call is made under test.
    """
    body = json.dumps(payload).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(API_URL, data=body, method="POST", headers={
            "x-api-key": api_key,
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            if exc.code not in RETRY_STATUS:
                raise JudgeError(f"HTTP {exc.code}: {detail}") from exc
            last_error = JudgeError(f"HTTP {exc.code}: {detail}")
            time.sleep(min(2 ** attempt, 20))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(min(2 ** attempt, 20))
    raise JudgeError(f"gave up after {retries} attempts: {last_error}")


# ---------------------------------------------------------------------------
# Judging one pair
# ---------------------------------------------------------------------------

def shorten(text: str, cap: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= cap else text[: cap - 1] + "…"


def build_user_content(pair: dict) -> str:
    k = pair.get("kalshi", {})
    p = pair.get("polymarket", {})

    def block(label: str, m: dict) -> str:
        rules = shorten(m.get("rules", ""), RULES_CHAR_CAP) or "(no resolution text provided)"
        return (
            f"{label}\n"
            f"  question: {m.get('question', '').strip()}\n"
            f"  category: {m.get('category', '') or 'unknown'}\n"
            f"  settles:  {m.get('settle_time') or 'unknown'}\n"
            f"  rules:    {rules}"
        )

    return (
        "Do a YES on KALSHI and a YES on POLYMARKET below pay out under exactly "
        "the same circumstances?\n\n"
        + block("KALSHI", k) + "\n\n" + block("POLYMARKET", p)
    )


def parse_response(api_response: dict) -> dict:
    """Turn a raw Messages API response into a verdict dict, or an error dict."""
    stop = api_response.get("stop_reason")
    if stop == "refusal":
        return {"error": "model refused to judge this pair"}
    text = next((b.get("text") for b in api_response.get("content", [])
                 if b.get("type") == "text" and b.get("text")), None)
    if text is None:
        note = "response truncated before a verdict" if stop == "max_tokens" else \
               f"no verdict text in response (stop_reason={stop})"
        return {"error": note}
    try:
        verdict = json.loads(text)
    except json.JSONDecodeError as exc:
        return {"error": f"verdict was not valid JSON: {exc}"}
    verdict["model"] = api_response.get("model")
    usage = api_response.get("usage", {})
    verdict["_tokens"] = {
        "input": usage.get("input_tokens"),
        "output": usage.get("output_tokens"),
    }
    return verdict


def judge_pair(pair: dict, api_key: str, model: str, max_tokens: int) -> dict:
    payload = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},   # reused across every pair in the batch
        }],
        "messages": [{"role": "user", "content": build_user_content(pair)}],
        "output_config": {"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
    }
    try:
        return parse_response(call_api(payload, api_key))
    except JudgeError as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

MARK = {"equivalent": "MATCH", "likely_equivalent": "LIKELY", "different": "MISMATCH"}


def render_verdict(i: int, pair: dict, v: dict) -> str:
    k = pair.get("kalshi", {}).get("question", "")
    p = pair.get("polymarket", {}).get("question", "")
    lines = [f"[{i}] " + ("ERROR: " + v["error"] if "error" in v else
                          f"{MARK.get(v['verdict'], v['verdict'])}  "
                          f"confidence {float(v.get('confidence', 0)):.0%}")]
    lines.append(f"    K: {shorten(k, 80)}")
    lines.append(f"    P: {shorten(p, 80)}")
    if "error" not in v:
        lines.append(f"    source {v['resolution_source']} | timing {v['timing']} | "
                     f"threshold/polarity {v['threshold_and_polarity']}")
        if v.get("divergence") and v["divergence"].strip().lower() not in ("none", "none found"):
            lines.append(f"    ! divergence: {shorten(v['divergence'], 200)}")
        lines.append(f"    {shorten(v.get('reasoning', ''), 220)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def load_pairs(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        data = data.get("pairs") or data.get("results") or []
    return data


def cmd_run(args, api_key: str) -> int:
    try:
        pairs = load_pairs(args.source)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read {args.source!r}: {exc}", file=sys.stderr)
        return 2

    shortlist = pairs[: args.count]
    if not shortlist:
        print("no pairs to review.", file=sys.stderr)
        return 0

    print(f"reviewing the top {len(shortlist)} pair(s) with {args.model}...",
          file=sys.stderr)
    judged, matches, in_tok, out_tok = [], 0, 0, 0
    for i, pair in enumerate(shortlist, 1):
        verdict = judge_pair(pair, api_key, args.model, args.max_tokens)
        pair = dict(pair, llm_verdict=verdict)
        judged.append(pair)
        tokens = verdict.pop("_tokens", {}) if "error" not in verdict else {}
        in_tok += tokens.get("input") or 0
        out_tok += tokens.get("output") or 0
        if verdict.get("verdict") in ("equivalent", "likely_equivalent"):
            matches += 1
        print(render_verdict(i, pair, verdict))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(judged, fh, indent=2)
    print(f"\n{matches}/{len(shortlist)} judged equivalent or likely; wrote {args.out}",
          file=sys.stderr)
    if in_tok or out_tok:
        print(f"tokens: {in_tok:,} in / {out_tok:,} out", file=sys.stderr)
    return 0


def cmd_score(args, api_key: str) -> int:
    """Judge the seed labels and score the model against the human calls.

    labels.json carries only the questions, not full rule texts, so this is a
    floor on the model's ability, not its full power on a real scan.
    """
    labels_path = os.path.join(HERE, "labels.json")
    try:
        with open(labels_path, encoding="utf-8") as fh:
            rows = [r for r in json.load(fh) if r.get("label") in ("match", "mismatch")]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"error: cannot read labels.json ({exc}). Run a scan first.", file=sys.stderr)
        return 2
    if args.count:
        rows = rows[: args.count]

    print(f"scoring {args.model} on {len(rows)} labelled pair(s) "
          f"(question text only — no full rules)...", file=sys.stderr)
    tp = fp = tn = fn = 0
    for row in rows:
        pair = {
            "kalshi": {"question": row.get("kalshi_question", "")},
            "polymarket": {"question": row.get("poly_question", "")},
        }
        v = judge_pair(pair, api_key, args.model, args.max_tokens)
        if "error" in v:
            print(f"  skipped (error: {v['error']})", file=sys.stderr)
            continue
        predicted_match = v["verdict"] in ("equivalent", "likely_equivalent")
        truth = row["label"] == "match"
        tp += predicted_match and truth
        fp += predicted_match and not truth
        fn += (not predicted_match) and truth
        tn += (not predicted_match) and not truth

    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    print(f"\n{args.model} vs human labels:")
    print(f"  kept:    {tp:>3} true  {fp:>3} false   precision {precision:.2f}")
    print(f"  dropped: {tn:>3} true  {fn:>3} false   recall    {recall:.2f}")
    print("  (run again with --model claude-sonnet-5 to compare)")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="LLM review of the scanner's most promising pairs. "
                    "Reads ANTHROPIC_API_KEY from the environment; never commit the key.")
    sub = p.add_subparsers(dest="command")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=DEFAULT_MODEL,
                        help="claude-opus-5 (default) or claude-sonnet-5 for a cheaper pass")
    common.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)

    r = sub.add_parser("run", parents=[common], help="review the top pairs from a scan")
    r.add_argument("--source", default=os.path.join(HERE, "results", "latest.json"))
    r.add_argument("-c", "--count", type=int, default=DEFAULT_COUNT,
                   help="how many of the most promising pairs to review")
    r.add_argument("--out", default=os.path.join(HERE, "results", "judged.json"))

    s = sub.add_parser("score", parents=[common], help="score the model against labels.json")
    s.add_argument("-c", "--count", type=int, default=0, help="limit labels judged (0 = all)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = args.command or "run"

    api_key = get_api_key()
    if api_key is None:
        print("ANTHROPIC_API_KEY is not set — skipping LLM review (the scan is "
              "unaffected).", file=sys.stderr)
        print("set it locally, or add it as a GitHub Actions secret named "
              "ANTHROPIC_API_KEY.", file=sys.stderr)
        return 0

    if command == "score":
        return cmd_score(args, api_key)
    return cmd_run(args, api_key)


if __name__ == "__main__":
    raise SystemExit(main())
