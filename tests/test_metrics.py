"""Aggregations asserted against hand-computed values on a seeded InMemorySink.

The empty-database case gets its own test for every function: a dashboard that
raises before its first request is worse than one showing zeros.
"""

from __future__ import annotations

import time

import pytest
from loom.observability.sink import InMemorySink

import metrics


@pytest.fixture
def sink():
    s = InMemorySink()
    yield s
    s.close()


def write(sink, **over):
    """One event with sane defaults; override whatever the test cares about."""
    event = {
        "ts": time.time(),
        "provider": "openai",
        "modality": "text",
        "model": "gpt-4o-mini",
        "upstream_model": "gpt-4o-mini",
        "latency_ms": 100.0,
        "input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 30,
        "cost_usd": 0.001,
        "ok": True,
        "cached": False,
        "deduped": False,
        "retries": 0,
    }
    event.update(over)
    sink.write(event)
    return event


# --- empty database ---------------------------------------------------------

def test_empty_database_returns_zeros(sink):
    assert metrics.requests_per_min(sink) == 0.0
    assert metrics.p95_latency(sink) == 0.0
    assert metrics.tokens_by_provider(sink) == []
    assert metrics.provider_health(sink) == []
    assert metrics.tail(sink) == []

    saved = metrics.cost_saved_estimate(sink)
    assert saved["usd"] == 0.0
    assert saved["basis"]["saved_calls"] == 0

    series = metrics.timeseries(sink, window_seconds=3600, buckets=10)
    assert len(series["points"]) == 10
    assert all(p["count"] == 0 for p in series["points"])


# --- requests per minute ----------------------------------------------------

def test_requests_per_min_counts_only_trailing_60s(sink):
    now = time.time()
    for _ in range(4):
        write(sink, ts=now - 10)
    write(sink, ts=now - 90)      # outside the window
    write(sink, ts=now - 3600)    # well outside

    assert metrics.requests_per_min(sink) == 4.0


# --- p95 --------------------------------------------------------------------

@pytest.mark.parametrize(
    "latencies, expected",
    [
        ([42.0], 42.0),
        # The design doc's int(n*0.95)-1 gives index 0 here — the *lower* of two
        # samples. Nearest-rank is ceil(0.95*2)=2 -> index 1.
        ([10.0, 20.0], 20.0),
        (list(range(1, 21)), 19.0),    # ceil(0.95*20)=19 -> index 18
        (list(range(1, 101)), 95.0),   # ceil(0.95*100)=95 -> index 94
    ],
)
def test_p95_nearest_rank(sink, latencies, expected):
    for ms in latencies:
        write(sink, latency_ms=float(ms))
    assert metrics.p95_latency(sink) == expected


def test_p95_respects_window(sink):
    now = time.time()
    write(sink, ts=now - 10, latency_ms=100.0)
    write(sink, ts=now - 7200, latency_ms=9999.0)   # older than the window

    assert metrics.p95_latency(sink, window_seconds=3600) == 100.0
    assert metrics.p95_latency(sink, window_seconds=None) == 9999.0


# --- timeseries -------------------------------------------------------------

