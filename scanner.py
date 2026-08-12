#!/usr/bin/env python3
"""Cross-venue scanner for Kalshi and Polymarket.

Finds pairs of markets that ask the same question on both venues and quotes
different odds. Matching uses TF-IDF token similarity on the question text,
similarity of the resolution rules, agreement of any numeric thresholds, and
closeness of the settlement dates.

Divergence is reported three ways: the raw gap between mid prices, the edge
available at a requested size after walking both order books and paying both
venues' fees, and that edge annualised over the time your capital is locked up.

Standard library only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

# ===========================================================================
#  CONFIG -- edit these, then just press Run. No command line needed.
#
#  Everything below is also available as a --flag, but you never have to use
#  one. Flags override CONFIG; CONFIG overrides the built-in defaults.
# ===========================================================================

CONFIG = {
    # --- what to do -------------------------------------------------------
    # "scan"   find candidate pairs and print them        (the normal thing)
    # "review" step through the last scan and label pairs (m / x / s / q)
    # "score"  precision and recall on everything labelled so far
    # "sweep"  see how the edge cap trades precision against recall
    "command": "scan",

    # --- how strict to be -------------------------------------------------
    "min_confidence": 0.45,      # text-similarity floor, 0-1
    "min_rules_sim": 0.35,       # rule-text agreement floor
    "max_plausible_edge": 0.10,  # edges above this are mismatch evidence
    "min_edge": 0.01,            # ignore pairs with less edge than this
    "top": 25,                   # how many pairs to print

    # --- what to trade ----------------------------------------------------
    "size": 100,                 # contracts per leg, used for fees and depth
    "depth": 15,                 # fetch real order books for the top N pairs

    # --- what to fetch ----------------------------------------------------
    "min_volume_kalshi": 50,     # contracts traded
    "min_volume_poly": 5000,     # dollars of volume
    "max_days_out": 400,         # ignore markets settling further out
    "cache_ttl": 300,            # seconds; set 0 to always refetch
}

# ===========================================================================

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
POLY_GAMMA_API = "https://gamma-api.polymarket.com"
POLY_CLOB_API = "https://clob.polymarket.com"
USER_AGENT = "market-arb-scanner/2.0 (public market data reader)"

# Gamma silently caps `limit` at 100. Requesting more returns 100 rows, which
# a naive "short page means last page" check reads as the end of the data.
GAMMA_PAGE = 100
KALSHI_PAGE = 1000

# ---------------------------------------------------------------------------
# Fees
#
# Both venues price the taker fee off expected earnings, not off stake, so both
# are bell curves peaking at 50c: fee_per_contract = coeff * P * (1 - P).
# Only the coefficient and the rounding differ.
#
#   Kalshi:     coeff 0.07 for most categories, rounded UP to the next cent
#               for the order as a whole. The round-up dominates at small size.
#   Polymarket: coeff varies by category since the March 2026 V2 schedule.
#               Geopolitics and world events remain fee-free. Makers pay zero.
#
# Both schedules change. Verify against kalshi.com/fee-schedule and
# help.polymarket.com before trusting any number this script prints.
# ---------------------------------------------------------------------------

KALSHI_FEE_DEFAULT = 0.07

POLY_FEE_BY_CATEGORY = {
    "politics": 0.04,
    "finance": 0.04,
    "tech": 0.04,
    "mentions": 0.04,
    "sports": 0.05,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "crypto": 0.07,
    "geopolitics": 0.0,
    "world": 0.0,
    "world events": 0.0,
}
POLY_FEE_DEFAULT = 0.05


# Counters the pipeline fills in as it runs, so the same numbers that go to
# stderr as prose are also available as data. stderr stays the human channel;
# this is what --meta serialises for the dashboard. A module-level dict rather
# than a return value because every stage from fetch to filter contributes,
# and threading an accumulator through all of them would be noise.
RUN_STATS: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Text handling
# ---------------------------------------------------------------------------

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

DIRECTION_UP = {"above", "over", "greater", "more", "least", "exceed", "exceeds",
                "higher", "up"}
DIRECTION_DOWN = {"below", "under", "less", "fewer", "most", "lower", "down"}

_WORD_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_TAG_RE = re.compile(r"<[^>]+>")
_NUM_RE = re.compile(
    r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|k\b|m\b|bn\b|billion|million|thousand)?"
)

# SequenceMatcher is O(n*m). Its autojunk heuristic distorts ratios once a
# sequence passes 200 items, so it is off here -- but that removes the very
# speedup that made long comparisons tolerable, at a measured 5.4ms per call
# on 600-char rule texts. Multiplied by tens of thousands of candidate pairs
# that is several minutes of silent grinding.
#
# So character matching is confined to questions, which are short. Rule texts
# are compared by cosine on their token vectors instead: 0.0016ms per pair,
# and a better measure of agreement than character overlap in any case.
SEQ_WINDOW = 200


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


def is_year(value: float) -> bool:
    return float(value).is_integer() and 1900 <= value <= 2100


def extract_numbers(text: str) -> tuple[set[float], set[float]]:
    """Split a question's figures into (years, magnitudes).

    Years must be kept apart from thresholds. A relative-tolerance comparison
    treats 2026 and 2028 as identical -- the gap is 0.1% -- which passes two
    completely different questions as the same market. Years get exact-match
    treatment; magnitudes get the tolerance.
    """
    years: set[float] = set()
    magnitudes: set[float] = set()
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
        elif not suffix and is_year(value):
            years.add(value)
            continue
        magnitudes.add(round(value, 4))
    return years, magnitudes


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
    return SequenceMatcher(None, a[:SEQ_WINDOW], b[:SEQ_WINDOW], autojunk=False).ratio()


# ---------------------------------------------------------------------------
# Market model
# ---------------------------------------------------------------------------

Level = tuple[float, float]  # (price in dollars, size in contracts)


@dataclass
class Book:
    """Resting liquidity, best price first."""
    yes_asks: list[Level] = field(default_factory=list)
    no_asks: list[Level] = field(default_factory=list)

    def cost_to_buy(self, side: str, contracts: float) -> float | None:
        """Total dollars to buy `contracts`, or None if the book is too thin."""
        levels = self.yes_asks if side == "yes" else self.no_asks
        remaining, total = contracts, 0.0
        for price, size in levels:
            take = min(size, remaining)
            total += take * price
            remaining -= take
            if remaining <= 1e-9:
                return total
        return None

    def top(self, side: str) -> float | None:
        levels = self.yes_asks if side == "yes" else self.no_asks
        return levels[0][0] if levels else None

    def depth(self, side: str) -> float:
        levels = self.yes_asks if side == "yes" else self.no_asks
        return sum(size for _, size in levels)


@dataclass
class Market:
    venue: str
    market_id: str
    question: str
    rules: str
    url: str
    category: str
    yes_bid: float | None          # best price you can sell YES at, 0-1
    yes_ask: float | None          # best price you can buy YES at, 0-1
    no_bid: float | None
    no_ask: float | None
    last: float | None
    volume: float
    liquidity: float
    close_time: datetime | None
    settle_time: datetime | None
    token_ids: tuple[str, str] | None = None   # (yes token, no token) on Polymarket
    book: Book | None = None
    tokens: list[str] = field(default_factory=list)
    rule_tokens: list[str] = field(default_factory=list)
    vector: dict[str, float] = field(default_factory=dict)
    rule_vector: dict[str, float] = field(default_factory=dict)
    q_norm: str = ""
    r_norm: str = ""
    years: set[float] = field(default_factory=set)
    magnitudes: set[float] = field(default_factory=set)
    direction: str = ""
    negations: int = 0
    group_key: str = ""              # Kalshi event_ticker, for ladder detection
    ladder_size: int = 1             # >1 means one contract of a one-of-N set
    distinguishing: set = field(default_factory=set)

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
    def quotable(self) -> bool:
        """True if at least one side can actually be bought at a stated price."""
        return self.effective_yes_ask() is not None or self.effective_no_ask() is not None

    def effective_no_ask(self) -> float | None:
        if self.book is not None:
            top = self.book.top("no")
            if top is not None:
                return top
        if self.no_ask is not None:
            return self.no_ask
        if self.yes_bid is not None:
            return 1.0 - self.yes_bid
        return None

    def effective_yes_ask(self) -> float | None:
        if self.book is not None:
            top = self.book.top("yes")
            if top is not None:
                return top
        if self.yes_ask is not None:
            return self.yes_ask
        if self.no_bid is not None:
            return 1.0 - self.no_bid
        return None

    def horizon(self) -> datetime | None:
        return self.settle_time or self.close_time

    def prepare(self) -> None:
        self.tokens = tokenize(self.question)
        self.rule_tokens = tokenize(self.rules)
        self.years, self.magnitudes = extract_numbers(self.question)
        self.direction = extract_direction(self.question)
        self.negations = negation_count(self.question)
        self.q_norm = normalize(self.question)
        self.r_norm = normalize(self.rules)

    def fee_coeff(self, overrides: dict[str, float]) -> float:
        if self.venue in overrides:
            return overrides[self.venue]
        if self.venue == "kalshi":
            return KALSHI_FEE_DEFAULT
        return POLY_FEE_BY_CATEGORY.get(self.category.lower(), POLY_FEE_DEFAULT)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class FetchError(RuntimeError):
    pass


RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def http_get_json(url: str, timeout: int = 30, retries: int = 3):
    """GET JSON, retrying only on transient failures.

    A 404 or a 400 is a bug in the request, not weather. Retrying it three
    times with backoff just makes the bug take nine seconds to surface.
    """
    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in RETRY_STATUS:
                raise FetchError(f"{url} returned HTTP {exc.code} {exc.reason}") from exc
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
            continue
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            time.sleep(1.5 * (attempt + 1))
            continue
        text = body.decode("utf-8", "replace").lstrip()
        if text[:1] not in ("{", "["):
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


# Kalshi's fixed-point migration retired the integer-cent price fields
# (yes_bid, no_ask, last_price ...) in favour of `_dollars` equivalents, which
# arrive as fixed-point decimal strings such as "0.4500". Subpenny pricing also
# means a price is no longer guaranteed to land on a whole cent. Resolve each
# logical field against a candidate list so the parser survives this migration
# and the next one, rather than silently reading zero markets.
KALSHI_FIELDS = {
    "yes_bid":    ("yes_bid_dollars", "yes_bid"),
    "yes_ask":    ("yes_ask_dollars", "yes_ask"),
    "no_bid":     ("no_bid_dollars", "no_bid"),
    "no_ask":     ("no_ask_dollars", "no_ask"),
    "last_price": ("last_price_dollars", "last_price"),
    "volume":     ("volume", "volume_fp", "volume_24h"),
    "open_interest": ("open_interest", "open_interest_fp"),
    "liquidity":  ("liquidity_dollars", "liquidity"),
}


def pick(row: dict, names) -> tuple[str, object] | tuple[None, None]:
    """First present field from a candidate list, with the name that matched."""
    for name in names:
        if name in row and row[name] is not None:
            return name, row[name]
    return None, None


def probability(value, field_name: str = "") -> float | None:
    """Parse a Kalshi price into 0-1, accepting dollars or legacy cents.

    A `_dollars` field is already a probability once parsed. A legacy field is
    integer cents. Both use 0 and the top of the range to mean 'no resting
    order', so those collapse to None either way.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if field_name.endswith("_dollars"):
        return number if 0 < number < 1 else None
    if 0 < number < 100:
        return number / 100.0
    return None


