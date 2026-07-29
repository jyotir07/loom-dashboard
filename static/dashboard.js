"use strict";

/* Dashboard front-end: poll, render, repeat. No build step, no dependencies.
   The chart is hand-rolled SVG — one series, so the work is a path and an
   axis, which a library would not make shorter. */

const SNAPSHOT_MS = 2000;
const TAIL_MS = 1300;
const LOG_MAX_ROWS = 300;

const PAD = { top: 14, right: 14, bottom: 24, left: 48 };
const CHART_H = 210;

const state = {
  // The UI opens on 1h: this is a live view, and a 24h window on a freshly
  // started process is 23 hours of correctly-zero-filled emptiness. The API's
  // own default stays 24h, as documented.
  window: "1h",
  lastId: null,
  chart: null,      // geometry + points, for the hover layer
  hoverIndex: null,
  timers: [],
};

const $ = (id) => document.getElementById(id);

// --- formatting -----------------------------------------------------------

function compact(n) {
  if (n === null || n === undefined) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1e4) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "K";
  return n.toLocaleString("en-US");
}

function latency(ms) {
  if (ms === null || ms === undefined) return "—";
  if (ms >= 1000) return (ms / 1000).toFixed(2) + "s";
  return Math.round(ms) + "ms";
}

function usd(v) {
  if (v === null || v === undefined) return "—";
  if (v === 0) return "$0";
  if (v < 0.01) return "$" + v.toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
  if (v < 1000) return "$" + v.toFixed(2);
  return "$" + compact(Math.round(v));
}

/** Fixed precision for the log's cost column — trimming trailing zeros makes
 *  a vertically-aligned column ragged. */
function usdPrecise(v) {
  if (!v) return null;
  return "$" + (v < 1 ? v.toFixed(6) : v.toFixed(4));
}

function clockTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("en-GB", { hour12: false });
}

/** Bare clock times are ambiguous once buckets span more than a day, so the
 *  axis picks up a date at that point. */
function axisTime(ts, bucketSeconds) {
  const d = new Date(ts * 1000);
  if (bucketSeconds >= 3600) {
    return d.toLocaleString("en-GB", {
      day: "2-digit", month: "short", hour: "2-digit",
      minute: "2-digit", hour12: false,
    }).replace(",", "");
  }
  return clockTime(ts);
}

function ago(ts) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s / 60) + "m";
  if (s < 86400) return Math.round(s / 3600) + "h";
  return Math.round(s / 86400) + "d";
}

function text(el, value) {
  if (el.textContent !== value) el.textContent = value;
}

// --- chart ----------------------------------------------------------------

/** Round a max up to a clean 1/2/5 x 10^n so axis ticks are readable. */
function niceMax(v) {
  if (v <= 0) return 1;
  const mag = Math.pow(10, Math.floor(Math.log10(v)));
  const step = [1, 2, 2.5, 5, 10].find((s) => v <= s * mag) ?? 10;
  return step * mag;
}

/** A step is readable when it's 1, 2, 2.5 or 5 x 10^n — 125 is a whole
 *  number but nobody reads an axis in units of 125. */
function isNiceStep(s) {
  const mantissa = s / Math.pow(10, Math.floor(Math.log10(s)));
  return [1, 2, 2.5, 5].some((m) => Math.abs(mantissa - m) < 1e-9);
}

/** Ticks that land on round numbers. Splitting the top into fixed quarters
 *  gives values like 3 and 8 on a 0–10 axis; pick the divisor whose step
 *  reads cleanly instead. */
function axisTicks(top) {
  if (top <= 5) {
    return Array.from({ length: Math.round(top) + 1 }, (_, i) => i);
  }
  for (const nice of [true, false]) {
    for (const parts of [5, 4, 3, 2]) {
      const step = top / parts;
      if (Number.isInteger(step) && (!nice || isNiceStep(step))) {
        return Array.from({ length: parts + 1 }, (_, i) => i * step);
      }
    }
  }
  return [0, top];
}

