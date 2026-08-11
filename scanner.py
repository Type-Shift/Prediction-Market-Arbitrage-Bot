#!/usr/bin/env python3
"""Cross-venue scanner for Kalshi and Polymarket.

Finds pairs of markets that ask the same question on both venues and quotes
different odds. Matching uses TF-IDF token similarity on the question text,
similarity of the resolution rules, agreement of any numeric thresholds, and
closeness of the settlement dates. Divergence is reported two ways: the raw
gap between mid prices, and the executable edge after crossing both spreads
and paying fees.

Standard library only. Python 3.11+.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA_API = "https://gamma-api.polymarket.com"
USER_AGENT = "market-arb-scanner/1.0 (public market data reader)"

# Kalshi charges takers ceil(0.07 * contracts * P * (1 - P)) in cents. Per
# contract that is 0.07 * P * (1 - P) dollars before the round-up. Polymarket
# currently charges no trading fee on most markets.
KALSHI_FEE_COEFF = 0.07
POLY_FEE_BPS = 0.0


# --------------------------------------------------------------------------
# Text handling
# --------------------------------------------------------------------------

STOPWORDS = {
    "a", "an", "the", "will", "be", "is", "are", "was", "were", "to", "of",
    "in", "on", "at", "by", "for", "with", "and", "or", "as", "that", "this",
    "it", "its", "any", "all", "from", "than", "then", "there", "which",
    "who", "whom", "what", "when", "market", "markets", "resolve", "resolves",
    "resolved", "resolution", "yes", "no", "if", "does", "do", "did", "has",
    "have", "had", "shall", "would", "could", "may", "might", "question",
    "contract", "outcome",
}

NEGATION = {"not", "no", "never", "without", "fail", "fails", "neither", "nor",
            "avoid", "avoids", "prevent", "prevents", "unable", "reject", "rejects",
            "miss", "misses", "lose", "loses", "unless"}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

DIRECTION_UP = {"above", "over", "greater", "more", "least", "exceed", "exceeds", "higher", "up"}
DIRECTION_DOWN = {"below", "under", "less", "fewer", "most", "lower", "down"}

_WORD_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|k\b|m\b|bn\b|billion|million|thousand)?")


def normalize(text: str) -> str:
    """Lowercase, strip markup and punctuation noise, collapse whitespace."""
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = text.replace("’", "'").replace("‘", "'")
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text.lower()).strip()


def tokenize(text: str, keep_stopwords: bool = False) -> list[str]:
    tokens = _WORD_RE.findall(normalize(text))
    if keep_stopwords:
        return tokens
    return [t for t in tokens if t not in STOPWORDS and len(t) > 1]


def extract_numbers(text: str) -> set[float]:
    """Pull comparable magnitudes out of a question, scaling k/m/bn suffixes.

    Years are kept as-is so "2028" matches "2028" but a bare 25 does not match
    a 25% threshold expressed elsewhere as 0.25 — that precision is deliberate,
    since a wrong threshold makes two markets genuinely different.
    """
    out: set[float] = set()
    for raw, suffix in _NUM_RE.findall(normalize(text)):
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        suffix = (suffix or "").strip()
        if suffix in ("k", "thousand"):
            value *= 1_000
        elif suffix in ("m", "million"):
            value *= 1_000_000
        elif suffix in ("bn", "billion"):
            value *= 1_000_000_000
        out.add(round(value, 4))
    return out


def extract_direction(text: str) -> str:
    """Return 'up', 'down' or '' for threshold-style questions."""
    tokens = set(tokenize(text, keep_stopwords=True))
    up = bool(tokens & DIRECTION_UP)
    down = bool(tokens & DIRECTION_DOWN)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return ""


def negation_count(text: str) -> int:
    return sum(1 for t in tokenize(text, keep_stopwords=True) if t in NEGATION)


def build_idf(docs: list[list[str]]) -> dict[str, float]:
    df: dict[str, int] = defaultdict(int)
    for tokens in docs:
        for token in set(tokens):
            df[token] += 1
    n = max(len(docs), 1)
    return {token: math.log(1 + n / count) for token, count in df.items()}


def tfidf_vector(tokens: list[str], idf: dict[str, float]) -> dict[str, float]:
    tf: dict[str, int] = defaultdict(int)
    for token in tokens:
        tf[token] += 1
    vec = {t: (1 + math.log(c)) * idf.get(t, math.log(2)) for t, c in tf.items()}
    norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
    return {t: v / norm for t, v in vec.items()}


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(weight * b.get(token, 0.0) for token, weight in a.items())


def ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a[:2000], b[:2000]).ratio()


# --------------------------------------------------------------------------
# Market model
# --------------------------------------------------------------------------

@dataclass
class Market:
    venue: str
    market_id: str
    question: str
    rules: str
    url: str
    yes_bid: float | None          # best price you can sell YES at, 0-1
    yes_ask: float | None          # best price you can buy YES at, 0-1
    no_bid: float | None
    no_ask: float | None
    last: float | None
    volume: float
    liquidity: float
    close_time: datetime | None
    tokens: list[str] = field(default_factory=list)
    rule_tokens: list[str] = field(default_factory=list)
    vector: dict[str, float] = field(default_factory=dict)
    numbers: set[float] = field(default_factory=set)
    direction: str = ""

    @property
    def mid(self) -> float | None:
        """Best available estimate of the YES probability."""
        if self.yes_bid is not None and self.yes_ask is not None:
            return (self.yes_bid + self.yes_ask) / 2
        for candidate in (self.last, self.yes_ask, self.yes_bid):
            if candidate is not None:
                return candidate
        return None

    @property
    def spread(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def effective_no_ask(self) -> float | None:
        """Cost of buying NO. Falls back to the complement identity."""
        if self.no_ask is not None:
            return self.no_ask
        if self.yes_bid is not None:
            return 1.0 - self.yes_bid
        return None

    def effective_yes_ask(self) -> float | None:
        if self.yes_ask is not None:
            return self.yes_ask
        if self.no_bid is not None:
            return 1.0 - self.no_bid
        return None

    def prepare(self) -> None:
        self.tokens = tokenize(self.question)
        self.rule_tokens = tokenize(self.rules)
        self.numbers = extract_numbers(self.question)
        self.direction = extract_direction(self.question)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class FetchError(RuntimeError):
    pass


def http_get_json(url: str, timeout: int = 30, retries: int = 3):
    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
            continue
        text = body.decode("utf-8", "replace").lstrip()
        if text[:1] not in ("{", "["):
            # A captive portal or corporate filter answered instead of the API.
            snippet = re.sub(r"\s+", " ", text[:120])
            raise FetchError(
                f"{urllib.parse.urlsplit(url).netloc} returned HTML, not JSON "
                f"(likely a network filter or block page): {snippet}"
            )
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            last_error = exc
            time.sleep(1.0)
    raise FetchError(f"could not fetch {url}: {last_error}")


def parse_time(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def cents(value) -> float | None:
    """Kalshi quotes in cents; 0 means no resting order on that side."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number >= 100:
        return None
    return number / 100.0


