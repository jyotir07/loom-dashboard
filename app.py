"""Flask app — startup wiring plus read-only JSON over the captured events.

Capture is two lines at startup: a SQLiteSink for storage and a LoomLogHandler
on the `loom` logger to feed it. After that every generate() call anywhere in
the process is recorded, with no changes at the call sites.

Read-only, bound to 127.0.0.1, no auth. See the README before exposing it.
"""

from __future__ import annotations

import logging
import os
import time

import loom
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from loom.observability import LoomLogHandler, SQLiteSink
from loom.observability import queries

import metrics
import traffic

load_dotenv()

DEFAULT_WINDOW = "24h"
CHART_BUCKETS = 60

# Loom resolves vendor keys as <PROVIDER>_API_KEY at call time; these are the
# providers whose env var doesn't follow from the catalog name.
_KEY_ENV_OVERRIDES = {
    "zhipu": "ZAI_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "seedream": "ARK_API_KEY",
}


def _key_env(provider: str) -> str:
    return _KEY_ENV_OVERRIDES.get(provider, f"{provider.upper()}_API_KEY")


def _install_capture(db_path: str) -> SQLiteSink:
    """Point the `loom` logger at a sink.

    The logger's own level matters as much as the handler's: `log_call` emits
    successes at INFO, and the `loom` logger inherits root's default of WARNING,
    so without this setLevel every successful call is dropped before any handler
    sees it — leaving a dashboard that only ever shows failures.

    `propagate = False` keeps our capture from also printing Loom's call lines
    through the root handler and spamming the console.
    """
    sink = SQLiteSink(db_path)
    logger = logging.getLogger("loom")

    # The `loom` logger is global, so a second create_app() in the same process
    # would otherwise stack handlers and write every call once per sink.
    for handler in [h for h in logger.handlers if isinstance(h, LoomLogHandler)]:
        logger.removeHandler(handler)

    logger.setLevel(logging.INFO)
    logger.addHandler(LoomLogHandler(sink))
    logger.propagate = False
    return sink