function renderChart(series) {
  const svg = $("chart");
  const wrap = $("chart-wrap");
  const pts = series.points || [];
  const width = Math.max(wrap.clientWidth, 320);
  const height = CHART_H;

  const plotW = width - PAD.left - PAD.right;
  const plotH = height - PAD.top - PAD.bottom;
  const baseY = PAD.top + plotH;

  const counts = pts.map((p) => p.count);
  const top = niceMax(Math.max(...counts, 0));
  const stepX = pts.length > 1 ? plotW / (pts.length - 1) : 0;

  const x = (i) => PAD.left + i * stepX;
  const y = (c) => baseY - (c / top) * plotH;

  const parts = [];

  // Gridlines: solid hairlines, one step off the surface, recessive.
  for (const t of axisTicks(top)) {
    const gy = y(t);
    parts.push(
      `<line x1="${PAD.left}" y1="${gy}" x2="${PAD.left + plotW}" y2="${gy}"
             stroke="var(--grid)" stroke-width="1" shape-rendering="crispEdges"/>`,
      `<text x="${PAD.left - 10}" y="${gy + 3.5}" text-anchor="end"
             fill="var(--muted)" font-size="10"
             style="font-variant-numeric: tabular-nums">${compact(t)}</text>`
    );
  }

  if (pts.length) {
    const line = pts.map((p, i) => `${x(i).toFixed(2)},${y(p.count).toFixed(2)}`);
    // Area is a ~10% wash of the series hue, never a saturated block.
    parts.push(
      `<path d="M ${x(0).toFixed(2)},${baseY} L ${line.join(" L ")} L ${x(pts.length - 1).toFixed(2)},${baseY} Z"
             fill="var(--series-1)" fill-opacity="0.10"/>`,
      `<path d="M ${line.join(" L ")}" fill="none" stroke="var(--series-1)"
             stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>`
    );

    // Direct-label the peak only — a number on every point is unreadable.
    const peak = counts.indexOf(Math.max(...counts));
    if (counts[peak] > 0) {
      parts.push(
        `<circle cx="${x(peak).toFixed(2)}" cy="${y(counts[peak]).toFixed(2)}" r="4"
                 fill="var(--series-1)" stroke="var(--surface)" stroke-width="2"/>`,
        `<text x="${x(peak).toFixed(2)}" y="${(y(counts[peak]) - 11).toFixed(2)}"
               text-anchor="${peak > pts.length - 4 ? "end" : peak < 3 ? "start" : "middle"}"
               fill="var(--ink)" font-size="10" font-weight="600">peak ${compact(counts[peak])}</text>`
      );
    }
  }

  parts.push(
    `<line x1="${PAD.left}" y1="${baseY}" x2="${PAD.left + plotW}" y2="${baseY}"
           stroke="var(--baseline)" stroke-width="1" shape-rendering="crispEdges"/>`
  );

  if (pts.length) {
    const labelAt = [0, Math.floor(pts.length / 2), pts.length - 1];
    const anchors = ["start", "middle", "end"];
    labelAt.forEach((i, k) => {
      parts.push(
        `<text x="${x(i).toFixed(2)}" y="${baseY + 15}" text-anchor="${anchors[k]}"
               fill="var(--muted)" font-size="10"
               style="font-variant-numeric: tabular-nums">${axisTime(pts[i].bucket_ts, series.bucket_seconds)}</text>`
      );
    });
  }

  parts.push(`<g id="chart-hover"></g>`);

  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", width);
  svg.setAttribute("height", height);
  svg.innerHTML = parts.join("");

  state.chart = { pts, x, y, baseY, plotH, stepX, width, height,
                  bucketSeconds: series.bucket_seconds };

  const mins = Math.round((series.bucket_seconds || 60) / 60);
  text($("chart-meta"),
       `${pts.length} buckets · ${mins >= 1 ? mins + "m" : series.bucket_seconds + "s"} each`);

  if (state.hoverIndex !== null) drawHover(state.hoverIndex);
}