def decimal_price(value) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0 or number >= 1:
        return None
    return number


def json_field(value, default):
    """Gamma encodes several array fields as JSON strings."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


# --------------------------------------------------------------------------
# Venue adapters
# --------------------------------------------------------------------------

def fetch_kalshi(max_markets: int, min_volume: float, verbose: bool = True) -> list[dict]:
    raw: list[dict] = []
    cursor = ""
    while len(raw) < max_markets:
        params = {"limit": min(1000, max_markets - len(raw)), "status": "open"}
        if cursor:
            params["cursor"] = cursor
        payload = http_get_json(f"{KALSHI_API}/markets?" + urllib.parse.urlencode(params))
        batch = payload.get("markets") or []
        raw.extend(batch)
        cursor = payload.get("cursor") or ""
        if verbose:
            print(f"  kalshi: {len(raw)} markets", file=sys.stderr)
        if not cursor or not batch:
            break
    # Volume filtering happens in load_markets, so the cache holds raw data and
    # stays usable if you lower the threshold on the next run.
    return raw


def kalshi_to_market(row: dict) -> Market:
    title = (row.get("title") or "").strip()
    sub = (row.get("yes_sub_title") or row.get("subtitle") or "").strip()
    question = title
    if sub and sub.lower() not in title.lower() and sub.lower() not in ("yes", "no"):
        question = f"{title} — {sub}"
    rules = " ".join(filter(None, [
        row.get("rules_primary") or "",
        row.get("rules_secondary") or "",
    ])).strip()
    ticker = row.get("ticker") or ""
    event = row.get("event_ticker") or ""
    return Market(
        venue="kalshi",
        market_id=ticker,
        question=question,
        rules=rules,
        url=f"https://kalshi.com/markets/{event.lower()}" if event else "https://kalshi.com",
        yes_bid=cents(row.get("yes_bid")),
        yes_ask=cents(row.get("yes_ask")),
        no_bid=cents(row.get("no_bid")),
        no_ask=cents(row.get("no_ask")),
        last=cents(row.get("last_price")),
        volume=float(row.get("volume") or 0),
        liquidity=float(row.get("liquidity") or 0) / 100.0,
        close_time=parse_time(row.get("close_time") or row.get("expiration_time")),
    )


def fetch_polymarket(max_markets: int, min_volume: float, verbose: bool = True) -> list[dict]:
    raw: list[dict] = []
    offset = 0
    page = 500
    while len(raw) < max_markets:
        params = {
            "limit": min(page, max_markets - len(raw)),
            "offset": offset,
            "closed": "false",
            "active": "true",
            "archived": "false",
            "order": "volumeNum",
            "ascending": "false",
        }
        if min_volume > 0:
            params["volume_num_min"] = min_volume
        batch = http_get_json(f"{POLY_GAMMA_API}/markets?" + urllib.parse.urlencode(params))
        if not isinstance(batch, list) or not batch:
            break
        raw.extend(batch)
        offset += len(batch)
        if verbose:
            print(f"  polymarket: {len(raw)} markets", file=sys.stderr)
        if len(batch) < params["limit"]:
            break
    return raw


def polymarket_to_market(row: dict) -> Market | None:
    outcomes = json_field(row.get("outcomes"), ["Yes", "No"])
    prices = json_field(row.get("outcomePrices"), [])
    if len(outcomes) != 2:
        return None  # Only binary books are directly comparable to a Kalshi contract.

    yes_index = 0
    for i, name in enumerate(outcomes):
        if str(name).strip().lower() == "yes":
            yes_index = i
            break

    last = None
    if len(prices) == len(outcomes):
        last = decimal_price(prices[yes_index])
    if last is None:
        last = decimal_price(row.get("lastTradePrice"))

    best_bid = decimal_price(row.get("bestBid"))
    best_ask = decimal_price(row.get("bestAsk"))
    if yes_index == 1 and best_bid is not None and best_ask is not None:
        best_bid, best_ask = 1 - best_ask, 1 - best_bid

    question = (row.get("question") or "").strip()
    group = (row.get("groupItemTitle") or "").strip()
    if group and group.lower() not in question.lower():
        question = f"{question} — {group}"

    slug = row.get("slug") or ""
    return Market(
        venue="polymarket",
        market_id=str(row.get("conditionId") or row.get("id") or slug),
        question=question,
        rules=(row.get("description") or "").strip(),
        url=f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com",
        yes_bid=best_bid,
        yes_ask=best_ask,
        no_bid=(1 - best_ask) if best_ask is not None else None,
        no_ask=(1 - best_bid) if best_bid is not None else None,
        last=last,
        volume=float(row.get("volumeNum") or row.get("volume") or 0),
        liquidity=float(row.get("liquidityNum") or row.get("liquidity") or 0),
        close_time=parse_time(row.get("endDateIso") or row.get("endDate")),
    )


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

@dataclass
class Pair:
    kalshi: Market
    poly: Market
    title_sim: float
    rules_sim: float
    date_sim: float
    number_penalty: float
    confidence: float
    days_apart: float | None
    warnings: list[str]
    polarity_suspect: bool = False

    # Pricing
    mid_kalshi: float | None = None
    mid_poly: float | None = None
    mid_gap: float = 0.0
    arb_edge: float = 0.0
    arb_leg: str = ""
    arb_cost: float | None = None


def date_similarity(a: datetime | None, b: datetime | None) -> tuple[float, float | None]:
    if a is None or b is None:
        return 0.5, None  # Unknown, so neither evidence for nor against.
    days = abs((a - b).total_seconds()) / 86400.0
    if days <= 1:
        score = 1.0
    elif days <= 3:
        score = 0.9
    elif days <= 7:
        score = 0.75
    elif days <= 21:
        score = 0.5
    elif days <= 60:
        score = 0.25
    else:
        score = 0.05
    return score, days


def number_agreement(a: set[float], b: set[float]) -> tuple[float, str | None]:
    """Penalise pairs whose thresholds differ — the classic false positive.

    "S&P 500 above 7000 on December 31, 2026" and the same question at 7500
    share almost every word and most of their numbers, so a plain intersection
    test passes them. The largest magnitude is nearly always the threshold, so
    compare that directly and treat the rest as a weaker signal.
    """
    if not a and not b:
        return 0.0, None
    if not a or not b:
        return 0.10, "one side states a numeric threshold, the other does not"

    union = a | b
    jaccard = len(a & b) / len(union) if union else 1.0
    penalty = 0.20 * (1.0 - jaccard)

    max_a, max_b = max(a), max(b)
    scale = max(abs(max_a), abs(max_b), 1.0)
    if abs(max_a - max_b) / scale > 0.01:
        return penalty + 0.40, f"thresholds differ ({max_a:g} vs {max_b:g})"
    if penalty > 0.08:
        return penalty, f"secondary figures differ ({sorted(a ^ b)[:4]})"
    return penalty, None


def score_pair(k: Market, p: Market, rules_idf: dict[str, float],
               weights: tuple[float, float, float]) -> Pair:
    w_title, w_rules, w_date = weights

    title_cos = cosine(k.vector, p.vector)
    title_seq = ratio(normalize(k.question), normalize(p.question))
    title_sim = 0.65 * title_cos + 0.35 * title_seq

    if k.rules and p.rules:
        rules_cos = cosine(tfidf_vector(k.rule_tokens, rules_idf),
                           tfidf_vector(p.rule_tokens, rules_idf))
        rules_seq = ratio(normalize(k.rules), normalize(p.rules))
        rules_sim = 0.7 * rules_cos + 0.3 * rules_seq
        rules_known = True
    else:
        rules_sim, rules_known = 0.0, False

    date_sim, days_apart = date_similarity(k.close_time, p.close_time)
    penalty, number_warning = number_agreement(k.numbers, p.numbers)

    if rules_known:
        base = w_title * title_sim + w_rules * rules_sim + w_date * date_sim
    else:
        # Redistribute the rules weight rather than scoring a missing text as
        # a disagreement.
        scale = w_title + w_date
        base = (w_title / scale) * title_sim + (w_date / scale) * date_sim
        base *= 0.95

    warnings: list[str] = []
    if number_warning:
        warnings.append(number_warning)
    if not rules_known:
        warnings.append("resolution text missing on one venue — rules not compared")
    if k.direction and p.direction and k.direction != p.direction:
        penalty += 0.35
        warnings.append("threshold direction disagrees (above vs below)")
    polarity_suspect = (negation_count(k.question) % 2) != (negation_count(p.question) % 2)
    if polarity_suspect:
        penalty += 0.15
        warnings.append("possible polarity flip — one side may be the complement of "
                        "the other, so YES is not comparable to YES; edge not computed")
    if days_apart is not None and days_apart > 30:
        warnings.append(f"settlement dates {days_apart:.0f} days apart")

    confidence = max(0.0, min(1.0, base - penalty))
    return Pair(
        kalshi=k, poly=p,
        title_sim=title_sim, rules_sim=rules_sim, date_sim=date_sim,
        number_penalty=penalty, confidence=confidence,
        days_apart=days_apart, warnings=warnings,
        polarity_suspect=polarity_suspect,
    )


def candidate_pairs(kalshi: list[Market], poly: list[Market], idf: dict[str, float],
                    max_candidates: int) -> dict[int, set[int]]:
    """Inverted index over discriminative tokens, so we score thousands of
    plausible pairs instead of millions of arbitrary ones."""
    index: dict[str, list[int]] = defaultdict(list)
    doc_count = max(len(poly), 1)
    for i, market in enumerate(poly):
        for token in set(market.tokens):
            index[token].append(i)

    # Tokens present in more than 5% of Polymarket questions carry no signal
    # for candidate generation and blow up the pair count.
    common = {t for t, ids in index.items() if len(ids) > max(20, 0.05 * doc_count)}

    out: dict[int, set[int]] = {}
    for ki, market in enumerate(kalshi):
        scores: dict[int, float] = defaultdict(float)
        for token in set(market.tokens):
            if token in common:
                continue
            weight = idf.get(token, 1.0)
            for pi in index.get(token, ()):
                scores[pi] += weight
        if not scores:
            continue
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:max_candidates]
        out[ki] = {pi for pi, _ in ranked}
    return out


# --------------------------------------------------------------------------
# Pricing and arbitrage
# --------------------------------------------------------------------------

def kalshi_fee(price: float, coeff: float = KALSHI_FEE_COEFF) -> float:
    """Per-contract taker fee in dollars, before Kalshi's per-order round-up."""
    return coeff * price * (1.0 - price)


