"""Synthetic traffic — plausible Loom events with no keys and no spend.

Writes straight to the sink rather than going through the logging handler.
There's no call to instrument, and the handler path is already exercised by
real traffic; going around it keeps the fake events out of anyone's log stream.

Every event is tagged `{"source": "demo"}` so synthetic rows stay separable
from real ones in the database. A demo that can't tell you which numbers were
fake isn't worth much.

CLI:
    python traffic.py seed --count 500 --spread 3600
    python traffic.py show
    python traffic.py run --rate 2
"""

from __future__ import annotations

import argparse
import json
import math
import random
import threading
import time
from collections import defaultdict, deque
from typing import Any

from loom.observability.sink import EventSink, SQLiteSink

DEMO_TAGS = {"source": "demo"}

# provider, model, USD per 1M input tokens, USD per 1M output tokens, median latency ms.
# Prices and latencies are representative, not quoted — this is demo data.
_MODELS = [
    ("openai",    "gpt-4o-mini",        0.15, 0.60,  380, 30),
    ("openai",    "gpt-4o",             2.50, 10.00, 620, 12),
    ("anthropic", "claude-3-5-haiku",   0.80, 4.00,  290, 20),
    ("anthropic", "claude-sonnet-4",    3.00, 15.00, 700, 10),
    ("gemini",    "gemini-2.0-flash",   0.10, 0.40,  240, 15),
    ("xai",       "grok-2",             2.00, 10.00, 810, 6),
    ("deepseek",  "deepseek-chat",      0.27, 1.10,  520, 5),
    ("mistral",   "mistral-large",      2.00, 6.00,  460, 4),
]
_WEIGHTS = [m[5] for m in _MODELS]

_ERRORS = [
    ("RateLimitError", "429 rate limit exceeded"),
    ("TimeoutError", "request timed out after 30s"),
    ("ProviderError", "502 upstream error"),
    ("APIConnectionError", "connection reset by peer"),
]

# Mix, per the design: cache hits dominate a warm system.
P_CACHED = 0.60
P_DEDUPED = 0.05
P_RETRY = 0.06

# Failure rates differ per provider on purpose. A uniform rate makes every
# provider warn at once, which turns the health panel into noise — the panel
# exists to make a bad provider stand out, so the demo data gives it one.
P_ERROR_DEFAULT = 0.004
P_ERROR = {"xai": 0.06, "mistral": 0.025}


class _Generator:
    """Builds events. Holds a small replay buffer of real results per model so
    cache hits can return a *prior* result — which is what Loom does: the cache
    stores the enriched dict, so a hit logs the original call's cost verbatim.
    """

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self._recent: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=24)
        )

    def _live_call(self, spec: tuple, ts: float) -> dict[str, Any]:
        provider, model, in_price, out_price, median_ms, _ = spec
        input_tokens = int(self.rng.lognormvariate(6.2, 0.7))
        output_tokens = int(self.rng.lognormvariate(5.4, 0.8))
        cost = (
            input_tokens * in_price + output_tokens * out_price
        ) / 1_000_000

        event = {
            "ts": ts,
            "provider": provider,
            "modality": "text",
            "model": model,
            "upstream_model": model,
            "latency_ms": round(self.rng.lognormvariate(_mu(median_ms), 0.45), 2),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "cost_usd": round(cost, 8),
            "cost_local": None,
            "cost_currency": None,
            "ok": True,
            "cached": False,
            "deduped": False,
            "retries": 1 if self.rng.random() < P_RETRY else 0,
            "tags": DEMO_TAGS,
        }
        self._recent[model].append(event)
        return event

    def _replay(self, prior: dict[str, Any], ts: float, *, kind: str) -> dict[str, Any]:
        """A cache hit or dedup wait: same payload, near-zero latency."""
        event = dict(prior)
        event.update({
            "ts": ts,
            "cached": kind == "cached",
            "deduped": kind == "deduped",
            "retries": 0,
            # A cache hit is a dict lookup; a dedup wait rides the in-flight call.
            "latency_ms": round(
                self.rng.uniform(0.05, 1.5) if kind == "cached"
                else self.rng.uniform(40.0, 300.0),
                2,
            ),
        })
        return event

    def _failure(self, spec: tuple, ts: float) -> dict[str, Any]:
        provider, model, _, _, median_ms, _ = spec
        error_type, error = self.rng.choice(_ERRORS)
        return {
            "ts": ts,
            "provider": provider,
            "modality": "text",
            "model": model,
            "upstream_model": model,
            "latency_ms": round(self.rng.lognormvariate(_mu(median_ms * 2), 0.6), 2),
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cost_usd": None,
            "cost_local": None,
            "cost_currency": None,
            "ok": False,
            "cached": False,
            "deduped": False,
            "retries": self.rng.randint(1, 3),
            "tags": DEMO_TAGS,
            "error_type": error_type,
            "error": error,
        }

    def event(self, ts: float | None = None) -> dict[str, Any]:
        ts = time.time() if ts is None else ts
        spec = self.rng.choices(_MODELS, weights=_WEIGHTS, k=1)[0]
        provider, model = spec[0], spec[1]

        if self.rng.random() < P_ERROR.get(provider, P_ERROR_DEFAULT):
            return self._failure(spec, ts)

        prior_pool = self._recent[model]
        if prior_pool:
            roll = self.rng.random()
            if roll < P_CACHED:
                return self._replay(self.rng.choice(prior_pool), ts, kind="cached")
            if roll < P_CACHED + P_DEDUPED:
                return self._replay(self.rng.choice(prior_pool), ts, kind="deduped")

        return self._live_call(spec, ts)