function drawHover(i) {
  const c = state.chart;
  const layer = document.querySelector("#chart-hover");
  if (!c || !layer || !c.pts[i]) return;

  const p = c.pts[i];
  const px = c.x(i);
  const py = c.y(p.count);

  layer.innerHTML =
    `<line x1="${px.toFixed(2)}" y1="${PAD.top}" x2="${px.toFixed(2)}" y2="${c.baseY}"
           stroke="var(--baseline)" stroke-width="1" shape-rendering="crispEdges"/>` +
    `<circle cx="${px.toFixed(2)}" cy="${py.toFixed(2)}" r="4.5" fill="var(--series-1)"
             stroke="var(--surface)" stroke-width="2"/>`;

  const tip = $("chart-tip");
  const end = p.bucket_ts + (c.bucketSeconds || 60);
  tip.innerHTML =
    `<b>${compact(p.count)}</b> ${p.count === 1 ? "request" : "requests"}` +
    `<div class="t-sub">${axisTime(p.bucket_ts, c.bucketSeconds)} – ${clockTime(end)}</div>`;
  tip.hidden = false;

  // Position in CSS pixels: the SVG scales to the wrap, so map through it.
  const scale = $("chart").clientWidth / c.width;
  tip.style.left = Math.min(Math.max(px * scale, 60), $("chart").clientWidth - 60) + "px";
  tip.style.top = Math.max(py * scale - 10, 10) + "px";
}

function clearHover() {
  state.hoverIndex = null;
  const layer = document.querySelector("#chart-hover");
  if (layer) layer.innerHTML = "";
  $("chart-tip").hidden = true;
}

function initChartHover() {
  const wrap = $("chart-wrap");

  // Nearest-bucket lookup across the whole plot, so the hit target is the
  // full column rather than a pixel-wide line.
  wrap.addEventListener("mousemove", (e) => {
    const c = state.chart;
    if (!c || !c.pts.length) return;
    const rect = $("chart").getBoundingClientRect();
    const scale = c.width / rect.width;
    const mx = (e.clientX - rect.left) * scale;
    if (mx < PAD.left - 10 || mx > c.width - PAD.right + 10) return clearHover();
    const i = Math.round((mx - PAD.left) / (c.stepX || 1));
    const clamped = Math.max(0, Math.min(c.pts.length - 1, i));
    state.hoverIndex = clamped;
    drawHover(clamped);
  });

  wrap.addEventListener("mouseleave", clearHover);
}

// --- panels ---------------------------------------------------------------

function renderCards(cards, summary) {
  text($("card-rpm"), compact(Math.round(cards.requests_per_min)));
  text($("card-p95"), latency(cards.p95_latency_ms));
  text($("card-cache"), cards.cache_hit_pct.toFixed(1) + "%");
  text($("card-cache-note"),
       `${compact(summary.calls)} calls · ${summary.dedup_pct.toFixed(1)}% deduped`);

  const saved = cards.cost_saved;
  text($("card-saved"), usd(saved.usd));
  text($("card-saved-note"),
       `${compact(saved.basis.saved_calls)} cached/deduped calls`);
  $("cost-basis").title =
    `Estimate. Method: ${saved.method}. ` +
    `${saved.basis.priced_calls} priced from the recorded cost, ` +
    `${saved.basis.estimated_calls} from an average.`;
}

function renderProviders(rows) {
  const list = $("health-list");
  $("health-empty").hidden = rows.length > 0;
  list.innerHTML = rows.map((r) => {
    const lat = r.avg_latency_ms === null
      ? `<span class="health-lat none" title="Only cached calls in this window — never reached the provider">—</span>`
      : `<span class="health-lat">${latency(r.avg_latency_ms)}</span>`;
    return `<li class="health-row">
      <span class="dot ${r.status}" aria-hidden="true"></span>
      <span class="health-name" title="${r.calls} calls · ${r.ok_pct}% ok">${r.provider}</span>
      ${lat}
      <span class="state ${r.status}">${r.status}</span>
    </li>`;
  }).join("");
}

function renderTokens(rows) {
  const list = $("token-list");
  $("tokens-empty").hidden = rows.length > 0;
  const total = rows.reduce((a, r) => a + r.tokens, 0);
  text($("tokens-total"), total ? compact(total) + " tokens" : "");

  // Bars scale to the busiest provider so the largest fills the track; the
  // labelled number stays share-of-total, which means something on its own.
  const max = Math.max(...rows.map((r) => r.tokens), 1);
  list.innerHTML = rows.map((r) => `
    <li class="token-row">
      <span class="token-head">
        <span>${r.provider}</span>
        <span class="num">${compact(r.tokens)}<span class="share">${r.share_pct.toFixed(1)}%</span></span>
      </span>
      <span class="token-track">
        <span class="token-fill" style="width: ${(100 * r.tokens / max).toFixed(2)}%"></span>
      </span>
    </li>`).join("");
}