def test_timeseries_zero_fills_idle_buckets(sink):
    now = time.time()
    write(sink, ts=now)
    write(sink, ts=now)
    write(sink, ts=now - 300)   # five minutes back

    series = metrics.timeseries(sink, window_seconds=3600, buckets=60)
    assert series["bucket_seconds"] == 60
    assert len(series["points"]) == 60

    by_bucket = {p["bucket_ts"]: p["count"] for p in series["points"]}
    assert by_bucket[int(now // 60) * 60] == 2
    assert by_bucket[int((now - 300) // 60) * 60] == 1
    assert sum(by_bucket.values()) == 3
    # Everything else is present and zero, not absent.
    assert sum(1 for v in by_bucket.values() if v == 0) == 58


def test_timeseries_widens_buckets_for_long_windows(sink):
    # 30 days over 60 buckets is 12h per bucket — never 1 minute, which would be
    # 43,200 points.
    series = metrics.timeseries(sink, window_seconds=30 * 86400, buckets=60)
    assert series["bucket_seconds"] == 30 * 86400 // 60
    assert len(series["points"]) == 60


def test_timeseries_buckets_are_contiguous(sink):
    series = metrics.timeseries(sink, window_seconds=3600, buckets=12)
    stamps = [p["bucket_ts"] for p in series["points"]]
    width = series["bucket_seconds"]
    assert stamps == sorted(stamps)
    assert all(b - a == width for a, b in zip(stamps, stamps[1:]))


# --- tokens by provider -----------------------------------------------------

def test_tokens_by_provider_shares_sum_to_100(sink):
    write(sink, provider="openai", total_tokens=300)
    write(sink, provider="openai", total_tokens=300)
    write(sink, provider="anthropic", total_tokens=400)

    rows = metrics.tokens_by_provider(sink)
    assert rows == [
        {"provider": "openai", "tokens": 600, "share_pct": 60.0},
        {"provider": "anthropic", "tokens": 400, "share_pct": 40.0},
    ]


def test_tokens_falls_back_to_input_plus_output(sink):
    write(sink, provider="gemini", total_tokens=None, input_tokens=7, output_tokens=5)
    rows = metrics.tokens_by_provider(sink)
    assert rows[0]["tokens"] == 12


# --- provider health --------------------------------------------------------

def test_provider_health_status_thresholds(sink):
    for _ in range(100):
        write(sink, provider="fast", latency_ms=100.0, ok=True)
    for _ in range(100):
        write(sink, provider="mid", latency_ms=300.0, ok=True)
    # 1000ms > median(300) * 2.5 -> warn on latency alone
    for _ in range(100):
        write(sink, provider="slow", latency_ms=1000.0, ok=True)
    # 95% success -> below the 99% healthy threshold
    for i in range(100):
        write(sink, provider="flaky", latency_ms=200.0, ok=i >= 5)
    for _ in range(10):
        write(sink, provider="dead", latency_ms=200.0, ok=False)

    status = {r["provider"]: r for r in metrics.provider_health(sink)}
    assert status["fast"]["status"] == "ok"
    assert status["mid"]["status"] == "ok"
    assert status["slow"]["status"] == "warn"
    assert status["flaky"]["status"] == "warn"
    assert status["flaky"]["ok_pct"] == 95.0
    assert status["dead"]["status"] == "down"
    assert status["dead"]["ok_pct"] == 0.0


def test_provider_health_excludes_cached_from_latency(sink):
    # Cache hits never touch the provider; averaging their ~0ms in would measure
    # our cache rather than their service.
    write(sink, provider="openai", latency_ms=400.0, cached=False)
    write(sink, provider="openai", latency_ms=600.0, cached=False)
    for _ in range(50):
        write(sink, provider="openai", latency_ms=0.2, cached=True)

    row = metrics.provider_health(sink)[0]
    assert row["avg_latency_ms"] == 500.0
    assert row["calls"] == 52
    assert row["upstream_calls"] == 2


def test_provider_health_reports_no_latency_when_only_cached(sink):
    # Never called upstream in this window, so there is nothing to measure.
    # 0.0 would read as "answered instantly", which is a different claim.
    for _ in range(5):
        write(sink, provider="openai", latency_ms=0.3, cached=True)

    row = metrics.provider_health(sink)[0]
    assert row["avg_latency_ms"] is None
    assert row["upstream_calls"] == 0
    assert row["status"] == "ok"


def test_provider_health_sorts_unmeasured_providers_last(sink):
    write(sink, provider="measured", latency_ms=900.0, cached=False)
    write(sink, provider="cached-only", latency_ms=0.2, cached=True)

    assert [r["provider"] for r in metrics.provider_health(sink)] == [
        "measured", "cached-only"
    ]


def test_provider_health_single_provider_is_ok(sink):
    # Median equals its own latency, so the ratio is 1.0 and nothing warns.
    write(sink, provider="solo", latency_ms=5000.0, ok=True)
    assert metrics.provider_health(sink)[0]["status"] == "ok"


# --- cost saved -------------------------------------------------------------

def test_cost_saved_uses_recorded_cost_of_cached_rows(sink):
    # Loom caches the enriched result, so a hit logs the original call's cost.
    write(sink, model="gpt-4o", cost_usd=0.10, cached=False)
    write(sink, model="gpt-4o", cost_usd=0.10, cached=True)
    write(sink, model="gpt-4o", cost_usd=0.10, deduped=True)

    saved = metrics.cost_saved_estimate(sink)
    assert saved["usd"] == pytest.approx(0.20)
    assert saved["basis"] == {
        "saved_calls": 2, "priced_calls": 2, "estimated_calls": 0
    }


def test_cost_saved_falls_back_to_per_model_average(sink):
    # Two priced live calls for this model: average 0.04.
    write(sink, model="gpt-4o", cost_usd=0.03, cached=False)
    write(sink, model="gpt-4o", cost_usd=0.05, cached=False)
    write(sink, model="gpt-4o", cost_usd=None, cached=True)

    saved = metrics.cost_saved_estimate(sink)
    assert saved["usd"] == pytest.approx(0.04)
    assert saved["basis"]["estimated_calls"] == 1
    assert "per-model average" in saved["method"]


def test_cost_saved_per_model_not_blended(sink):
    # A cached mini hit is worth far less than a cached 4o hit; one blended
    # average would price them the same.
    write(sink, model="gpt-4o", cost_usd=1.00, cached=False)
    write(sink, model="gpt-4o-mini", cost_usd=0.01, cached=False)
    write(sink, model="gpt-4o-mini", cost_usd=None, cached=True)

    saved = metrics.cost_saved_estimate(sink)
    assert saved["usd"] == pytest.approx(0.01)


def test_cost_saved_uses_global_average_for_unpriced_model(sink):
    write(sink, model="known", cost_usd=0.02, cached=False)
    write(sink, model="mystery", cost_usd=None, cached=True)

    saved = metrics.cost_saved_estimate(sink)
    assert saved["usd"] == pytest.approx(0.02)
    assert "global average" in saved["method"]


def test_cost_saved_ignores_calls_outside_window(sink):
    now = time.time()
    write(sink, ts=now - 7200, cost_usd=5.00, cached=True)
    write(sink, ts=now, cost_usd=0.25, cached=True)

    saved = metrics.cost_saved_estimate(sink, window_seconds=3600)
    assert saved["usd"] == pytest.approx(0.25)


# --- recent retries ---------------------------------------------------------

def test_recent_retries_only_returns_retried_calls(sink):
    write(sink, provider="openai", retries=0)
    write(sink, provider="openai", retries=2, ok=False, error_type="RateLimitError")
    write(sink, provider="xai", retries=1)

    rows = metrics.recent_retries(sink)
    assert [r["provider"] for r in rows] == ["xai", "openai"]
    assert rows[1]["retries"] == 2
    assert rows[1]["error_type"] == "RateLimitError"


def test_recent_retries_reaches_past_the_log_tail(sink):
    # The panel must not go blank just because the log moved on.
    write(sink, provider="openai", retries=3)
    for _ in range(200):
        write(sink, provider="openai", retries=0)

    assert metrics.tail(sink, limit=50) and all(
        r["retries"] == 0 for r in metrics.tail(sink, limit=50)
    )
    assert metrics.recent_retries(sink)[0]["retries"] == 3


def test_recent_retries_respects_window_and_limit(sink):
    now = time.time()
    write(sink, ts=now - 7200, retries=9)
    for _ in range(5):
        write(sink, ts=now, retries=1)

    rows = metrics.recent_retries(sink, window_seconds=3600, limit=3)
    assert len(rows) == 3
    assert all(r["retries"] == 1 for r in rows)


def test_recent_retries_empty_database(sink):
    assert metrics.recent_retries(sink) == []


# --- tail -------------------------------------------------------------------

def test_tail_is_newest_first_and_carries_id(sink):
    for i in range(5):
        write(sink, latency_ms=float(i))

    rows = metrics.tail(sink, limit=3)
    assert [r["latency_ms"] for r in rows] == [4.0, 3.0, 2.0]
    assert [r["id"] for r in rows] == sorted((r["id"] for r in rows), reverse=True)


def test_tail_after_id_returns_only_newer_rows(sink):
    for i in range(5):
        write(sink, latency_ms=float(i))
    seen = metrics.tail(sink, limit=50)
    newest = seen[0]["id"]

    assert metrics.tail(sink, after_id=newest) == []

    write(sink, latency_ms=99.0)
    fresh = metrics.tail(sink, after_id=newest)
    assert len(fresh) == 1
    assert fresh[0]["latency_ms"] == 99.0


def test_tail_parses_tags_json(sink):
    write(sink, tags={"source": "demo"})
    assert metrics.tail(sink)[0]["tags"] == {"source": "demo"}


def test_tail_keeps_error_fields(sink):
    write(sink, ok=False, error_type="RateLimitError", error="429 slow down")
    row = metrics.tail(sink)[0]
    assert row["ok"] == 0
    assert row["error_type"] == "RateLimitError"
