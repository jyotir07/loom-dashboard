"""Aggregations the dashboard needs beyond `loom.observability.queries`.

Everything here reads through the public `EventSink.fetch(sql=..., params=...)`
protocol. Nothing reaches into `sink._conn`, so any sink that speaks SQL works
unchanged.

Windows follow `queries.WINDOWS`: an int of seconds, or None for all time.
Every function returns zeros / empty lists on an empty database rather than
raising — a dashboard that 500s before its first request is useless.
"""

from __future__ import annotations

import json
import math
import time
from typing import Any

from loom.observability.sink import EventSink

# total_tokens is nullable and some providers only report the two halves.
_TOKENS = "COALESCE(total_tokens, COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0))"

# A provider is healthy at or above this success rate. Not 100%: one failed call
# in a busy window is noise, and a panel that sits permanently at "warn" stops
# carrying information.
OK_PCT_HEALTHY = 99.0

# ...and within this multiple of the median provider's latency. Relative rather
# than absolute because "slow" depends on the model mix, not on a fixed ms count.
LATENCY_WARN_RATIO = 2.5


def _window(window_seconds: int | None) -> tuple[str, tuple[Any, ...]]:
    """WHERE fragment + params for a time filter.

    Callers reuse the returned params rather than recomputing the cutoff, so
    multi-query functions all filter against the same instant.
    """
    if window_seconds is None:
        return "1=1", ()
    return "ts >= ?", (time.time() - window_seconds,)


def requests_per_min(sink: EventSink) -> float:
    """Requests in the trailing 60 seconds."""
    rows = sink.fetch(
        sql="SELECT COUNT(*) AS n FROM loom_events WHERE ts >= ?",
        params=(time.time() - 60.0,),
    )
    return float(rows[0]["n"] or 0) if rows else 0.0


def p95_latency(sink: EventSink, *, window_seconds: int | None = None) -> float:
    """95th percentile latency by nearest-rank.

    `rows[int(n * 0.95) - 1]` (the design doc's formula) misindexes at small n —
    at n=2 it returns the lower of the two samples. Nearest-rank is
    `ceil(0.95 * n)`, clamped to the last index.

    Ranked with LIMIT/OFFSET rather than pulling every row into Python, so a
    30-day window doesn't materialize the whole table.
    """
    where, params = _window(window_seconds)
    rows = sink.fetch(
        sql=f"SELECT COUNT(*) AS n FROM loom_events WHERE {where}", params=params
    )
    n = int(rows[0]["n"] or 0) if rows else 0
    if not n:
        return 0.0
    rank = min(n - 1, math.ceil(0.95 * n) - 1)
    rows = sink.fetch(
        sql=f"""
            SELECT latency_ms FROM loom_events
            WHERE {where}
            ORDER BY latency_ms
            LIMIT 1 OFFSET ?
        """,
        params=(*params, rank),
    )
    return round(float(rows[0]["latency_ms"] or 0.0), 2) if rows else 0.0