def kalshi_price(row: dict, logical: str) -> float | None:
    name, value = pick(row, KALSHI_FIELDS[logical])
    return probability(value, name or "")


def kalshi_number(row: dict, logical: str) -> float:
    _, value = pick(row, KALSHI_FIELDS[logical])
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def cents(value) -> float | None:
    """Legacy helper retained for the orderbook path."""
    return probability(value)


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


# ---------------------------------------------------------------------------
# Venue adapters
# ---------------------------------------------------------------------------

def fetch_kalshi(max_markets: int, horizon_days: int, scan_limit: int,
                 series: list[str], verbose: bool) -> list[dict]:
    """Pull open Kalshi markets, then keep the most active ones.

    Kalshi lists far more open contracts than any other venue -- auto-generated
    strike ladders, hourly weather, per-game sports -- and the great majority
    have never traded and carry no resting quotes. `/markets` returns them in
    no useful order and offers no volume filter, so taking the first N rows
    lands the entire budget in that dead tail: 4000 fetched, 0 quotable.

    So scan wide, rank by activity, and keep the top slice. The wide scan is
    cached, so it costs one slow run rather than one per invocation.
    """
    raw: list[dict] = []
    now = datetime.now(timezone.utc)
    base = {"status": "open", "min_close_ts": int(now.timestamp())}
    if horizon_days:
        base["max_close_ts"] = int((now + timedelta(days=horizon_days)).timestamp())

    for series_ticker in (series or [None]):
        cursor = ""
        while len(raw) < scan_limit:
            params = dict(base)
            params["limit"] = min(KALSHI_PAGE, scan_limit - len(raw))
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            payload = http_get_json(f"{KALSHI_API}/markets?" + urllib.parse.urlencode(params))
            batch = payload.get("markets") or []
            raw.extend(batch)
            cursor = payload.get("cursor") or ""
            if verbose:
                print(f"  kalshi: scanned {len(raw)}", file=sys.stderr)
            if not cursor or not batch:
                break

    def activity(row: dict) -> tuple[float, float, float]:
        return (kalshi_number(row, "volume"), kalshi_number(row, "open_interest"),
                kalshi_number(row, "liquidity"))

    quoted = [r for r in raw
              if kalshi_price(r, "yes_ask") is not None
              or kalshi_price(r, "yes_bid") is not None]
    print(f"kalshi: scanned {len(raw)} open markets, {len(quoted)} carry a resting quote",
          file=sys.stderr)
    RUN_STATS["kalshi_scanned"] = len(raw)
    RUN_STATS["kalshi_quoted"] = len(quoted)

    pool = quoted or raw
    pool.sort(key=activity, reverse=True)
    return pool[:max_markets] if max_markets else pool


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
        category=(row.get("category") or "").strip(),
        group_key=event,
        yes_bid=kalshi_price(row, "yes_bid"),
        yes_ask=kalshi_price(row, "yes_ask"),
        no_bid=kalshi_price(row, "no_bid"),
        no_ask=kalshi_price(row, "no_ask"),
        last=kalshi_price(row, "last_price"),
        volume=kalshi_number(row, "volume"),
        liquidity=kalshi_number(row, "liquidity"),
        close_time=parse_time(row.get("close_time")),
        # Kalshi's close_time is when trading stops; expiration_time is when the
        # contract settles. Polymarket's endDate is a settlement date, so
        # comparing it against close_time systematically penalises true matches.
        settle_time=parse_time(row.get("expiration_time") or row.get("close_time")),
    )