def poly_fee(price: float, bps: float = POLY_FEE_BPS) -> float:
    return price * bps / 10_000.0


def price_pair(pair: Pair, kalshi_coeff: float, poly_bps: float) -> None:
    k, p = pair.kalshi, pair.poly
    pair.mid_kalshi = k.mid
    pair.mid_poly = p.mid
    if pair.mid_kalshi is not None and pair.mid_poly is not None:
        pair.mid_gap = pair.mid_kalshi - pair.mid_poly

    if pair.polarity_suspect:
        # YES on one venue may be NO on the other. Quoting an edge here would
        # be worse than quoting nothing.
        pair.arb_leg = "not computed — check polarity by hand"
        return

    best_edge = float("-inf")
    best_leg, best_cost = "", None

    # Leg A: buy YES on Kalshi, buy NO on Polymarket. Both pay $1 if the pair
    # really is the same question, so any total cost under $1 is locked profit.
    ky, pn = k.effective_yes_ask(), p.effective_no_ask()
    if ky is not None and pn is not None:
        cost = ky + pn + kalshi_fee(ky, kalshi_coeff) + poly_fee(pn, poly_bps)
        if 1.0 - cost > best_edge:
            best_edge, best_leg, best_cost = 1.0 - cost, "buy YES kalshi / NO polymarket", cost

    # Leg B: the mirror image.
    py, kn = p.effective_yes_ask(), k.effective_no_ask()
    if py is not None and kn is not None:
        cost = py + kn + kalshi_fee(kn, kalshi_coeff) + poly_fee(py, poly_bps)
        if 1.0 - cost > best_edge:
            best_edge, best_leg, best_cost = 1.0 - cost, "buy YES polymarket / NO kalshi", cost

    if best_edge > float("-inf"):
        pair.arb_edge = best_edge
        pair.arb_leg = best_leg
        pair.arb_cost = best_cost


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------