def timeseries(
    sink: EventSink, *, window_seconds: int | None = None, buckets: int = 60
) -> dict[str, Any]:
    """Request counts per time bucket, zero-filled.

    Bucket width is derived from the window rather than fixed at 60s: a 30-day
    window at one-minute granularity is 43,200 points, which is neither
    renderable nor meaningful. Minimum width stays 60s.

    Missing buckets are filled with 0 in Python. Without that an idle minute
    renders as a skipped point and the chart implies continuous traffic that
    never happened.
    """
    buckets = max(1, int(buckets))
    now = time.time()

    span = window_seconds
    if span is None:
        rows = sink.fetch(sql="SELECT MIN(ts) AS first_ts FROM loom_events")
        first_ts = rows[0]["first_ts"] if rows else None
        span = max(now - float(first_ts), 60.0 * buckets) if first_ts else 60.0 * buckets

    width = max(60, int(math.ceil(span / buckets)))
    last = int(now // width)
    first = last - buckets + 1

    rows = sink.fetch(
        sql=f"""
            SELECT CAST(ts / {width} AS INT) AS bucket, COUNT(*) AS n
            FROM loom_events
            WHERE ts >= ?
            GROUP BY bucket
        """,
        params=(float(first * width),),
    )
    counts = {int(r["bucket"]): int(r["n"] or 0) for r in rows}

    return {
        "bucket_seconds": width,
        "points": [
            {"bucket_ts": b * width, "count": counts.get(b, 0)}
            for b in range(first, last + 1)
        ],
    }


def tokens_by_provider(
    sink: EventSink, *, window_seconds: int | None = None
) -> list[dict[str, Any]]:
    """Total tokens per provider, plus each provider's share of the total.

    `share_pct` is share of all tokens in the window, so the column sums to 100
    and each number means something on its own. The mockup's bars are drawn
    relative to the busiest provider — that's a scaling choice for the bar
    width, made in the UI, not a different number.
    """
    where, params = _window(window_seconds)
    rows = sink.fetch(
        sql=f"""
            SELECT provider, SUM({_TOKENS}) AS tokens
            FROM loom_events
            WHERE {where}
            GROUP BY provider
            ORDER BY tokens DESC
        """,
        params=params,
    )
    out = [
        {"provider": r["provider"], "tokens": int(r["tokens"] or 0)}
        for r in rows
    ]
    total = sum(r["tokens"] for r in out)
    for r in out:
        r["share_pct"] = round(100.0 * r["tokens"] / total, 2) if total else 0.0
    return out


def provider_health(
    sink: EventSink, *, window_seconds: int | None = None
) -> list[dict[str, Any]]:
    """Per-provider latency, success rate, and a status of ok / warn / down.

    Cache hits and dedup waits are excluded from the latency figure: they never
    touch the provider, so folding their ~0ms into the average measures our
    cache, not their service.

    A provider whose only calls in the window were cached therefore has no
    latency measurement at all. That reports as None, not 0.0 — "we didn't call
    them" and "they answered instantly" are different facts, and a health panel
    that renders the first as `0ms ok` is lying.
    """
    where, params = _window(window_seconds)
    rows = sink.fetch(
        sql=f"""
            SELECT
                provider,
                COUNT(*) AS calls,
                AVG(CASE WHEN cached = 0 AND deduped = 0
                         THEN latency_ms END) AS avg_latency_ms,
                SUM(CASE WHEN cached = 0 AND deduped = 0
                         THEN 1 ELSE 0 END) AS upstream_calls,
                SUM(CASE WHEN ok = 1 THEN 1 ELSE 0 END) AS ok_calls
            FROM loom_events
            WHERE {where}
            GROUP BY provider
        """,
        params=params,
    )

    out: list[dict[str, Any]] = []
    for r in rows:
        calls = int(r["calls"] or 0)
        ok_calls = int(r["ok_calls"] or 0)
        latency = r["avg_latency_ms"]
        out.append({
            "provider": r["provider"],
            "calls": calls,
            "upstream_calls": int(r["upstream_calls"] or 0),
            "avg_latency_ms": round(float(latency), 2) if latency is not None else None,
            "ok_pct": round(100.0 * ok_calls / calls, 2) if calls else 0.0,
        })

    latencies = sorted(
        r["avg_latency_ms"] for r in out if r["avg_latency_ms"] is not None
    )
    median = latencies[len(latencies) // 2] if latencies else 0.0

    for r in out:
        latency = r["avg_latency_ms"]
        if r["ok_pct"] == 0.0:
            r["status"] = "down"
        elif r["ok_pct"] < OK_PCT_HEALTHY:
            r["status"] = "warn"
        elif median and latency is not None and latency > median * LATENCY_WARN_RATIO:
            r["status"] = "warn"
        else:
            r["status"] = "ok"

    # Unmeasured providers sort last rather than leading as if they were fastest.
    out.sort(key=lambda r: (r["avg_latency_ms"] is None, r["avg_latency_ms"] or 0.0))
    return out


def cost_saved_estimate(
    sink: EventSink, *, window_seconds: int | None = None
) -> dict[str, Any]:
    """Spend avoided by cache hits and dedup, in USD.

    Loom stores the *enriched* result in the cache (`_loom.py:417`), and a cache
    hit logs that same dict — so a cached row carries the exact cost of the
    upstream call it replaced. That is the avoided spend, recorded rather than
    inferred, and it's what this sums.

    Rows whose cost is NULL (unpriced model, failure) fall back to the average
    cost of real calls to that same model, then to the global average. Per-model
    because a cached gpt-4o hit and a cached gpt-4o-mini hit are worth very
    different amounts. `method` reports which paths actually contributed.
    """
    where, params = _window(window_seconds)

    rows = sink.fetch(
        sql=f"""
            SELECT
                model,
                COUNT(*) AS saved_calls,
                SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced_calls,
                COALESCE(SUM(cost_usd), 0.0) AS priced_usd
            FROM loom_events
            WHERE {where} AND (cached = 1 OR deduped = 1)
            GROUP BY model
        """,
        params=params,
    )
    if not rows:
        return {
            "usd": 0.0,
            "basis": {"saved_calls": 0, "priced_calls": 0, "estimated_calls": 0},
            "method": "no cached or deduped calls in window",
        }

    baselines = {
        r["model"]: float(r["avg_cost"])
        for r in sink.fetch(
            sql=f"""
                SELECT model, AVG(cost_usd) AS avg_cost
                FROM loom_events
                WHERE {where} AND cached = 0 AND deduped = 0 AND ok = 1
                      AND cost_usd IS NOT NULL
                GROUP BY model
            """,
            params=params,
        )
        if r["avg_cost"] is not None
    }
    global_avg = (
        sum(baselines.values()) / len(baselines) if baselines else 0.0
    )

    usd = 0.0
    saved_calls = priced_calls = estimated_calls = 0
    used_model_baseline = used_global = unpriced = False

    for r in rows:
        n = int(r["saved_calls"] or 0)
        priced = int(r["priced_calls"] or 0)
        saved_calls += n
        priced_calls += priced
        usd += float(r["priced_usd"] or 0.0)

        gap = n - priced
        if gap <= 0:
            continue
        estimated_calls += gap
        if r["model"] in baselines:
            usd += gap * baselines[r["model"]]
            used_model_baseline = True
        elif global_avg:
            usd += gap * global_avg
            used_global = True
        else:
            unpriced = True

    parts = ["recorded cost of the cached/deduped call"]
    if used_model_baseline:
        parts.append("per-model average for unpriced rows")
    if used_global:
        parts.append("global average where the model had no baseline")
    if unpriced:
        parts.append("no baseline available for some rows — counted as $0")

    return {
        "usd": round(usd, 6),
        "basis": {
            "saved_calls": saved_calls,
            "priced_calls": priced_calls,
            "estimated_calls": estimated_calls,
        },
        "method": "; ".join(parts),
    }


def recent_retries(
    sink: EventSink, *, limit: int = 8, window_seconds: int | None = None
) -> list[dict[str, Any]]:
    """Calls that took more than one attempt, newest first.

    Its own query rather than a filter over `tail()`: the last retry may be
    hundreds of rows back, and a panel that goes blank because the log moved on
    is worse than no panel.

    This is the failover panel from the design doc, narrowed. Loom's router
    attaches its decision trace to the returned result dict rather than logging
    it, so no consumer-side handler can observe a failover. `retries` is a real
    recorded column, so that's what this reports.
    """
    where, params = _window(window_seconds)
    return sink.fetch(
        sql=f"""
            SELECT id, ts, provider, model, retries, ok, error_type
            FROM loom_events
            WHERE {where} AND retries > 0
            ORDER BY id DESC
            LIMIT ?
        """,
        params=(*params, int(limit)),
    )


def tail(
    sink: EventSink, *, limit: int = 50, after_id: int | None = None
) -> list[dict[str, Any]]:
    """Most recent events, newest first.

    Own query rather than a wrapper over `queries.recent()`, which doesn't
    select `id` — and `id` is what makes polling incremental.
    """
    sql = """
        SELECT id, ts, provider, modality, model, upstream_model,
               latency_ms, input_tokens, output_tokens, total_tokens,
               cost_usd, ok, cached, deduped, retries, tags,
               error_type, error
        FROM loom_events
        {where}
        ORDER BY id DESC
        LIMIT ?
    """
    if after_id is not None:
        rows = sink.fetch(
            sql=sql.format(where="WHERE id > ?"),
            params=(int(after_id), int(limit)),
        )
    else:
        rows = sink.fetch(sql=sql.format(where=""), params=(int(limit),))

    for r in rows:
        if r.get("tags"):
            try:
                r["tags"] = json.loads(r["tags"])
            except (ValueError, TypeError):
                pass
    return rows