def fetch_polymarket(max_markets: int, min_volume: float, horizon_days: int,
                     verbose: bool) -> list[dict]:
    """Page through Gamma using keyset cursors.

    The legacy offset-paginated /markets was retired on 1 May 2026 and now
    rejects `offset` outright with "offset too large, use /markets/keyset".
    The replacement takes `after_cursor` and echoes `next_cursor` until the
    final page, which omits it.

    Two traps this guards against:

      * The parameter is `after_cursor`, not `cursor`. Sending `cursor` is
        accepted and silently ignored, so every request returns page one and
        the loop spins forever collecting duplicates.
      * Keyset paging imposes its own ordering, so `order=volumeNum` is not
        honoured here. Sort by volume client-side after the fetch instead of
        assuming the first page holds the busiest markets.
    """
    seen: dict[str, dict] = {}
    cursor = ""
    now = datetime.now(timezone.utc)
    base = {"closed": "false", "active": "true", "archived": "false"}
    if min_volume > 0:
        base["volume_num_min"] = min_volume
    if horizon_days:
        base["end_date_min"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        base["end_date_max"] = (now + timedelta(days=horizon_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    while len(seen) < max_markets:
        params = dict(base)
        params["limit"] = min(GAMMA_PAGE, max_markets - len(seen))
        if cursor:
            params["after_cursor"] = cursor
        payload = http_get_json(f"{POLY_GAMMA_API}/markets/keyset?"
                                + urllib.parse.urlencode(params))

        if isinstance(payload, list):        # tolerate a bare-array response
            batch, next_cursor = payload, ""
        else:
            batch = (payload or {}).get("markets") or (payload or {}).get("data") or []
            next_cursor = (payload or {}).get("next_cursor") or ""

        if not batch:
            break

        before = len(seen)
        for row in batch:
            key = str(row.get("conditionId") or row.get("id") or row.get("slug") or "")
            if key:
                seen.setdefault(key, row)
        if verbose:
            print(f"  polymarket: {len(seen)} markets", file=sys.stderr)

        # If a page adds nothing new, or the cursor stops advancing, the server
        # is handing back the same page. Stop rather than loop to the cap.
        if len(seen) == before or not next_cursor or next_cursor == cursor:
            if next_cursor and next_cursor == cursor:
                print("warning: polymarket cursor stopped advancing; "
                      "stopping pagination early", file=sys.stderr)
            break
        cursor = next_cursor

    rows = list(seen.values())
    rows.sort(key=lambda r: float(r.get("volumeNum") or r.get("volume") or 0), reverse=True)
    return rows


def polymarket_to_market(row: dict, reject: Counter) -> Market | None:
    outcomes = [str(o).strip() for o in json_field(row.get("outcomes"), ["Yes", "No"])]
    if len(outcomes) != 2:
        reject["poly: not a binary book"] += 1
        return None

    labels = {o.lower() for o in outcomes}
    if labels != {"yes", "no"}:
        # Outcomes like ["Trump", "Newsom"] are binary but have no canonical
        # YES side. Assuming outcome[0] is YES silently inverts the contract
        # against whichever Kalshi market it matches, which is the single most
        # expensive mistake this program can make. Refuse instead of guessing.
        reject["poly: binary but not yes/no labelled"] += 1
        return None

    if row.get("enableOrderBook") is False:
        reject["poly: order book disabled"] += 1
        return None

    yes_index = 0 if outcomes[0].lower() == "yes" else 1

    prices = json_field(row.get("outcomePrices"), [])
    last = None
    if len(prices) == len(outcomes):
        last = decimal_price(prices[yes_index])
    if last is None:
        last = decimal_price(row.get("lastTradePrice"))

    best_bid = decimal_price(row.get("bestBid"))
    best_ask = decimal_price(row.get("bestAsk"))
    if yes_index == 1 and best_bid is not None and best_ask is not None:
        best_bid, best_ask = 1 - best_ask, 1 - best_bid

    clob_ids = json_field(row.get("clobTokenIds"), [])
    token_ids = None
    if isinstance(clob_ids, list) and len(clob_ids) == 2:
        token_ids = (str(clob_ids[yes_index]), str(clob_ids[1 - yes_index]))

    question = (row.get("question") or "").strip()
    group = (row.get("groupItemTitle") or "").strip()
    if group and group.lower() not in question.lower():
        question = f"{question} — {group}"

    category = (row.get("category") or "").strip()
    if not category:
        events = row.get("events") or []
        if isinstance(events, list) and events and isinstance(events[0], dict):
            category = str(events[0].get("category") or "").strip()

    slug = row.get("slug") or ""
    settle = parse_time(row.get("endDateIso") or row.get("endDate"))
    return Market(
        venue="polymarket",
        market_id=str(row.get("conditionId") or row.get("id") or slug),
        question=question,
        rules=(row.get("description") or "").strip(),
        url=f"https://polymarket.com/market/{slug}" if slug else "https://polymarket.com",
        category=category,
        yes_bid=best_bid,
        yes_ask=best_ask,
        # These complements are a placeholder. YES and NO are separate ERC-1155
        # tokens with independent books on Polymarket, so 1 - yes_bid is not the
        # real NO ask. fetch_books replaces them with the actual quotes.
        no_bid=(1 - best_ask) if best_ask is not None else None,
        no_ask=(1 - best_bid) if best_bid is not None else None,
        last=last,
        volume=float(row.get("volumeNum") or row.get("volume") or 0),
        liquidity=float(row.get("liquidityNum") or row.get("liquidity") or 0),
        close_time=settle,
        settle_time=settle,
        token_ids=token_ids,
    )


# ---------------------------------------------------------------------------
# Order books
# ---------------------------------------------------------------------------

def kalshi_book(ticker: str) -> Book | None:
    """Fetch and transform a Kalshi order book.

    Kalshi publishes resting *bids* on both sides. To buy YES you cross against
    someone bidding NO, so a NO bid at 55c is a YES ask at 45c. Reading the
    'yes' array as YES asks inverts the whole book.
    """
    payload = http_get_json(f"{KALSHI_API}/markets/{urllib.parse.quote(ticker)}/orderbook")
    ob = (payload or {}).get("orderbook") or {}
    yes_bids = ob.get("yes") or []
    no_bids = ob.get("no") or []

    def to_asks(bids) -> list[Level]:
        out: list[Level] = []
        for entry in bids or []:
            try:
                if isinstance(entry, dict):
                    name, value = pick(entry, ("price_dollars", "price"))
                    bid = probability(value, name or "")
                    size = float(entry.get("size") or entry.get("size_fp") or 0)
                else:
                    raw_price, size = entry[0], float(entry[1])
                    # A level under 1 is already dollars; 1-99 is legacy cents.
                    bid = (float(raw_price) if 0 < float(raw_price) < 1
                           else probability(raw_price))
            except (TypeError, ValueError, IndexError, KeyError):
                continue
            if bid is None or size <= 0:
                continue
            # Round to Kalshi's finest (sub-penny) granularity so the
            # complement of a clean price is a clean price, not 0.44999...96.
            ask = round(1.0 - bid, 4)
            if 0 < ask < 1:
                out.append((ask, size))
        out.sort(key=lambda level: level[0])
        return out

    return Book(yes_asks=to_asks(no_bids), no_asks=to_asks(yes_bids))


def polymarket_book(token_id: str) -> list[Level]:
    payload = http_get_json(f"{POLY_CLOB_API}/book?token_id={urllib.parse.quote(token_id)}")
    out: list[Level] = []
    for entry in (payload or {}).get("asks") or []:
        try:
            price = float(entry["price"])
            size = float(entry["size"])
        except (TypeError, ValueError, KeyError):
            continue
        if 0 < price < 1 and size > 0:
            out.append((price, size))
    out.sort(key=lambda level: level[0])
    return out


def fetch_books(markets: list[Market], verbose: bool) -> int:
    """Attach real books to the given markets. Returns how many succeeded."""
    ok = 0
    for market in markets:
        if market.book is not None:
            ok += 1
            continue
        try:
            if market.venue == "kalshi":
                market.book = kalshi_book(market.market_id)
            elif market.token_ids:
                yes_token, no_token = market.token_ids
                market.book = Book(yes_asks=polymarket_book(yes_token),
                                   no_asks=polymarket_book(no_token))
        except FetchError as exc:
            if verbose:
                print(f"  book fetch failed for {market.market_id}: {exc}", file=sys.stderr)
            continue
        if market.book is not None:
            ok += 1
    return ok


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    kalshi: Market
    poly: Market
    title_sim: float
    rules_sim: float
    date_sim: float
    penalty: float
    confidence: float
    days_apart: float | None
    warnings: list[str]
    polarity_suspect: bool = False
    rules_known: bool = True

    mid_kalshi: float | None = None
    mid_poly: float | None = None
    mid_gap: float = 0.0

    edge: float = 0.0            # per contract, after fees, at requested size
    edge_leg: str = ""
    cost: float | None = None    # per contract, after fees
    filled: float = 0.0          # contracts actually available
    days_to_settle: float | None = None
    annualised: float | None = None
    priced_at_top: bool = True   # True if only top-of-book was available
    implausible: bool = False    # edge too large to be a real dislocation


IMPLAUSIBLE_MARK = "exceeds any plausible cross-venue dislocation"


def date_similarity(a: datetime | None, b: datetime | None) -> tuple[float, float | None]:
    if a is None or b is None:
        return 0.5, None
    days = abs((a - b).total_seconds()) / 86400.0
    if days <= 2:
        score = 1.0
    elif days <= 7:
        score = 0.9
    elif days <= 14:
        score = 0.75
    elif days <= 30:
        score = 0.5
    elif days <= 75:
        score = 0.25
    else:
        score = 0.05
    return score, days


def number_agreement(k: Market, p: Market) -> tuple[float, list[str]]:
    """Penalise pairs whose thresholds or years differ.

    Years are compared exactly. Magnitudes get a 1% relative tolerance on the
    largest value, which is almost always the threshold.
    """
    penalty = 0.0
    notes: list[str] = []

    if k.years != p.years:
        only_k = sorted(k.years - p.years)
        only_p = sorted(p.years - k.years)
        if only_k and only_p:
            penalty += 0.45
            notes.append(f"years differ ({only_k[0]:.0f} vs {only_p[0]:.0f})")
        else:
            penalty += 0.12
            notes.append("one side names a year the other does not")

    a, b = k.magnitudes, p.magnitudes
    if not a and not b:
        return penalty, notes
    if not a or not b:
        return penalty + 0.10, notes + ["one side states a numeric threshold, the other does not"]

    union = a | b
    jaccard = len(a & b) / len(union) if union else 1.0
    penalty += 0.20 * (1.0 - jaccard)

    max_a, max_b = max(a), max(b)
    scale = max(abs(max_a), abs(max_b), 1.0)
    if abs(max_a - max_b) / scale > 0.01:
        return penalty + 0.40, notes + [f"thresholds differ ({max_a:g} vs {max_b:g})"]
    if jaccard < 0.6:
        notes.append(f"secondary figures differ ({sorted(a ^ b)[:4]})")
    return penalty, notes


def best_possible(k: Market, p: Market, weights: tuple[float, float, float]) -> float:
    """Ceiling on confidence using only the cheap cosine, for prefiltering.

    Every other term is bounded by 1, and the character ratio can add at most
    0.35 to the title term, so this over-estimates and never discards a pair
    that full scoring would have kept.
    """
    w_title, w_rules, w_date = weights
    return w_title * (0.65 * cosine(k.vector, p.vector) + 0.35) + w_rules + w_date


def score_pair(k: Market, p: Market, weights: tuple[float, float, float]) -> Pair:
    w_title, w_rules, w_date = weights

    title_sim = 0.65 * cosine(k.vector, p.vector) + 0.35 * ratio(k.q_norm, p.q_norm)

    rules_known = bool(k.rules and p.rules)
    rules_sim = cosine(k.rule_vector, p.rule_vector) if rules_known else 0.0

    date_sim, days_apart = date_similarity(k.horizon(), p.horizon())
    penalty, warnings = number_agreement(k, p)

    if rules_known:
        base = w_title * title_sim + w_rules * rules_sim + w_date * date_sim
    else:
        scale = w_title + w_date
        base = ((w_title / scale) * title_sim + (w_date / scale) * date_sim) * 0.95
        warnings.append("resolution text missing on one venue — rules not compared")

    if k.direction and p.direction and k.direction != p.direction:
        penalty += 0.35
        warnings.append("threshold direction disagrees (above vs below)")

    polarity_suspect = (k.negations % 2) != (p.negations % 2)
    if polarity_suspect:
        penalty += 0.15
        warnings.append("possible polarity flip — one side may be the complement of "
                        "the other, so YES is not comparable to YES; edge not computed")
    if days_apart is not None and days_apart > 30:
        warnings.append(f"settlement dates {days_apart:.0f} days apart")

    return Pair(
        kalshi=k, poly=p,
        title_sim=title_sim, rules_sim=rules_sim, date_sim=date_sim,
        penalty=penalty, confidence=max(0.0, min(1.0, base - penalty)),
        days_apart=days_apart, warnings=warnings, polarity_suspect=polarity_suspect,
        rules_known=rules_known,
    )


def candidate_pairs(kalshi: list[Market], poly: list[Market], idf: dict[str, float],
                    max_candidates: int, common_frac: float) -> dict[int, set[int]]:
    """Inverted index over question tokens, ranked by IDF weight.

    The previous version hard-dropped any token appearing in more than 5% of
    Polymarket questions. In politics that discards 'trump', 'senate', 'fed' --
    exactly the tokens that identify a market -- and leaves the highest-volume
    contracts with no candidates at all. IDF already down-weights common terms,
    so the cutoff only needs to catch pathological tokens.
    """
    index: dict[str, list[int]] = defaultdict(list)
    for i, market in enumerate(poly):
        for token in set(market.tokens):
            index[token].append(i)

    doc_count = max(len(poly), 1)
    ceiling = max(200, common_frac * doc_count)
    common = {t for t, ids in index.items() if len(ids) > ceiling}

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


def mark_ladders(markets: list[Market]) -> int:
    """Flag Kalshi markets that are members of a one-of-N ladder.

    Kalshi splits a single question into one contract per outcome, all sharing
    an event_ticker: KXARREST-27JAN-LJAM, -ASCH, -HCLIN and so on. Polymarket
    usually carries one standalone binary for one of those outcomes. Matching a
    ladder member against a standalone contract pairs "Will Letitia James be
    arrested" with "Obama arrested before 2027" -- six of the top 25 on the
    first clean run were exactly this.

    The distinguishing tokens of a member are the ones its siblings do not
    share. Requiring at least one of them to appear on the other side is a
    structural fix that no amount of threshold tuning would achieve.
    """
    groups: dict[str, list[Market]] = defaultdict(list)
    for market in markets:
        if market.venue == "kalshi" and market.group_key:
            groups[market.group_key].append(market)

    flagged = 0
    for members in groups.values():
        if len(members) < 2:
            continue
        shared = set.intersection(*(set(m.tokens) for m in members))
        for member in members:
            member.ladder_size = len(members)
            member.distinguishing = set(member.tokens) - shared
            flagged += 1
    return flagged


def ladder_mismatch(k: Market, p: Market) -> bool:
    """True if k is a ladder member whose distinguishing tokens are absent."""
    if k.ladder_size < 2 or not k.distinguishing:
        return False
    return not (k.distinguishing & set(p.tokens))


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

def kalshi_fee_total(price: float, contracts: float, coeff: float) -> float:
    """Kalshi rounds the order's fee UP to the next cent.

    At 100 contracts at 45c the round-up costs a twentieth of a cent per
    contract. At 3 contracts it costs a third of a cent. Ignoring it makes
    small trades look profitable when they are not.
    """
    return math.ceil(coeff * contracts * price * (1.0 - price) * 100.0) / 100.0


def poly_fee_total(price: float, contracts: float, coeff: float) -> float:
    """Same bell curve, rounded to five decimals rather than up to a cent."""
    return round(coeff * contracts * price * (1.0 - price), 5)


def _leg_cost(buy: Market, buy_side: str, other: Market, other_side: str,
              contracts: float, overrides: dict[str, float]) -> tuple[float, float] | None:
    """Total cost including fees for buying both sides. Returns (cost, filled)."""
    legs = ((buy, buy_side), (other, other_side))
    total = 0.0
    for market, side in legs:
        if market.book is not None:
            gross = market.book.cost_to_buy(side, contracts)
            if gross is None:
                return None
            avg = gross / contracts
        else:
            avg = (market.effective_yes_ask() if side == "yes"
                   else market.effective_no_ask())
            if avg is None:
                return None
            gross = avg * contracts
        coeff = market.fee_coeff(overrides)
        fee = (kalshi_fee_total(avg, contracts, coeff) if market.venue == "kalshi"
               else poly_fee_total(avg, contracts, coeff))
        total += gross + fee
    return total, contracts


def passes_gates(confidence: float, rules_sim: float | None, edge: float,
                 min_confidence: float, min_rules_sim: float,
                 max_plausible_edge: float) -> tuple[bool, str]:
    """The single definition of 'is this pair worth a human reading the rules'.

    Both scan() and the evaluation harness call this. Reimplementing the
    thresholds in the harness would let the two drift, and a filter you cannot
    measure is a filter you cannot tune.

    `rules_sim` is None when one venue carries no rule text: an absent text
    cannot be held to the floor. A measured 0.0, though, is total disagreement
    between two texts that both exist, and must fail the gate -- `if rules_sim`
    would wave it through on falsiness.
    """
    if confidence < min_confidence:
        return False, "confidence below threshold"
    if rules_sim is not None and rules_sim < min_rules_sim:
        return False, "rule texts disagree"
    if edge > max_plausible_edge:
        return False, "edge implausibly large"
    return True, "kept"


def price_pair(pair: Pair, contracts: float, overrides: dict[str, float],
               max_plausible: float = 1.0) -> None:
    k, p = pair.kalshi, pair.poly

    # Reset before repricing. Pairs are priced twice -- once on top of book to
    # build the shortlist, then again after real books arrive. Without this,
    # a pair whose depth pass finds insufficient size keeps the optimistic
    # top-of-book edge from the first pass and reports "no two-sided liquidity
    # at requested size" alongside a price it can no longer honour.
    # The implausible flag and its warning reset too: a silly top-of-book quote
    # that real depth corrects to something modest is exactly the case the
    # depth pass exists for, and a sticky flag would discard the pair on the
    # strength of a price it no longer quotes.
    pair.edge, pair.cost, pair.filled, pair.annualised = 0.0, None, 0.0, None
    pair.edge_leg = ""
    pair.implausible = False
    pair.warnings = [w for w in pair.warnings if IMPLAUSIBLE_MARK not in w]
    pair.mid_kalshi, pair.mid_poly = k.mid, p.mid
    if pair.mid_kalshi is not None and pair.mid_poly is not None:
        pair.mid_gap = pair.mid_kalshi - pair.mid_poly
    pair.priced_at_top = k.book is None or p.book is None

    horizon = max([t for t in (k.horizon(), p.horizon()) if t is not None], default=None)
    if horizon is not None:
        pair.days_to_settle = max(
            (horizon - datetime.now(timezone.utc)).total_seconds() / 86400.0, 0.5)

    if pair.polarity_suspect:
        pair.edge_leg = "not computed — check polarity by hand"
        return

    best = None
    for buy, buy_side, other, other_side, label in (
        (k, "yes", p, "no", "buy YES kalshi / NO polymarket"),
        (p, "yes", k, "no", "buy YES polymarket / NO kalshi"),
    ):
        result = _leg_cost(buy, buy_side, other, other_side, contracts, overrides)
        if result is None:
            continue
        total, filled = result
        per_contract = total / filled
        if best is None or per_contract < best[0]:
            best = (per_contract, label, filled)

    if best is None:
        pair.edge_leg = "no two-sided liquidity at requested size"
        return

    cost, label, filled = best
    pair.cost, pair.edge_leg, pair.filled = cost, label, filled
    pair.edge = 1.0 - cost

    # Capital is fully collateralised on both legs until settlement, with no
    # cross-margining. A 0.5pp edge over six days and one over four hundred
    # are not the same trade, and sorting on raw edge conflates them.
    # An edge far larger than any real cross-venue dislocation is evidence the
    # two contracts ask different questions, not evidence of free money. On the
    # first live run every one of the top 25 "opportunities" sat between 25 and
    # 87pp, and every one was a mismatch: a primary-nominee market paired with a
    # general-election market, Israel-Colombia against Israel-Lebanon, a gas
    # price of $4.20 against XRP at $4.20.
    if pair.edge > max_plausible:
        pair.implausible = True
        pair.warnings.append(
            f"edge of {pair.edge * 100:.0f}pp {IMPLAUSIBLE_MARK} "
            f"— treat as evidence the questions differ, not as an arb")

    if pair.days_to_settle and cost > 0 and pair.edge > 0:
        try:
            pair.annualised = (1.0 / cost) ** (365.0 / pair.days_to_settle) - 1.0
        except (OverflowError, ZeroDivisionError):
            pair.annualised = None


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def cache_key(args, venue: str) -> str:
    """Cache identity has to include the arguments that shaped the fetch.

    Keying on the venue alone means a run with --max-markets 100 poisons the
    next run with --max-markets 4000 for the whole TTL, and you spend an
    evening debugging a filter that was never the problem.
    """
    shape = json.dumps({
        "venue": venue,
        "max_markets": args.max_markets,
        "max_days_out": args.max_days_out,
        "min_volume_poly": args.min_volume_poly,
        "kalshi_scan_limit": args.kalshi_scan_limit,
        "kalshi_series": sorted(args.kalshi_series or []),
    }, sort_keys=True)
    digest = hashlib.sha1(shape.encode()).hexdigest()[:10]
    return os.path.join(args.cache_dir, f"{venue}-{digest}.json")


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
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass


def load_raw(args) -> tuple[list[dict], list[dict]]:
    if args.offline:
        def read(name):
            with open(os.path.join(args.offline, name), encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get("markets", data) if isinstance(data, dict) else data
        return read("kalshi.json"), read("polymarket.json")

    os.makedirs(args.cache_dir, exist_ok=True)

    kalshi_path = cache_key(args, "kalshi")
    kalshi_raw = read_cache(kalshi_path, args.cache_ttl)
    if kalshi_raw is None:
        print("fetching kalshi...", file=sys.stderr)
        kalshi_raw = fetch_kalshi(args.max_markets, args.max_days_out,
                                  args.kalshi_scan_limit, args.kalshi_series, args.verbose)
        write_cache(kalshi_path, kalshi_raw)
    else:
        print(f"kalshi: {len(kalshi_raw)} markets from cache", file=sys.stderr)

    poly_path = cache_key(args, "polymarket")
    poly_raw = read_cache(poly_path, args.cache_ttl)
    if poly_raw is None:
        print("fetching polymarket...", file=sys.stderr)
        poly_raw = fetch_polymarket(args.max_markets, args.min_volume_poly,
                                    args.max_days_out, args.verbose)
        write_cache(poly_path, poly_raw)
    else:
        print(f"polymarket: {len(poly_raw)} markets from cache", file=sys.stderr)

    return kalshi_raw, poly_raw


def apply_filters(markets: list[Market], min_volume: float, horizon_days: int,
                  reject: Counter, tag: str) -> list[Market]:
    """Filter with attrition accounting, so an empty result explains itself.

    Every failing reason is recorded, not just the first one hit. Stopping at
    the first makes the table lie by omission: a contract that is both dormant
    and below the volume floor gets filed under whichever check happens to run
    first, and the other cause never appears at all. Overlapping counts are
    the point -- they are what tells you whether one filter or four is the
    thing emptying the pipeline.
    """
    now = datetime.now(timezone.utc)
    kept: list[Market] = []
    for market in markets:
        reasons: list[str] = []
        if not market.question:
            reasons.append(f"{tag}: no question text")
        if not market.quotable:
            reasons.append(f"{tag}: no live quote on either side")
        if market.volume < min_volume:
            reasons.append(f"{tag}: volume below {min_volume:g}")
        horizon = market.horizon()
        if horizon_days and horizon is not None:
            days = (horizon - now).total_seconds() / 86400.0
            if days < 0:
                reasons.append(f"{tag}: already past settlement")
            elif days > horizon_days:
                reasons.append(f"{tag}: settles beyond {horizon_days}d horizon")
        if reasons:
            for reason in reasons:
                reject[reason] += 1
            reject[f"{tag}: DROPPED (any reason)"] += 1
            continue
        kept.append(market)
    return kept


def diagnose_quotes(rows: list[dict], tag: str) -> None:
    """Show the schema the venue actually returned, not the one we expected.

    The earlier version checked a hardcoded field list and reported "absent
    entirely", which is true but useless -- it cannot tell you that yes_bid
    became yes_bid_dollars. Print the keys that are present, with a sample
    value each, so a renamed field is visible on sight.
    """
    if not rows:
        return
    keys: Counter = Counter()
    samples: dict[str, object] = {}
    for row in rows[:500]:
        for name, value in row.items():
            keys[name] += 1
            if name not in samples and value not in (None, "", [], {}):
                samples[name] = value

    print(f"{tag}: no usable markets. Schema seen across {min(len(rows), 500)} rows:",
          file=sys.stderr)
    price_like = [k for k in keys if re.search(
        r"(bid|ask|price|volume|interest|liquidity)", k, re.I)]
    for name in sorted(price_like):
        sample = json.dumps(samples.get(name), default=str)[:40]
        print(f"    {name:<28} in {keys[name]:>5} rows   e.g. {sample}", file=sys.stderr)
    if not price_like:
        print(f"    no price-like fields at all. All keys: "
              f"{', '.join(sorted(keys))[:400]}", file=sys.stderr)

    # The KALSHI_FIELDS hint only makes sense for Kalshi rows; Polymarket
    # fields are read by name in polymarket_to_market.
    if tag == "kalshi":
        expected = {n for names in KALSHI_FIELDS.values() for n in names}
        unknown = [k for k in price_like if k not in expected]
        if unknown:
            print(f"    !! price-like fields this build does not read: "
                  f"{', '.join(sorted(unknown))}", file=sys.stderr)
            print("       add them to KALSHI_FIELDS near the top of this file.",
                  file=sys.stderr)


def report_attrition(reject: Counter, kalshi_raw: int, poly_raw: int,
                     kalshi_kept: int, poly_kept: int) -> None:
    print(f"kalshi     {kalshi_raw:>6} fetched -> {kalshi_kept:>5} usable", file=sys.stderr)
    print(f"polymarket {poly_raw:>6} fetched -> {poly_kept:>5} usable", file=sys.stderr)
    if reject:
        print("dropped:", file=sys.stderr)
        for reason, count in reject.most_common():
            print(f"    {count:>6}  {reason}", file=sys.stderr)


def scan(args) -> tuple[list[Pair], Counter]:
    reject: Counter = Counter()
    RUN_STATS.clear()
    RUN_STATS["started_at"] = time.time()
    kalshi_raw, poly_raw = load_raw(args)

    kalshi = [kalshi_to_market(r) for r in kalshi_raw]
    poly = [m for m in (polymarket_to_market(r, reject) for r in poly_raw) if m is not None]

    kalshi = apply_filters(kalshi, args.min_volume_kalshi, args.max_days_out, reject, "kalshi")
    poly = apply_filters(poly, args.min_volume_poly, args.max_days_out, reject, "poly")

    report_attrition(reject, len(kalshi_raw), len(poly_raw), len(kalshi), len(poly))
    RUN_STATS.update(kalshi_fetched=len(kalshi_raw), poly_fetched=len(poly_raw),
                     kalshi_usable=len(kalshi), poly_usable=len(poly))
    if not kalshi:
        diagnose_quotes(kalshi_raw, "kalshi")
    if not poly:
        diagnose_quotes(poly_raw, "polymarket")
    if not kalshi or not poly:
        return [], reject

    for market in kalshi + poly:
        market.prepare()

    idf = build_idf([m.tokens for m in kalshi + poly])
    rules_idf = build_idf([m.rule_tokens for m in kalshi + poly if m.rule_tokens])
    for market in kalshi + poly:
        market.vector = tfidf_vector(market.tokens, idf)
        market.rule_vector = tfidf_vector(market.rule_tokens, rules_idf)

    ladder_members = mark_ladders(kalshi)
    RUN_STATS["ladder_members"] = ladder_members
    if ladder_members:
        print(f"flagged {ladder_members} kalshi markets as one-of-N ladder members",
              file=sys.stderr)

    weights = (args.weight_title, args.weight_rules, args.weight_date)
    candidates = candidate_pairs(kalshi, poly, idf, args.max_candidates, args.common_token_frac)

    total = sum(len(v) for v in candidates.values())
    print(f"scoring {total:,} candidate pairs...", file=sys.stderr)

    best_by_kalshi: dict[int, Pair] = {}
    scored = skipped = skipped_ladder = 0
    started = time.time()
    for ki, poly_indices in candidates.items():
        for pi in poly_indices:
            scored += 1
            if scored % 25_000 == 0:
                rate = scored / max(time.time() - started, 1e-6)
                remaining = (total - scored) / rate
                print(f"  {scored:,}/{total:,} ({100*scored/total:.0f}%) "
                      f"~{remaining:.0f}s left", file=sys.stderr)
            # Skip pairs whose ceiling cannot reach the threshold, before
            # paying for the character ratio.
            if best_possible(kalshi[ki], poly[pi], weights) < args.min_confidence:
                skipped += 1
                continue
            if not args.no_ladder_filter and ladder_mismatch(kalshi[ki], poly[pi]):
                skipped_ladder += 1
                continue
            pair = score_pair(kalshi[ki], poly[pi], weights)
            # Edge is not known yet at this stage, so pass 0 for it; the
            # implausible-edge gate is applied after pricing.
            ok, _ = passes_gates(pair.confidence,
                                 pair.rules_sim if pair.rules_known else None, 0.0,
                                 args.min_confidence, args.min_rules_sim,
                                 args.max_plausible_edge)
            if not ok:
                continue
            if pair.days_apart is not None and pair.days_apart > args.max_date_diff:
                continue
            current = best_by_kalshi.get(ki)
            if current is None or pair.confidence > current.confidence:
                best_by_kalshi[ki] = pair

    pairs = list(best_by_kalshi.values())
    print(f"scored {scored:,} pairs in {time.time() - started:.1f}s "
          f"({skipped:,} skipped by prefilter, {skipped_ladder:,} by ladder rule), "
          f"{len(pairs)} above confidence "
          f"{args.min_confidence}", file=sys.stderr)
    RUN_STATS.update(candidate_pairs=total, scored=scored, skipped_prefilter=skipped,
                     skipped_ladder=skipped_ladder, above_confidence=len(pairs),
                     scoring_seconds=round(time.time() - started, 1))

    overrides = {}
    if args.kalshi_fee_coeff is not None:
        overrides["kalshi"] = args.kalshi_fee_coeff
    if args.poly_fee_coeff is not None:
        overrides["polymarket"] = args.poly_fee_coeff

    # Two-stage pricing: screen everything on top-of-book, then spend the
    # request budget fetching real depth only for the pairs that survive.
    for pair in pairs:
        price_pair(pair, args.size, overrides, args.max_plausible_edge)

    if args.depth and not args.offline:
        pairs.sort(key=lambda p: p.edge, reverse=True)
        eligible = [p for p in pairs if p.edge >= args.min_edge]
        # Plausible pairs first: they are the likeliest real trades. Implausible
        # ones take the leftover slots -- since price_pair resets the flag, real
        # depth can demote a silly top-of-book quote and rescue the pair. The
        # old order let fake 80pp edges monopolise the book budget.
        shortlist = ([p for p in eligible if not p.implausible]
                     + [p for p in eligible if p.implausible])[:args.depth]
        RUN_STATS["depth_priced"] = len(shortlist)
        if shortlist:
            print(f"fetching order books for top {len(shortlist)} pairs...", file=sys.stderr)
            fetch_books([p.kalshi for p in shortlist] + [p.poly for p in shortlist],
                        args.verbose)
            for pair in shortlist:
                price_pair(pair, args.size, overrides, args.max_plausible_edge)

    if not args.show_implausible:
        hidden = sum(1 for p in pairs if p.implausible)
        pairs = [p for p in pairs if not p.implausible]
        RUN_STATS["hidden_implausible"] = hidden
        if hidden:
            print(f"hid {hidden} pair(s) with implausibly large edges "
                  f"(>{args.max_plausible_edge * 100:.0f}pp); --show-implausible to see them",
                  file=sys.stderr)

    if args.arb_only:
        pairs = [p for p in pairs if p.edge >= args.min_edge]
    else:
        pairs = [p for p in pairs
                 if abs(p.mid_gap) >= args.min_edge or p.edge >= args.min_edge]

    if args.sort == "annualised":
        pairs.sort(key=lambda p: (p.annualised if p.annualised is not None else -9e9),
                   reverse=True)
    elif args.sort == "gap":
        pairs.sort(key=lambda p: abs(p.mid_gap), reverse=True)
    elif args.sort == "confidence":
        pairs.sort(key=lambda p: p.confidence, reverse=True)
    else:
        pairs.sort(key=lambda p: p.edge, reverse=True)
    pairs = pairs[:args.top]
    RUN_STATS["kept"] = len(pairs)
    return pairs, reject


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def shorten(text: str, width: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= width else text[: width - 1] + "…"


def pct(value: float | None) -> str:
    return "  n/a" if value is None else f"{value * 100:5.1f}"


def fmt2(value: float | None) -> str:
    return "n/a " if value is None else f"{value:.2f}"


def render(pairs: list[Pair], args) -> str:
    if not pairs:
        return ("\nNo pairs cleared the thresholds. The attrition table above shows "
                "where markets were dropped;\nif both venues show usable markets, "
                "loosen with --min-confidence 0.35 --min-edge 0.005.\n")

    lines = ["", f"{len(pairs)} matched pair(s), best first",
             f"sized at {args.size:g} contracts per leg", "=" * 100]
    for i, p in enumerate(pairs, 1):
        flag = "  <-- CANDIDATE, VERIFY RULES" if p.edge > 0 else ""
        lines.append("")
        lines.append(f"[{i}] confidence {p.confidence:.2f}   "
                     f"mid gap {p.mid_gap * 100:+.1f}pp   "
                     f"edge {p.edge * 100:+.2f}pp{flag}")
        if p.annualised is not None and p.days_to_settle:
            lines.append(f"    {p.annualised * 100:+.1f}% annualised over "
                         f"{p.days_to_settle:.0f} days of locked capital")
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
        lines.append(f"    similarity: title {p.title_sim:.2f}  "
                     f"rules {fmt2(p.rules_sim if p.rules_known else None)}  "
                     f"dates {p.date_sim:.2f}"
                     + (f"  ({p.days_apart:.0f}d apart)" if p.days_apart is not None else ""))
        if p.edge > 0 and p.cost is not None:
            lines.append(f"    trade: {p.edge_leg} — pay ${p.cost:.4f} per contract "
                         f"for $1.00 at settlement (both venues' fees included)")
        elif p.edge_leg:
            lines.append(f"    {p.edge_leg}")
        if p.priced_at_top:
            lines.append("    ! priced from top of book only — size not verified "
                         "against real depth")
        for warning in p.warnings:
            lines.append(f"    ! {warning}")
        if args.verbose and p.kalshi.rules:
            lines.append(f"    kalshi rules: {shorten(p.kalshi.rules, 300)}")
            lines.append(f"    poly rules:   {shorten(p.poly.rules, 300)}")
    lines += [
        "",
        "Confidence is a text-similarity estimate. It is not evidence that the two",
        "contracts resolve on the same criteria, and a mismatch there turns a hedge",
        "into two directional bets. Read both rule sets in full before trading.",
        "",
    ]
    return "\n".join(lines)


def pair_to_dict(p: Pair) -> dict:
    def side(m: Market) -> dict:
        return {
            "venue": m.venue, "id": m.market_id, "question": m.question, "url": m.url,
            "category": m.category, "yes_bid": m.yes_bid, "yes_ask": m.yes_ask,
            "mid": m.mid, "volume": m.volume, "liquidity": m.liquidity,
            "yes_depth": m.book.depth("yes") if m.book else None,
            "no_depth": m.book.depth("no") if m.book else None,
            "settle_time": m.settle_time.isoformat() if m.settle_time else None,
            "rules": m.rules,
        }
    return {
        "confidence": round(p.confidence, 4),
        "title_similarity": round(p.title_sim, 4),
        # None, not 0.0, when a rule text is missing: the harness must be able
        # to tell "could not compare" from "compared and disagreed".
        "rules_similarity": round(p.rules_sim, 4) if p.rules_known else None,
        "date_similarity": round(p.date_sim, 4),
        "days_apart": p.days_apart,
        "mid_gap": round(p.mid_gap, 4),
        "edge": round(p.edge, 4),
        "edge_leg": p.edge_leg,
        "cost_per_contract": p.cost,
        "contracts": p.filled,
        "days_to_settle": p.days_to_settle,
        "annualised": p.annualised,
        "priced_at_top_of_book": p.priced_at_top,
        "warnings": p.warnings,
        "kalshi": side(p.kalshi),
        "polymarket": side(p.poly),
    }


def write_csv(path: str, pairs: list[Pair]) -> None:
    columns = ["confidence", "mid_gap", "edge", "annualised", "days_to_settle",
               "edge_leg", "cost_per_contract", "priced_at_top_of_book",
               "kalshi_mid", "poly_mid", "kalshi_question", "poly_question",
               "kalshi_id", "poly_url", "title_similarity", "rules_similarity",
               "days_apart", "warnings"]
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(columns)
        for p in pairs:
            writer.writerow([
                f"{p.confidence:.4f}", f"{p.mid_gap:.4f}", f"{p.edge:.4f}",
                "" if p.annualised is None else f"{p.annualised:.4f}",
                "" if p.days_to_settle is None else f"{p.days_to_settle:.1f}",
                p.edge_leg, "" if p.cost is None else f"{p.cost:.4f}",
                p.priced_at_top,
                p.mid_kalshi, p.mid_poly, p.kalshi.question, p.poly.question,
                p.kalshi.market_id, p.poly.url,
                f"{p.title_sim:.4f}",
                "" if not p.rules_known else f"{p.rules_sim:.4f}",
                "" if p.days_apart is None else f"{p.days_apart:.1f}",
                "; ".join(p.warnings),
            ])


# The number of history snapshots kept on disk. One scheduled run a day, so
# this is roughly six months; older files are pruned to stop the repo growing
# without bound.
HISTORY_LIMIT = 180


def run_meta(pairs: list[Pair], reject: Counter, args, error: str = "") -> dict:
    """The machine-readable twin of what the run prints to stderr.

    The stderr log is prose meant for a person watching a terminal. Parsing it
    back out with regexes is how a dashboard silently starts lying the first
    time a message is reworded, so the counters are published as data instead.
    """
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "scanner_version": "2.0",
        "thresholds": {
            "min_confidence": args.min_confidence,
            "min_rules_sim": args.min_rules_sim,
            "max_plausible_edge": args.max_plausible_edge,
            "min_edge": args.min_edge,
            "size": args.size,
            "depth": args.depth,
            "top": args.top,
            "sort": args.sort,
            "max_days_out": args.max_days_out,
            "min_volume_kalshi": args.min_volume_kalshi,
            "min_volume_poly": args.min_volume_poly,
        },
        "weights": {
            "title": args.weight_title,
            "rules": args.weight_rules,
            "date": args.weight_date,
        },
        "fees": {
            "kalshi": args.kalshi_fee_coeff if args.kalshi_fee_coeff is not None
                      else KALSHI_FEE_DEFAULT,
            "polymarket_default": args.poly_fee_coeff if args.poly_fee_coeff is not None
                                  else POLY_FEE_DEFAULT,
            "polymarket_by_category": POLY_FEE_BY_CATEGORY,
        },
        "funnel": {k: v for k, v in RUN_STATS.items() if k != "started_at"},
        "dropped": dict(reject.most_common()),
        "kept": len(pairs),
        "errors": [error] if error else [],
    }


def write_meta(path: str, meta: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)


def slim_record(record: dict) -> dict:
    """A pair record with the rule texts dropped.

    Rules are most of the bytes in a scan (84 KB of which ~70 KB is rule prose)
    and a trend chart never reads them. Keeping them out of every daily
    snapshot is the difference between a repo that grows a few KB a day and one
    that grows a megabyte a week.
    """
    slim = dict(record)
    for venue in ("kalshi", "polymarket"):
        side = dict(slim.get(venue) or {})
        side.pop("rules", None)
        slim[venue] = side
    return slim


def history_summary(filename: str, generated_at: str, records: list[dict]) -> dict:
    edges = [r["edge"] for r in records]
    annualised = [r["annualised"] for r in records if r.get("annualised") is not None]
    confidences = sorted(r["confidence"] for r in records)
    mid = (confidences[len(confidences) // 2] if confidences else None)
    return {
        "file": filename,
        "generated_at": generated_at,
        "kept": len(records),
        "best_edge": max(edges) if edges else None,
        "best_annualised": max(annualised) if annualised else None,
        "median_confidence": mid,
        "profit_at_size": round(sum(r["edge"] * (r.get("contracts") or 0)
                                    for r in records), 2),
    }


def write_history(directory: str, records: list[dict], meta: dict,
                  limit: int = HISTORY_LIMIT) -> str:
    """Append this run to the dated snapshot set and prune the oldest.

    Written per run rather than overwriting a `latest`, because a trend needs
    points. The index carries the summary numbers so the dashboard reads one
    small file instead of every snapshot it has ever kept.
    """
    os.makedirs(directory, exist_ok=True)
    stamp = meta["generated_at"].replace(":", "-")
    filename = f"{stamp}.json"
    with open(os.path.join(directory, filename), "w", encoding="utf-8") as fh:
        json.dump([slim_record(r) for r in records], fh, indent=2)

    index_path = os.path.join(directory, "index.json")
    index: list[dict] = []
    try:
        with open(index_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, list):
            index = loaded
    except (OSError, json.JSONDecodeError):
        index = []

    index = [row for row in index if row.get("file") != filename]
    index.append(history_summary(filename, meta["generated_at"], records))
    index.sort(key=lambda row: row.get("generated_at") or "")

    for stale in index[:-limit]:
        try:
            os.remove(os.path.join(directory, stale["file"]))
        except OSError:
            pass
    index = index[-limit:]

    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=2)
    return filename


# ---------------------------------------------------------------------------
# Labelling and evaluation
# ---------------------------------------------------------------------------

HERE = os.path.dirname(os.path.abspath(__file__))
LABELS_PATH = os.path.join(HERE, "labels.json")
LAST_RUN_PATH = os.path.join(HERE, "last_run.json")


# Labelled examples from the first clean live run, so the scoring commands work
# before you have labelled anything yourself. Written out to labels.json on
# first use; your own labels are appended to the same file.
SEED_LABELS = [
    # (kalshi_id, poly_slug, kalshi_q, poly_q, conf, rules, edge, label, why)
    ("KXFL19R-26-OHAW", "fl-19-r", "Will Ola Hawatmeh be the Republican nominee for FL-19?", "Will the Republican Party win the FL-19 House seat?", 0.51, 0.24, 0.8489, "mismatch", "primary nominee vs general election"),
    ("KXMA6D-26-TNGU", "ma-06", "Will Tram Nguyen be the Democratic nominee for MA-6?", "Will the Democratic Party win the MA-06 House seat?", 0.46, 0.23, 0.7312, "mismatch", "primary nominee vs general election"),
    ("KXXITAIWAN-27JAN01", "xi-us", "Will Xi Jinping visit Taiwan before Jan 1, 2027?", "Will Xi Jinping visit US before 2027?", 0.50, 0.24, 0.8741, "mismatch", "entity substitution: Taiwan vs US"),
    ("KXLEADERMLBRUNS-26-JWOO", "wood-doubles", "Will James Wood lead Pro Baseball in runs for the 2026 regular season?", "Will James Wood lead the MLB in doubles for the 2026 regular season?", 0.54, 0.07, 0.6242, "mismatch", "metric substitution: runs vs doubles"),
    ("KXABRAHAMSA-27-JAN01-COLO", "israel-lebanon", "Will Israel and Colombia normalize relations before Jan 1, 2027?", "Israel and Lebanon normalize relations before 2027?", 0.53, 0.27, 0.7994, "mismatch", "entity substitution: Colombia vs Lebanon"),
    ("KXMACCELL-27", "foldable", "Will Apple release a Mac with cellular connectivity before 2027?", "Will Apple release a foldable iPhone before 2027?", 0.46, 0.13, 0.7261, "mismatch", "product substitution"),
    ("KXTOPARTISTRUNNERUP-26-DRA", "drake-top", "Will Drake be the #2 most streamed Artist on the 2026 Spotify Wrapped", "Will Drake be the top artist for 2026?", 0.49, 0.66, 0.3973, "mismatch", "ordinal substitution: #2 vs top"),
    ("KXLEADERMLBWINS-26-CSAN", "sanchez-era", "Will Cristopher Sanchez lead Pro Baseball in wins for the 2026 regular season?", "Will Cristopher Sanchez lead the MLB in ERA for the 2026 regular season?", 0.53, 0.05, 0.2169, "mismatch", "metric substitution: wins vs ERA"),
    ("KXAAAGASMAXTX-26DEC31-4.20", "xrp-420", "Will average gas prices be above or below $4.20 by Dec 31, 2026?", "Will XRP reach $4.20 by December 31, 2026?", 0.53, 0.06, 0.3408, "mismatch", "numeric coincidence: 4.20"),
    ("KXCONGRESSTESTIFY-27JAN-JPOW", "powell-jail", "Will Jerome Powell testify in front of Congress before Jan 2027?", "Jerome Powell in jail before 2027?", 0.49, 0.10, 0.2579, "mismatch", "unrelated event, same person"),
    ("KXMLSCUP-26-MIA", "dc-united", "Will Miami win the MLS Cup?", "Will D.C. United win the 2026 MLS Cup?", 0.45, 0.45, 0.2585, "mismatch", "entity substitution: different team"),
    ("KXMLSCUP-26-LAG", "lafc", "Will Los Angeles G win the MLS Cup?", "Will Los Angeles FC win the 2026 MLS Cup?", 0.55, 0.44, 0.0645, "mismatch", "Galaxy vs LAFC, title similarity 0.85"),
    ("KXARREST-27JAN-LJAM", "obama-arrested", "Will Letitia James be arrested before Jan 2027?", "Obama arrested before 2027?", 0.52, 0.41, 0.0990, "mismatch", "ladder member vs standalone"),
    ("KXSTARSHIPSPACE-26-5.0", "starship-lt5", "Will exactly 5 Starship launches reach space in 2026?", "Will less than 5 SpaceX Starship launches successfully reach Space in 2026?", 0.65, 0.35, 0.0910, "mismatch", "exactly 5 vs fewer than 5"),
    ("KXLARGECUT-26", "one-fed-cut", "Will the Fed cut rates more than 25 bps in 2026?", "Will 1 Fed rate cut happen in 2026?", 0.53, 0.40, 0.0261, "mismatch", "more than 25bps vs exactly one cut"),
    ("KXMODELHIGH-27-1550-CLAU", "openai-1550", "Will Claude be the first to hit 1550 on Text Arena?", "Will OpenAI be the first company to have an AI model hit 1550 on Chatbot Arena in 2026?", 0.45, 0.39, 0.0973, "mismatch", "entity substitution: different lab"),
    ("KXF1-26-MV", "verstappen-retire", "Will Max Verstappen win the F1 Drivers Championship?", "Will Max Verstappen retire from F1 in 2026?", 0.53, 0.48, 0.0799, "mismatch", "unrelated event, same person"),
    ("KXUSACOMPANYSTAKE-27JAN01-TSM", "openai-stake", "Will any part of the United States federal government take a stake of above 0% in TSMC?", "Will the US federal government take a stake in OpenAI?", 0.47, 0.41, 0.0469, "mismatch", "entity substitution: TSMC vs OpenAI"),
    ("KXVENEZUELALEADER-26DEC31-DRON", "cabello", "Will Diosdado Cabello Rondon be the head of state of Venezuela on Dec 31, 2026?", "Will Diosdado Cabello Rondon be the leader of Venezuela end of 2026?", 0.61, 0.44, 0.0321, "match", "same person, same date, head of state vs leader"),
    ("KXVENEZUELALEADER-26DEC31-EGON", "edmundo", "Will Edmundo Gonzalez Urrutia be the head of state of Venezuela on Dec 31, 2026?", "Will Edmundo Gonzalez be the leader of Venezuela end of 2026?", 0.56, 0.43, 0.0254, "match", "same person, same date"),
    ("KXUSACOMPANYSTAKE-27JAN01-ANTH", "anthropic-stake", "Will any part of the United States federal government take a stake of above 0% in Anthropic?", "Will the US federal government take a stake in Anthropic PBC?", 0.50, 0.42, 0.0435, "match", "same entity, same action"),
    ("KXTRUMPPARDON-27JAN01-SBAN", "bannon-pardon", "Will Steve Bannon receive a presidential pardon before Jan 1, 2027?", "Will Trump pardon Steve Bannon before 2027?", 0.56, 0.35, 0.0191, "match", "same person, same action, same date"),
    ("KXTRUMPADMINLEAVE-26DEC31-HLUT", "lutnick-out", "Will Howard Lutnick leaves Secretary of Commerce in before 2027?", "Howard Lutnick out as Secretary of Commerce by December 31?", 0.47, 0.42, 0.0239, "match", "same person, same role, same date"),
    ("KXREDISTRICTING-26-VIR", "virginia-map", "What states will redistrict before the 2026 Congressional elections? Virginia", "Will Virginia use a new congressional map for the 2026 United States midterm elections?", 0.63, 0.65, 0.0389, "match", "redistricting vs new map, same state and cycle"),
]


def seed_rows() -> list[dict]:
    return [{"kalshi_id": kid, "poly_url": f"https://polymarket.com/market/{slug}",
             "kalshi_question": kq, "poly_question": pq, "confidence": conf,
             "rules_sim": rules, "edge": edge, "label": label, "note": why}
            for kid, slug, kq, pq, conf, rules, edge, label, why in SEED_LABELS]


def load_labels() -> list[dict]:
    if not os.path.exists(LABELS_PATH):
        rows = seed_rows()
        save_labels(rows)
        print(f"created {LABELS_PATH} with {len(rows)} seed labels", file=sys.stderr)
        return rows
    try:
        with open(LABELS_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return []


def save_labels(rows: list[dict]) -> None:
    with open(LABELS_PATH, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)


def label_key(row: dict) -> str:
    return f"{row.get('kalshi_id', '')}|{row.get('poly_url', '')}"


def review(args) -> int:
    """Step through the last scan and label each pair by hand.

    Labelling is the only way to know whether a threshold change helped. This
    keeps it to one keystroke per pair so it actually gets done.
    """
    if not os.path.exists(LAST_RUN_PATH):
        print("no scan to review yet. Run a scan first (the default command).",
              file=sys.stderr)
        return 1
    with open(LAST_RUN_PATH, encoding="utf-8") as fh:
        run = json.load(fh)

    rows = load_labels()
    known = {label_key(r) for r in rows}
    todo = [pair for pair in run
            if f"{pair['kalshi']['id']}|{pair['polymarket']['url']}" not in known]

    if not todo:
        print(f"every pair in the last scan is already labelled "
              f"({len(rows)} total). Run the score command.")
        return 0

    print(f"{len(todo)} unlabelled pair(s) from the last scan.")
    print("For each: does a YES on one venue pay out under exactly the same")
    print("circumstances as a YES on the other?")
    print("  m = match     x = mismatch     s = skip     q = save and quit\n")

    for i, pair in enumerate(todo, 1):
        k, p = pair["kalshi"], pair["polymarket"]
        print("=" * 72)
        print(f"[{i}/{len(todo)}]  confidence {pair['confidence']:.2f}   "
              f"rules {fmt2(pair['rules_similarity'])}   "
              f"edge {pair['edge'] * 100:+.2f}pp")
        print(f"  KALSHI      {k['question']}")
        print(f"              {k['url']}")
        print(f"  POLYMARKET  {p['question']}")
        print(f"              {p['url']}")
        for warning in pair.get("warnings", []):
            print(f"  ! {warning}")

        try:
            answer = input("  m / x / s / q > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if answer == "q":
            break
        if answer not in ("m", "x"):
            continue

        rows.append({
            "kalshi_id": k["id"],
            "poly_url": p["url"],
            "kalshi_question": k["question"],
            "poly_question": p["question"],
            "confidence": pair["confidence"],
            "rules_sim": pair["rules_similarity"],
            "edge": pair["edge"],
            "label": "match" if answer == "m" else "mismatch",
            "note": "",
        })
        save_labels(rows)

    save_labels(rows)
    matches = sum(1 for r in rows if r["label"] == "match")
    print(f"\nsaved {len(rows)} labelled pair(s): {matches} match, "
          f"{len(rows) - matches} mismatch")
    print("run the score command to see how the current thresholds do.")
    return 0


def score(args) -> int:
    rows = [r for r in load_labels() if r.get("label") in ("match", "mismatch")]
    if not rows:
        print("nothing labelled yet. Run a scan, then the review command.",
              file=sys.stderr)
        return 1

    tp = fp = tn = fn = 0
    misses, leaks = [], []
    for row in rows:
        kept, reason = passes_gates(
            row["confidence"], row.get("rules_sim"), row["edge"],
            args.min_confidence, args.min_rules_sim, args.max_plausible_edge)
        truth = row["label"] == "match"
        if kept and truth:
            tp += 1
        elif kept:
            fp += 1
            leaks.append(row)
        elif truth:
            fn += 1
            misses.append(dict(row, reason=reason))
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    print(f"labelled pairs: {len(rows)}  ({tp + fn} match, {tn + fp} mismatch)")
    print(f"thresholds: confidence>={args.min_confidence} "
          f"rules>={args.min_rules_sim} edge<={args.max_plausible_edge}")
    print(f"  kept:    {tp:>4} true  {fp:>4} false   precision {precision:.2f}")
    print(f"  dropped: {tn:>4} true  {fn:>4} false   recall    {recall:.2f}")

    # False negatives are the expensive error and the easy one to hide from
    # yourself, so they print in full whether or not you asked for detail.
    if misses:
        print(f"\nreal matches these thresholds would discard ({len(misses)}):")
        for row in misses:
            print(f"  [{row['reason']}] conf {row['confidence']:.2f} "
                  f"rules {fmt2(row.get('rules_sim'))} edge {row['edge'] * 100:+.1f}pp")
            print(f"      K: {row['kalshi_question'][:74]}")
            print(f"      P: {row['poly_question'][:74]}")
    if leaks and args.verbose:
        print(f"\nmismatches still getting through ({len(leaks)}):")
        for row in leaks:
            print(f"  conf {row['confidence']:.2f} rules {fmt2(row.get('rules_sim'))} "
                  f"edge {row['edge'] * 100:+.1f}pp")
            print(f"      K: {row['kalshi_question'][:74]}")
            print(f"      P: {row['poly_question'][:74]}")
    return 0


def _pr(rows, conf, rules, edge) -> tuple[int, int, int, float, float]:
    tp = fp = fn = 0
    for row in rows:
        kept, _ = passes_gates(row["confidence"], row.get("rules_sim"), row["edge"],
                               conf, rules, edge)
        truth = row["label"] == "match"
        tp += kept and truth
        fp += kept and not truth
        fn += (not kept) and truth
    precision = tp / (tp + fp) if (tp + fp) else float("nan")
    recall = tp / (tp + fn) if (tp + fn) else float("nan")
    return tp, fp, fn, precision, recall


def sweep(args) -> int:
    """Vary each threshold on its own, then search for the best combination.

    A one-axis sweep answers the wrong question once recall is already 1.00:
    at that point nothing is being discarded, so the useful question is not
    "what does this cost" but "how far can I tighten before it costs anything".
    Sweeping each axis separately shows where the headroom is; the grid search
    finds the corner that spends the least of it.
    """
    rows = [r for r in load_labels() if r.get("label") in ("match", "mismatch")]
    if not rows:
        print("nothing labelled yet. Run a scan, then the review command.",
              file=sys.stderr)
        return 1

    matches = sum(1 for r in rows if r["label"] == "match")
    print(f"{len(rows)} labelled pairs ({matches} match, {len(rows) - matches} mismatch)")
    base = (args.min_confidence, args.min_rules_sim, args.max_plausible_edge)
    print(f"current: confidence>={base[0]} rules>={base[1]} edge<={base[2]}\n")

    axes = (
        ("confidence floor", 0, (0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70)),
        ("rules floor",      1, (0.00, 0.20, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)),
        ("edge cap",         2, (0.01, 0.02, 0.03, 0.05, 0.10, 0.20, 0.50, 1.00)),
    )
    for name, index, values in axes:
        print(f"{name} (others held at current):")
        print(f"{'value':>8} {'kept':>6} {'prec':>6} {'recall':>7}  {'misses':>7}")
        for value in values:
            settings = list(base)
            settings[index] = value
            tp, fp, fn, precision, recall = _pr(rows, *settings)
            marker = "  <-- current" if abs(value - base[index]) < 1e-9 else ""
            print(f"{value:>8.2f} {tp + fp:>6} {precision:>6.2f} {recall:>7.2f}  "
                  f"{fn:>7}{marker}")
        print()

    # Grid search. Recall is what you are spending; precision is what you buy.
    print(f"best settings holding recall at or above {args.target_recall:.2f}:")
    best = None
    for conf in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        for rules in (0.0, 0.20, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60):
            for edge in (0.01, 0.02, 0.03, 0.05, 0.075, 0.10, 0.15, 0.20, 0.50):
                tp, fp, fn, precision, recall = _pr(rows, conf, rules, edge)
                if recall < args.target_recall or tp == 0:
                    continue
                score_tuple = (round(precision, 4), round(recall, 4), tp)
                if best is None or score_tuple > best[0]:
                    best = (score_tuple, conf, rules, edge, tp, fp, fn)

    if best is None:
        print("  no combination reaches that recall. Lower --target-recall.")
        return 0

    (precision, recall, _), conf, rules, edge, tp, fp, fn = best
    print(f"  confidence>={conf}  rules>={rules}  edge<={edge}")
    print(f"  keeps {tp + fp} pairs: {tp} true, {fp} false   "
          f"precision {precision:.2f}  recall {recall:.2f}")
    _, _, _, now_p, now_r = _pr(rows, *base)
    print(f"  (currently precision {now_p:.2f}, recall {now_r:.2f})")
    print()
    print("  Put these in CONFIG, then re-run review on a fresh scan. Tuning")
    print("  thresholds on the same pairs you tuned them with will flatter them;")
    print("  the number that counts is the one from labels you have not seen.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find Kalshi and Polymarket markets that ask the same "
                    "question at different odds.",
        epilog="Run with no arguments to scan using the CONFIG block at the "
               "top of this file.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("command", nargs="?", default=CONFIG["command"],
                        choices=["scan", "review", "score", "sweep"],
                        help="scan for pairs, review them by hand, score the "
                             "thresholds, or sweep the edge cap")

    g = parser.add_argument_group("strictness")
    g.add_argument("--min-edge", type=float, default=CONFIG["min_edge"])
    g.add_argument("--min-confidence", type=float, default=CONFIG["min_confidence"])
    g.add_argument("--min-rules-sim", type=float, default=CONFIG["min_rules_sim"])
    g.add_argument("--max-plausible-edge", type=float, default=CONFIG["max_plausible_edge"],
                   help="edges above this are treated as mismatch evidence")
    g.add_argument("--show-implausible", action="store_true")
    g.add_argument("--no-ladder-filter", action="store_true",
                   help="allow one-of-N ladder members to match standalone markets")
    g.add_argument("--arb-only", action="store_true")

    g = parser.add_argument_group("output")
    g.add_argument("--top", type=int, default=CONFIG["top"])
    g.add_argument("--sort", choices=["edge", "annualised", "gap", "confidence"],
                   default="annualised")
    g.add_argument("--json", metavar="PATH")
    g.add_argument("--csv", metavar="PATH")
    g.add_argument("--meta", metavar="PATH",
                   help="write the run's thresholds and funnel counts as JSON")
    g.add_argument("--history-dir", metavar="DIR",
                   help="append a dated snapshot of this run for trend charts")
    g.add_argument("--verbose", action="store_true")
    g.add_argument("--target-recall", type=float, default=1.0,
                   help="sweep: lowest recall the grid search may settle for")

    g = parser.add_argument_group("trade sizing")
    g.add_argument("--size", type=float, default=CONFIG["size"])
    g.add_argument("--depth", type=int, default=CONFIG["depth"])

    g = parser.add_argument_group("fetching")
    g.add_argument("--min-volume-kalshi", type=float, default=CONFIG["min_volume_kalshi"])
    g.add_argument("--min-volume-poly", type=float, default=CONFIG["min_volume_poly"])
    g.add_argument("--max-markets", type=int, default=4000)
    g.add_argument("--kalshi-scan-limit", type=int, default=30000)
    g.add_argument("--kalshi-series", action="append", metavar="TICKER")
    g.add_argument("--max-days-out", type=int, default=CONFIG["max_days_out"])
    g.add_argument("--max-date-diff", type=float, default=45)
    g.add_argument("--max-candidates", type=int, default=40)
    g.add_argument("--common-token-frac", type=float, default=0.25)
    g.add_argument("--cache-dir", default=os.path.join(HERE, "cache"))
    g.add_argument("--cache-ttl", type=int, default=CONFIG["cache_ttl"])
    g.add_argument("--offline", metavar="DIR")

    g = parser.add_argument_group("scoring weights")
    g.add_argument("--weight-title", type=float, default=0.55)
    g.add_argument("--weight-rules", type=float, default=0.28)
    g.add_argument("--weight-date", type=float, default=0.17)
    g.add_argument("--kalshi-fee-coeff", type=float, default=None)
    g.add_argument("--poly-fee-coeff", type=float, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "review":
        return review(args)
    if args.command == "score":
        return score(args)
    if args.command == "sweep":
        return sweep(args)

    try:
        pairs, reject = scan(args)
    except FetchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print("hint: if you are behind a filtered network, set HTTPS_PROXY or "
              "point --offline at cached JSON.", file=sys.stderr)
        # Still publish the metadata. A dashboard that shows "the last run
        # failed, here is why" is worth more than one that silently keeps
        # displaying yesterday's numbers as if they were current.
        if args.meta:
            write_meta(args.meta, run_meta([], Counter(), args, error=str(exc)))
        return 2

    sys.stdout.write(render(pairs, args))

    # Always save the run so `review` has something to work with, whether or
    # not --json was passed. One less step to remember.
    records = [pair_to_dict(p) for p in pairs]
    try:
        with open(LAST_RUN_PATH, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
    except OSError:
        pass

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(records, fh, indent=2)
        print(f"wrote {args.json}", file=sys.stderr)
    if args.csv:
        write_csv(args.csv, pairs)
        print(f"wrote {args.csv}", file=sys.stderr)

    meta = run_meta(pairs, reject, args)
    if args.meta:
        write_meta(args.meta, meta)
        print(f"wrote {args.meta}", file=sys.stderr)
    if args.history_dir:
        snapshot = write_history(args.history_dir, records, meta)
        print(f"wrote {os.path.join(args.history_dir, snapshot)}", file=sys.stderr)

    if pairs:
        unlabelled = len({label_key(r) for r in load_labels()})
        print(f"\n{len(pairs)} pair(s) saved. To label them, set "
              f'CONFIG["command"] = "review" and press Run.'
              + (f" ({unlabelled} already labelled)" if unlabelled else ""),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