def load_markets(args) -> tuple[list[Market], list[Market]]:
    if args.offline:
        with open(os.path.join(args.offline, "kalshi.json"), encoding="utf-8") as fh:
            kalshi_raw = json.load(fh)
        with open(os.path.join(args.offline, "polymarket.json"), encoding="utf-8") as fh:
            poly_raw = json.load(fh)
        kalshi_raw = kalshi_raw.get("markets", kalshi_raw) if isinstance(kalshi_raw, dict) else kalshi_raw
    else:
        cache = cache_paths(args.cache_dir)
        kalshi_raw = read_cache(cache["kalshi"], args.cache_ttl)
        if kalshi_raw is None:
            print("fetching kalshi...", file=sys.stderr)
            kalshi_raw = fetch_kalshi(args.max_markets, args.min_volume_kalshi)
            write_cache(cache["kalshi"], kalshi_raw)
        else:
            print(f"kalshi: {len(kalshi_raw)} markets from cache", file=sys.stderr)

        poly_raw = read_cache(cache["polymarket"], args.cache_ttl)
        if poly_raw is None:
            print("fetching polymarket...", file=sys.stderr)
            poly_raw = fetch_polymarket(args.max_markets, args.min_volume_poly)
            write_cache(cache["polymarket"], poly_raw)
        else:
            print(f"polymarket: {len(poly_raw)} markets from cache", file=sys.stderr)

    kalshi = [kalshi_to_market(r) for r in kalshi_raw]
    poly = [m for m in (polymarket_to_market(r) for r in poly_raw) if m is not None]

    kalshi = [m for m in kalshi if m.question and m.mid is not None
              and m.volume >= args.min_volume_kalshi]
    poly = [m for m in poly if m.question and m.mid is not None
            and m.volume >= args.min_volume_poly]

    now = datetime.now(timezone.utc)
    if args.max_days_out:
        horizon = args.max_days_out
        kalshi = [m for m in kalshi if m.close_time is None
                  or 0 <= (m.close_time - now).days <= horizon]
        poly = [m for m in poly if m.close_time is None
                or 0 <= (m.close_time - now).days <= horizon]

    for market in kalshi + poly:
        market.prepare()
    return kalshi, poly