def _mu(median_ms: float) -> float:
    """Log-normal mu that puts the median at `median_ms`."""
    return math.log(max(median_ms, 1.0))


def seed(
    sink: EventSink,
    count: int,
    *,
    spread_seconds: float = 3600.0,
    seed_value: int | None = None,
) -> int:
    """Backfill `count` events spread over the trailing `spread_seconds`.

    Timestamps are sorted ascending before writing so row id order matches time
    order, which is what the incremental log tail assumes.
    """
    rng = random.Random(seed_value)
    gen = _Generator(rng)
    now = time.time()
    stamps = sorted(now - rng.uniform(0, spread_seconds) for _ in range(count))
    for ts in stamps:
        sink.write(gen.event(ts))
    return count


class DemoTraffic:
    """Background generator. Start/stop from the API or DEMO_MODE."""

    def __init__(self, sink: EventSink, *, rate_per_sec: float = 2.0) -> None:
        self.sink = sink
        self.rate_per_sec = max(0.05, float(rate_per_sec))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._gen = _Generator()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="demo-traffic", daemon=True
        )
        self._thread.start()
        return True

    def stop(self) -> bool:
        if not self.running:
            return False
        self._stop.set()
        self._thread.join(timeout=5.0)
        self._thread = None
        return True

    def _run(self) -> None:
        rng = self._gen.rng
        while not self._stop.is_set():
            try:
                self.sink.write(self._gen.event())
            except Exception:
                # A demo generator must never take the app down. Back off and
                # retry rather than killing the thread on a transient DB lock.
                self._stop.wait(1.0)
                continue
            # Exponential gaps: arrivals look like traffic, not a metronome.
            self._stop.wait(rng.expovariate(self.rate_per_sec))


# --------------------------------------------------------------------------
# CLI — verify the aggregations before any web layer exists.

def _show(sink: EventSink, window_seconds: int | None) -> dict[str, Any]:
    import metrics
    from loom.observability import queries

    return {
        "summary": queries.summary(sink, window_seconds=window_seconds),
        "requests_per_min": metrics.requests_per_min(sink),
        "p95_latency_ms": metrics.p95_latency(sink, window_seconds=window_seconds),
        "cost_saved": metrics.cost_saved_estimate(sink, window_seconds=window_seconds),
        "providers": metrics.provider_health(sink, window_seconds=window_seconds),
        "tokens": metrics.tokens_by_provider(sink, window_seconds=window_seconds),
        "timeseries": metrics.timeseries(
            sink, window_seconds=window_seconds, buckets=10
        ),
        "tail": metrics.tail(sink, limit=5),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("seed", "show", "run"))
    parser.add_argument("--db", default="loom_events.db")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--spread", type=float, default=3600.0)
    parser.add_argument("--rate", type=float, default=2.0)
    parser.add_argument("--window", type=int, default=None,
                        help="window in seconds; omit for all time")
    args = parser.parse_args()

    sink = SQLiteSink(args.db)

    if args.command == "seed":
        n = seed(sink, args.count, spread_seconds=args.spread)
        print(f"seeded {n} events into {args.db} over {args.spread:.0f}s")
    elif args.command == "show":
        print(json.dumps(_show(sink, args.window), indent=2, default=str))
    else:
        demo = DemoTraffic(sink, rate_per_sec=args.rate)
        demo.start()
        print(f"generating ~{args.rate}/s into {args.db} — ctrl-c to stop")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            demo.stop()
            print("\nstopped")


if __name__ == "__main__":
    main()