def _window_seconds() -> int | None:
    """Parse ?window= against Loom's own vocabulary rather than a second one."""
    key = request.args.get("window", DEFAULT_WINDOW)
    if key not in queries.WINDOWS:
        raise ValueError(key)
    return queries.WINDOWS[key]


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SINK"] = _install_capture(
        db_path or os.getenv("LOOM_EVENTS_DB", "loom_events.db")
    )
    app.config["DEMO"] = traffic.DemoTraffic(
        app.config["SINK"], rate_per_sec=float(os.getenv("DEMO_RATE", "2"))
    )

    if os.getenv("DEMO_MODE", "0") == "1":
        app.config["DEMO"].start()

    @app.errorhandler(ValueError)
    def _bad_window(exc: ValueError):
        return jsonify({
            "error": f"unknown window {exc}",
            "valid": list(queries.WINDOWS),
        }), 400

    @app.get("/")
    def dashboard():
        return render_template("dashboard.html", windows=list(queries.WINDOWS))

    @app.get("/api/snapshot")
    def snapshot():
        """Every panel in one read.

        The UI polls this instead of the four routes below: one request per
        tick, and all panels reflect the same instant rather than four
        staggered reads.
        """
        sink = app.config["SINK"]
        window = _window_seconds()
        summary = queries.summary(sink, window_seconds=window)
        return jsonify({
            "generated_at": time.time(),
            "window": request.args.get("window", DEFAULT_WINDOW),
            "cards": {
                "requests_per_min": metrics.requests_per_min(sink),
                "p95_latency_ms": metrics.p95_latency(sink, window_seconds=window),
                "cache_hit_pct": summary["cache_hit_pct"],
                "cost_saved": metrics.cost_saved_estimate(sink, window_seconds=window),
            },
            "summary": summary,
            "timeseries": metrics.timeseries(
                sink, window_seconds=window, buckets=CHART_BUCKETS
            ),
            "providers": metrics.provider_health(sink, window_seconds=window),
            "tokens": metrics.tokens_by_provider(sink, window_seconds=window),
            "retries": metrics.recent_retries(sink, window_seconds=window),
            "demo_running": app.config["DEMO"].running,
        })

    # The individual routes exist to keep the API browsable and curl-verifiable.

    @app.get("/api/metrics/summary")
    def metrics_summary():
        sink = app.config["SINK"]
        window = _window_seconds()
        summary = queries.summary(sink, window_seconds=window)
        return jsonify({
            **summary,
            "requests_per_min": metrics.requests_per_min(sink),
            "p95_latency_ms": metrics.p95_latency(sink, window_seconds=window),
            "cost_saved": metrics.cost_saved_estimate(sink, window_seconds=window),
        })

    @app.get("/api/metrics/providers")
    def metrics_providers():
        return jsonify(
            metrics.provider_health(app.config["SINK"], window_seconds=_window_seconds())
        )

    @app.get("/api/metrics/timeseries")
    def metrics_timeseries():
        buckets = request.args.get("buckets", CHART_BUCKETS, type=int)
        return jsonify(metrics.timeseries(
            app.config["SINK"],
            window_seconds=_window_seconds(),
            buckets=max(1, min(buckets, 500)),
        ))

    @app.get("/api/metrics/tokens")
    def metrics_tokens():
        return jsonify(
            metrics.tokens_by_provider(
                app.config["SINK"], window_seconds=_window_seconds()
            )
        )

    @app.get("/api/metrics/retries")
    def metrics_retries():
        limit = max(1, min(request.args.get("n", 8, type=int), 100))
        return jsonify(metrics.recent_retries(
            app.config["SINK"], limit=limit, window_seconds=_window_seconds()
        ))

    @app.get("/api/logs/tail")
    def logs_tail():
        limit = max(1, min(request.args.get("n", 50, type=int), 500))
        after_id = request.args.get("after_id", type=int)
        rows = metrics.tail(app.config["SINK"], limit=limit, after_id=after_id)
        return jsonify({"events": rows})

    # --- demo traffic ---------------------------------------------------

    @app.post("/api/demo/start")
    def demo_start():
        started = app.config["DEMO"].start()
        return jsonify({"running": True, "changed": started})

    @app.post("/api/demo/stop")
    def demo_stop():
        stopped = app.config["DEMO"].stop()
        return jsonify({"running": False, "changed": stopped})

    # --- playground -----------------------------------------------------

    def _client() -> loom.Loom:
        """One lazily-built client, reused. Building it reads .env, so it is
        deferred until someone actually makes a call — the dashboard must run
        with no keys at all."""
        if "LOOM" not in app.config:
            app.config["LOOM"] = loom.Loom.from_env()
        return app.config["LOOM"]

    @app.get("/api/playground/models")
    def playground_models():
        """Only providers whose key is actually present.

        Offering a model we have no credentials for just buys the user a 401
        with extra steps.
        """
        catalog = _client().catalog
        out = []
        for provider in catalog.providers():
            if not os.getenv(_key_env(provider)):
                continue
            models = [m["id"] for m in catalog.models(provider, "text") if m.get("id")]
            if models:
                out.append({"provider": provider, "models": sorted(models)})
        return jsonify({"providers": out})

    @app.post("/api/playground")
    def playground():
        """Fire a real generate() and let the normal handler path record it.

        Nothing here writes to the sink: the call is logged by Loom, captured
        by LoomLogHandler, and shows up in the dashboard like any other call.
        The `playground` tag is what distinguishes it from demo rows.
        """
        body = request.get_json(silent=True) or {}
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "prompt is required"}), 400

        provider = body.get("provider") or None
        model = body.get("model") or None
        if provider and not os.getenv(_key_env(provider)):
            return jsonify({
                "error": f"no API key for {provider} — set {_key_env(provider)}"
            }), 400

        started = time.time()
        try:
            result = _client().generate(
                prompt=prompt,
                provider=provider,
                model=model,
                tags={"source": "playground"},
            )
        except Exception as exc:
            # Loom has already logged the failure with its structured payload,
            # so it lands in the log panel too. Report it rather than 500ing.
            return jsonify({
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": round((time.time() - started) * 1000, 1),
            }), 502

        return jsonify({
            "text": result.get("text"),
            "provider": result.get("provider"),
            "model": result.get("model"),
            "upstream_model": result.get("upstream_model"),
            "usage": result.get("usage"),
            "cost": result.get("cost"),
            "elapsed_ms": round((time.time() - started) * 1000, 1),
        })

    return app


app = create_app()


if __name__ == "__main__":
    # 127.0.0.1 deliberately: these routes report spend to anyone who can reach
    # them. 5001 leaves 3001 free for Loom's own demo app.
    app.run(
        host="127.0.0.1",
        port=int(os.getenv("PORT", "5001")),
        debug=os.getenv("FLASK_DEBUG", "0") == "1",
    )