def cache_paths(directory: str) -> dict[str, str]:
    os.makedirs(directory, exist_ok=True)
    return {
        "kalshi": os.path.join(directory, "kalshi.json"),
        "polymarket": os.path.join(directory, "polymarket.json"),
    }


def read_cache(path: str, ttl_seconds: int):
    if ttl_seconds <= 0 or not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > ttl_seconds:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def write_cache(path: str, payload) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass


def scan(args) -> list[Pair]:
    kalshi, poly = load_markets(args)
    print(f"comparing {len(kalshi)} kalshi against {len(poly)} polymarket markets",
          file=sys.stderr)
    if not kalshi or not poly:
        return []

    idf = build_idf([m.tokens for m in kalshi + poly])
    rules_idf = build_idf([m.rule_tokens for m in kalshi + poly if m.rule_tokens])
    for market in kalshi + poly:
        market.vector = tfidf_vector(market.tokens, idf)

    weights = (args.weight_title, args.weight_rules, args.weight_date)
    candidates = candidate_pairs(kalshi, poly, idf, args.max_candidates)

    best_by_kalshi: dict[int, Pair] = {}
    for ki, poly_indices in candidates.items():
        for pi in poly_indices:
            pair = score_pair(kalshi[ki], poly[pi], rules_idf, weights)
            if pair.confidence < args.min_confidence:
                continue
            if pair.days_apart is not None and pair.days_apart > args.max_date_diff:
                continue
            current = best_by_kalshi.get(ki)
            if current is None or pair.confidence > current.confidence:
                best_by_kalshi[ki] = pair

    pairs = list(best_by_kalshi.values())
    for pair in pairs:
        price_pair(pair, args.kalshi_fee_coeff, args.poly_fee_bps)

    if args.arb_only:
        pairs = [p for p in pairs if p.arb_edge >= args.min_edge]
    else:
        pairs = [p for p in pairs
                 if abs(p.mid_gap) >= args.min_edge or p.arb_edge >= args.min_edge]

    key = (lambda p: p.arb_edge) if args.sort == "arb" else (
        lambda p: abs(p.mid_gap) if args.sort == "gap" else p.confidence)
    pairs.sort(key=key, reverse=True)
    return pairs[:args.top]


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def shorten(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def pct(value: float | None) -> str:
    return "  n/a" if value is None else f"{value * 100:5.1f}"


def render(pairs: list[Pair], verbose: bool) -> str:
    if not pairs:
        return "No pairs cleared the thresholds. Try --min-confidence 0.45 --min-edge 0.02.\n"

    lines = [
        "",
        f"{len(pairs)} matched pair(s), best first",
        "=" * 100,
    ]
    for i, p in enumerate(pairs, 1):
        arb_flag = "  <-- RISKLESS EDGE" if p.arb_edge > 0 else ""
        lines.append("")
        lines.append(f"[{i}] confidence {p.confidence:.2f}   "
                     f"mid gap {p.mid_gap * 100:+.1f}pp   "
                     f"executable edge {p.arb_edge * 100:+.2f}pp{arb_flag}")
        lines.append(f"    kalshi     {pct(p.mid_kalshi)}%  "
                     f"[bid {pct(p.kalshi.yes_bid)} / ask {pct(p.kalshi.yes_ask)}]  "
                     f"vol {p.kalshi.volume:,.0f}")
        lines.append(f"      {shorten(p.kalshi.question, 88)}")
        lines.append(f"      {p.kalshi.market_id}  {p.kalshi.url}")
        lines.append(f"    polymarket {pct(p.mid_poly)}%  "
                     f"[bid {pct(p.poly.yes_bid)} / ask {pct(p.poly.yes_ask)}]  "
                     f"vol {p.poly.volume:,.0f}")
        lines.append(f"      {shorten(p.poly.question, 88)}")
        lines.append(f"      {p.poly.url}")
        lines.append(f"    similarity: title {p.title_sim:.2f}  rules {p.rules_sim:.2f}  "
                     f"dates {p.date_sim:.2f}"
                     + (f"  ({p.days_apart:.0f}d apart)" if p.days_apart is not None else ""))
        if p.arb_edge > 0:
            lines.append(f"    trade: {p.arb_leg} — pay ${p.arb_cost:.4f} "
                         f"for $1.00 at settlement (fees included)")
        for warning in p.warnings:
            lines.append(f"    ! {warning}")
        if verbose and p.kalshi.rules:
            lines.append(f"    kalshi rules: {shorten(p.kalshi.rules, 300)}")
            lines.append(f"    poly rules:   {shorten(p.poly.rules, 300)}")
    lines.append("")
    lines.append("Confidence is a text-similarity estimate, not a guarantee the rules "
                 "match. Read both rule sets before trading.")
    lines.append("")
    return "\n".join(lines)


def pair_to_dict(p: Pair) -> dict:
    def side(m: Market) -> dict:
        return {
            "venue": m.venue, "id": m.market_id, "question": m.question, "url": m.url,
            "yes_bid": m.yes_bid, "yes_ask": m.yes_ask, "mid": m.mid,
            "volume": m.volume, "liquidity": m.liquidity,
            "close_time": m.close_time.isoformat() if m.close_time else None,
            "rules": m.rules,
        }
    return {
        "confidence": round(p.confidence, 4),
        "title_similarity": round(p.title_sim, 4),
        "rules_similarity": round(p.rules_sim, 4),
        "date_similarity": round(p.date_sim, 4),
        "days_apart": p.days_apart,
        "mid_gap": round(p.mid_gap, 4),
        "arb_edge": round(p.arb_edge, 4),
        "arb_leg": p.arb_leg,
        "arb_cost": p.arb_cost,
        "warnings": p.warnings,
        "kalshi": side(p.kalshi),
        "polymarket": side(p.poly),
    }


def write_csv(path: str, pairs: list[Pair]) -> None:
    columns = ["confidence", "mid_gap", "arb_edge", "arb_leg", "kalshi_mid", "poly_mid",
               "kalshi_question", "poly_question", "kalshi_id", "poly_url",
               "title_similarity", "rules_similarity", "days_apart", "warnings"]
    # utf-8-sig so Excel reads the question text correctly.
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for p in pairs:
            writer.writerow([
                f"{p.confidence:.4f}", f"{p.mid_gap:.4f}", f"{p.arb_edge:.4f}", p.arb_leg,
                p.mid_kalshi, p.mid_poly, p.kalshi.question, p.poly.question,
                p.kalshi.market_id, p.poly.url,
                f"{p.title_sim:.4f}", f"{p.rules_sim:.4f}",
                "" if p.days_apart is None else f"{p.days_apart:.1f}",
                "; ".join(p.warnings),
            ])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find Kalshi and Polymarket markets with matching rules and different odds.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--min-edge", type=float, default=0.03,
                        help="minimum mid-price gap or executable edge, as a probability")
    parser.add_argument("--min-confidence", type=float, default=0.55,
                        help="minimum match confidence, 0-1")
    parser.add_argument("--arb-only", action="store_true",
                        help="show only pairs with a positive edge after fees and spreads")
    parser.add_argument("--top", type=int, default=25, help="how many pairs to report")
    parser.add_argument("--sort", choices=["arb", "gap", "confidence"], default="arb")

    parser.add_argument("--min-volume-kalshi", type=float, default=100,
                        help="minimum Kalshi contract volume")
    parser.add_argument("--min-volume-poly", type=float, default=5000,
                        help="minimum Polymarket USD volume")
    parser.add_argument("--max-markets", type=int, default=4000,
                        help="cap on markets pulled per venue")
    parser.add_argument("--max-days-out", type=int, default=400,
                        help="ignore markets settling further out than this; 0 disables")
    parser.add_argument("--max-date-diff", type=float, default=45,
                        help="reject pairs whose settlement dates differ by more days")
    parser.add_argument("--max-candidates", type=int, default=40,
                        help="Polymarket candidates scored per Kalshi market")

    parser.add_argument("--weight-title", type=float, default=0.55)
    parser.add_argument("--weight-rules", type=float, default=0.28)
    parser.add_argument("--weight-date", type=float, default=0.17)

    parser.add_argument("--kalshi-fee-coeff", type=float, default=KALSHI_FEE_COEFF,
                        help="Kalshi taker fee coefficient (0.07 standard, 0.035 on some series)")
    parser.add_argument("--poly-fee-bps", type=float, default=POLY_FEE_BPS,
                        help="Polymarket fee in basis points")

    parser.add_argument("--cache-dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache"))
    parser.add_argument("--cache-ttl", type=int, default=300,
                        help="seconds to reuse cached market data; 0 disables the cache")
    parser.add_argument("--offline", metavar="DIR",
                        help="read kalshi.json and polymarket.json from DIR instead of the network")
    parser.add_argument("--json", metavar="PATH", help="write results as JSON")
    parser.add_argument("--csv", metavar="PATH", help="write results as CSV")
    parser.add_argument("--verbose", action="store_true", help="print both rule texts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        pairs = scan(args)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: if you are behind a filtered network, set HTTPS_PROXY or run "
              "--offline with cached JSON.", file=sys.stderr)
        return 2

    sys.stdout.write(render(pairs, args.verbose))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump([pair_to_dict(p) for p in pairs], fh, indent=2)
        print(f"wrote {args.json}", file=sys.stderr)
    if args.csv:
        write_csv(args.csv, pairs)
        print(f"wrote {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