function renderRetries(rows) {
  const list = $("retry-list");
  $("retries-empty").hidden = rows.length > 0;
  list.innerHTML = rows.map((r) => {
    const why = r.error_type
      ? `<span class="err">${r.error_type}</span>`
      : "recovered";
    return `<li class="retry-row">
      <span class="retry-count">${r.retries}×</span>
      <span class="retry-what">${r.provider} · ${why}</span>
      <span class="retry-ago">${ago(r.ts)}</span>
    </li>`;
  }).join("");
}

function logRow(e) {
  const tr = document.createElement("tr");
  tr.className = "fresh";

  const flags = [];
  if (e.cached) flags.push(`<span class="flag saved">cached</span>`);
  if (e.deduped) flags.push(`<span class="flag saved">deduped</span>`);
  if (e.retries) flags.push(`<span class="flag retry">${e.retries}× retry</span>`);

  const source = (e.tags && e.tags.source) || null;
  const tokens = e.total_tokens || ((e.input_tokens || 0) + (e.output_tokens || 0));

  tr.innerHTML = `
    <td class="t-muted">${clockTime(e.ts)}</td>
    <td class="${e.ok ? "t-ok" : "t-fail"}">${e.ok ? "ok" : "fail"}</td>
    <td>${e.provider}</td>
    <td class="t-model">${e.model}${e.error_type ? ` <span class="t-fail">${e.error_type}</span>` : ""}</td>
    <td class="num">${latency(e.latency_ms)}</td>
    <td>${flags.join("") || `<span class="t-muted">—</span>`}</td>
    <td class="num">${tokens ? compact(tokens) : `<span class="t-muted">—</span>`}</td>
    <td class="num">${usdPrecise(e.cost_usd) || `<span class="t-muted">—</span>`}</td>
    <td class="${source === "demo" ? "src-demo" : "src-live"}">${source || "live"}</td>`;
  return tr;
}

function appendLog(events) {
  if (!events.length) return;
  const body = $("log-body");
  $("log-empty").hidden = true;

  // Rows arrive newest-first; inserting in reverse leaves the newest on top.
  for (let i = events.length - 1; i >= 0; i--) {
    body.insertBefore(logRow(events[i]), body.firstChild);
  }
  while (body.children.length > LOG_MAX_ROWS) body.lastChild.remove();

  state.lastId = Math.max(state.lastId ?? 0, ...events.map((e) => e.id));
}

// --- polling --------------------------------------------------------------

