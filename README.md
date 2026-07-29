# Loom Dashboard

A real-time observability dashboard for [Loom](https://github.com/jyotir07/Loom) — provider
health, cost, cache effectiveness, and throughput, live, from the call records Loom
already emits.

This is a **standalone consumer app**. It depends on `loom-router` as a library and adds
nothing to it. You point it at a Loom deployment, and every `generate()` call anywhere in
that process shows up on the dashboard — no changes to your call sites, no new
instrumentation.

```
┌───────────────────────────────────────────────────────────────────┐
│  REQUESTS / MIN     P95 LATENCY     CACHE HIT RATE     COST SAVED   │
│     12,847            312ms             61.4%            $1,284      │
├───────────────────────────────────────┬───────────────────────────┤
│  REQUESTS OVER TIME (throughput)       │  PROVIDER HEALTH           │
│  ╱╲    ╱╲   ╱╲╱╲                       │  ● OpenAI       370ms  ✓   │
│ ╱  ╲__╱  ╲_╱    ╲___                   │  ● Anthropic    280ms  ✓   │
│                                        │  ● xAI Grok     480ms  ⚠   │
│  TOKEN USAGE · BY PROVIDER             │  ...                       │
│  OpenAI    ████████████ 82%            ├───────────────────────────┤
│  Anthropic ████████ 64%                │  RECENT RETRIES            │
│  Gemini    █████ 47%                   │  retry openai · 429   2s   │
│  ...                                   │                            │
├───────────────────────────────────────┴───────────────────────────┤
│  LIVE REQUEST LOG · STRUCTURED INFO                    tailing 1.3s │
│  15:50:52  ok  openai  gpt-4o-mini   483.0ms  cached   $0.0001686   │
└───────────────────────────────────────────────────────────────────┘
```

---

## Status

All four stages are built. `pytest` covers the aggregation layer (30 tests); every route
has been exercised against seeded, empty, and error-heavy databases.

| Stage | Scope | State |
| ----- | ----- | ----- |
| 1 | Capture wiring, `metrics.py` aggregations, synthetic traffic, tests | done |
| 2 | Flask app + JSON endpoints | done |
| 3 | Dashboard UI — template, CSS, SVG charts | done |
| 4 | Playground (real Loom calls) + demo-mode toggle | done |

Each stage is independently runnable. Stage 1 is verifiable from a CLI with no web layer:

```bash
python traffic.py seed --count 500     # write synthetic events
python traffic.py show --window 3600   # print every aggregation as JSON
```

**One thing is unverified:** the playground has never been run against a live provider,
because this checkout has no API keys. Every other path is tested. Add a key to `.env` and
fire one call to close that gap.

---

## How it works

Loom emits one structured record per `generate()` call at `INFO` on the `loom` logger,
with the full payload attached as `record.loom`. The library adds no handlers of its own —
that's deliberate, a "no surprises" rule — so the records are discarded unless something
captures them.

This app is that something. Four layers:

**1 · Capture.** A `LoomLogHandler` on the `loom` logger drains records into a
`SQLiteSink`. Both ship inside `loom.observability`. Wiring is two lines at startup:

```python
sink = SQLiteSink(os.getenv("LOOM_EVENTS_DB", "loom_events.db"))
logging.getLogger("loom").addHandler(LoomLogHandler(sink))
```

The handler path is used rather than Loom's per-client `analytics=` sink because it
captures *every* client in the process, including module-level `loom.generate()`, and
because it works unchanged across Loom versions.

**2 · Aggregation.** `loom.observability.queries` already covers `summary`, `by_provider`,
`by_model`, and `recent`. `metrics.py` adds what the dashboard needs on top: requests/min,
p95 latency, zero-filled per-minute time buckets, tokens by provider, provider health
status, and the cost-saved estimate. Everything goes through the public `EventSink.fetch`
protocol — no reaching into the sink's connection.

**3 · API.** Read-only JSON routes over those functions.

**4 · UI.** One Jinja template plus vanilla CSS and JS. Hand-rolled SVG for the throughput
chart. No build step, no npm, no CDN — `pip install` and run.

---

## Quick start

Requires Python 3.11+.

```bash
python -m venv .venv
.venv\Scripts\activate          # bash/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

`requirements.txt` pins Loom to the wheel built from the sibling repo
(`../Loom/dist/loom_router-2.0.0-py3-none-any.whl`) rather than an editable install, so the
dashboard runs against a fixed Loom build instead of whatever is in your working tree.
Swap it for a PyPI pin or `pip install -e ../Loom` if you want the opposite.

Copy `.env.example` to `.env` and fill in whichever provider keys you want to exercise —
Loom reads them via `Loom.from_env()`:

```
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
```

Then:

```bash
python app.py
```

Dashboard at <http://127.0.0.1:5001>. Port 5001 so it can run alongside Loom's own demo
app on 3001.

### Seeing data

Two ways to fill the dashboard:

- **Playground** — a panel in the UI fires a real `loom.generate()` call with your keys.
  Real provider, real latency, real cost, and you watch it land.
- **Demo mode** — a synthetic generator writes plausible events straight into the sink.
  No keys, no spend, a populated screen on demand. Enable with `DEMO_MODE=1` or from the
  UI.

Synthetic events are tagged `{"source": "demo"}` in the `tags` column, so demo traffic is
always distinguishable from real traffic in the database. A demo that can't tell you which
numbers were fake isn't worth much.

---

## API

All routes are read-only and accept `?window=1h|24h|7d|30d|all` (default `24h`), matching
`loom.observability.queries.WINDOWS`.

| Route | Returns |
| ----- | ------- |
| `GET /` | the dashboard |
| `GET /api/snapshot` | all four panels in one response — what the UI actually polls |
| `GET /api/metrics/summary` | the four top cards |
| `GET /api/metrics/providers` | per-provider latency, error rate, health status |
| `GET /api/metrics/timeseries` | per-minute request counts for the throughput chart |
| `GET /api/metrics/tokens` | token usage by provider |
| `GET /api/metrics/retries?n=8` | recent calls that took more than one attempt |
| `GET /api/logs/tail?n=50&after_id=` | recent events for the live log |

Three routes mutate, and are the only ones that do:

| Route | Does |
| ----- | ---- |
| `POST /api/demo/start` · `/api/demo/stop` | control the synthetic generator |
| `POST /api/playground` | fire one real `loom.generate()` and return the response |
| `GET /api/playground/models` | providers with a key present, and their text models |

The individual metric routes exist to keep the API browsable and `curl`-verifiable. The
frontend uses `/api/snapshot` instead: one request every 2s rather than four, and all
panels reflect the same instant rather than four staggered reads. The log tail polls
separately at 1.3s and sends only rows newer than `after_id`.

There is no auth. This binds to `127.0.0.1`. If you expose it, put it behind your own
login: the read routes will happily report your spend to anyone who can reach them, and
`POST /api/playground` spends real money on your keys.

---

## Two honest caveats

### "Cost saved" is close to exact, but still an estimate

The design doc assumed the avoided cost isn't recorded anywhere. It very nearly is. Loom
puts the *enriched* result into the cache (`_loom.py:417`), and a cache hit logs that same
dict — so a cached row carries the exact `cost_usd` of the upstream call it replaced. Dedup
waiters get the same enriched result. The card sums those recorded costs rather than
inferring from an average.

The per-model average is the fallback, used only for rows with no recorded cost (an
unpriced model, a failure). Per-model rather than one blended average, because a cached
`gpt-4o` hit and a cached `gpt-4o-mini` hit are worth very different amounts. The card
reports which paths it actually used — hover the `est` badge.

It stays labelled an estimate because "what you would have paid" is counterfactual: it
assumes the call would have been made, at that model, at that price. A `cost_saved_usd`
field on Loom's `log_call` would settle it, and that's an upstream change.

One consequence worth knowing: because cached rows carry the original call's price,
`SUM(cost_usd)` over all events **overstates** real spend. Actual spend is the sum over
rows where `cached = 0 AND deduped = 0`.

### Failover isn't shown

Loom's router attaches its decision trace to the returned result dict rather than logging
it, so no consumer-side handler can observe a failover. That panel shows **retries**
instead, which Loom does record per call as a real column. Failover waits on Loom emitting
it.

---

## Layout

```
app.py                     Flask app, startup wiring, routes
metrics.py                 aggregations beyond loom.observability.queries
traffic.py                 synthetic demo generator + seed/show CLI
templates/dashboard.html
static/dashboard.css
static/dashboard.js
tests/test_metrics.py      aggregations vs hand-computed values
requirements.txt           pins Loom to ../Loom/dist/*.whl
.env.example               provider keys, DEMO_MODE, PORT
obser_db.md                the design doc this implements
```

Colors come from the `dataviz` skill's reference palette, dark column, validated against
this surface (`#1a1a19`). The typeface is the one deliberate substitution — that palette
parameterizes a UI sans, and the brief here is a monospace terminal.

---

## Design principles

- **Loom stays untouched.** This repo consumes the library; it does not patch it. Anything
  needing an upstream change is either estimated and labelled, or left out.
- **No per-call-site changes.** Capture happens entirely through the logging handler, so
  existing `generate()` calls light up the dashboard for free.
- **No build step.** Vanilla JS and hand-rolled SVG. Clone, `pip install`, run.
- **Read-only endpoints.** The dashboard observes; it never mutates.
</content>