async function pollSnapshot() {
  try {
    const res = await fetch(`/api/snapshot?window=${state.window}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    renderCards(data.cards, data.summary);
    renderChart(data.timeseries);
    renderProviders(data.providers);
    renderTokens(data.tokens);
    renderRetries(data.retries || []);
    paintDemo(data.demo_running);

    document.querySelector(".grid").classList.remove("stale");
    const status = $("poll-status");
    status.className = "status-line live";
    text(status, `live · ${clockTime(data.generated_at)}`);
  } catch (err) {
    const status = $("poll-status");
    status.className = "status-line";
    text(status, `disconnected — ${err.message}`);
    document.querySelector(".grid").classList.add("stale");
  }
}

async function pollTail() {
  try {
    const q = state.lastId === null ? "n=50" : `n=200&after_id=${state.lastId}`;
    const res = await fetch(`/api/logs/tail?${q}`);
    if (!res.ok) return;
    const { events } = await res.json();
    appendLog(events);
  } catch { /* the snapshot poll already surfaces connection state */ }
}

function startPolling() {
  stopPolling();
  state.timers = [
    setInterval(pollSnapshot, SNAPSHOT_MS),
    setInterval(pollTail, TAIL_MS),
  ];
}

function stopPolling() {
  state.timers.forEach(clearInterval);
  state.timers = [];
}

// --- demo toggle ----------------------------------------------------------

function paintDemo(running) {
  const btn = $("demo-toggle");
  btn.setAttribute("aria-pressed", String(running));
  text($("demo-label"), running ? "demo on" : "demo off");
}

function initDemoToggle() {
  const btn = $("demo-toggle");
  btn.addEventListener("click", async () => {
    const turningOn = btn.getAttribute("aria-pressed") !== "true";
    btn.disabled = true;
    try {
      const res = await fetch(`/api/demo/${turningOn ? "start" : "stop"}`,
                              { method: "POST" });
      const data = await res.json();
      paintDemo(data.running);
      pollSnapshot();
    } catch { /* the next snapshot reports the true state anyway */ }
    btn.disabled = false;
  });
}

// --- playground -----------------------------------------------------------

let playgroundCatalog = [];

function fillModels() {
  const provider = $("play-provider").value;
  const entry = playgroundCatalog.find((p) => p.provider === provider);
  const select = $("play-model");
  select.innerHTML = (entry ? entry.models : [])
    .map((m) => `<option value="${m}">${m}</option>`).join("");
}

async function initPlayground() {
  const form = $("play-form");
  const providerSelect = $("play-provider");

  try {
    const res = await fetch("/api/playground/models");
    const data = await res.json();
    playgroundCatalog = data.providers || [];
  } catch { playgroundCatalog = []; }

  if (!playgroundCatalog.length) {
    // No keys configured is a normal state, not an error — demo mode is the
    // whole point of being able to run this without credentials.
    providerSelect.innerHTML = `<option value="">no API keys found</option>`;
    providerSelect.disabled = true;
    $("play-model").disabled = true;
    $("play-run").disabled = true;
    text($("play-meta"), "set a provider key in .env to enable");
    return;
  }

  providerSelect.innerHTML = playgroundCatalog
    .map((p) => `<option value="${p.provider}">${p.provider}</option>`).join("");
  providerSelect.addEventListener("change", fillModels);
  fillModels();

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const prompt = $("play-prompt").value.trim();
    if (!prompt) return;

    const out = $("play-output");
    const run = $("play-run");
    run.disabled = true;
    out.classList.remove("err");
    out.textContent = "calling…";
    text($("play-stats"), "");

    try {
      const res = await fetch("/api/playground", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          prompt,
          provider: providerSelect.value,
          model: $("play-model").value,
        }),
      });
      const data = await res.json();

      if (!res.ok) {
        out.classList.add("err");
        out.textContent = data.error || `HTTP ${res.status}`;
      } else {
        out.textContent = data.text || "(no text in response)";
        const u = data.usage || {};
        const c = data.cost || {};
        text($("play-stats"), [
          `${data.provider}/${data.upstream_model || data.model}`,
          latency(data.elapsed_ms),
          u.total_tokens ? `${compact(u.total_tokens)} tokens` : null,
          c.usd !== undefined && c.usd !== null ? usdPrecise(c.usd) : null,
        ].filter(Boolean).join("  ·  "));
      }
    } catch (err) {
      out.classList.add("err");
      out.textContent = err.message;
    }

    run.disabled = false;
    pollTail();   // show the call immediately rather than waiting for the tick
  });
}

// --- wiring ---------------------------------------------------------------

function markWindow() {
  document.querySelectorAll(".win").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.window === state.window)));
}

function initWindowPicker() {
  // The pressed state is derived from state.window rather than hard-coded in
  // the template, so there is one source of truth for the default.
  markWindow();
  document.querySelectorAll(".win").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.window = btn.dataset.window;
      markWindow();
      document.querySelector(".grid").classList.add("stale");
      clearHover();
      pollSnapshot();
    });
  });
}

// A backgrounded tab has no business generating load.
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopPolling();
    const status = $("poll-status");
    status.className = "status-line paused";
    text(status, "paused · tab hidden");
  } else {
    pollSnapshot();
    pollTail();
    startPolling();
  }
});

let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (state.chart) renderChart({
      points: state.chart.pts,
      bucket_seconds: state.chart.bucketSeconds,
    });
  }, 120);
});

initWindowPicker();
initChartHover();
initDemoToggle();
initPlayground();
pollSnapshot();
pollTail();
startPolling();
