"use strict";
// CRI Control Centre. Every figure on screen comes from the platform API;
// nothing is computed or invented client-side beyond formatting.

const S = { key: null, who: null, tenant: null, tab: "overview", account: null };
const COLORS = ["#4f8cff", "#2fb47c", "#e0a526", "#e5484d", "#b07cff", "#3cc7d6", "#ff6a3d"];

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (id) => document.getElementById(id);
const fmt = (x, d = 0) => (x === null || x === undefined || Number.isNaN(x)) ? "—" :
  Number(x).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
const pct = (x, d = 1) => (x === null || x === undefined) ? "—" : (100 * x).toFixed(d) + "%";
const ago = (iso) => { if (!iso) return "—"; const s = (Date.now() - new Date(iso).getTime()) / 1000;
  return s < 90 ? `${Math.round(s)}s ago` : s < 5400 ? `${Math.round(s / 60)}m ago` : s < 172800 ? `${Math.round(s / 3600)}h ago` : `${Math.round(s / 86400)}d ago`; };
const pill = (v) => h("span", { class: `pill s-${String(v || "").toLowerCase()}` }, v ?? "—");
const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random());

async function api(path, opts = {}) {
  const headers = { Authorization: `Bearer ${S.key}` };
  if (S.tenant && S.who && S.who.role === "admin" && !path.startsWith("/admin")) headers["X-CRI-Tenant"] = S.tenant;
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (opts.method && opts.method !== "GET") headers["Idempotency-Key"] = opts.idem || uuid();
  const r = await fetch(path, { method: opts.method || "GET", headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  const text = await r.text();
  let data; try { data = JSON.parse(text); } catch { data = text; }
  if (!r.ok) throw new Error((data && data.detail) ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `HTTP ${r.status}`);
  return data;
}

function table(cols, rows, onClick) {
  return h("table", {},
    h("thead", {}, h("tr", {}, cols.map((c) => h("th", { class: c.n ? "n" : "" }, c.t)))),
    h("tbody", {}, rows.map((r) => h("tr", { class: onClick ? "click" : "", onclick: onClick ? () => onClick(r) : null },
      cols.map((c) => h("td", { class: c.n ? "n" : "" }, c.f ? c.f(r) : r[c.k]))))));
}
const card = (...kids) => h("div", { class: "card" }, ...kids);
const kpi = (label, value) => h("div", { class: "card kpi" }, h("div", { class: "v" }, value), h("div", { class: "l" }, label));
function errorBox(e) { return h("div", { class: "notice" }, `Could not load: ${e.message}`); }

function lineChart(series, { yfmt = (v) => fmt(v), title = "" } = {}) {
  // series: [{name, points:[{x: Date|string, y: number}]}]
  const W = 900, H = 220, P = { l: 70, r: 12, t: 16, b: 26 };
  const all = series.flatMap((s) => s.points.filter((p) => p.y !== null && p.y !== undefined && isFinite(p.y)));
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "chart");
  if (!all.length) return h("div", { class: "muted" }, "No data yet.");
  const xs = all.map((p) => new Date(p.x).getTime()), ys = all.map((p) => p.y);
  let [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  if (x0 === x1) x1 = x0 + 1; if (y0 === y1) { y0 -= 1; y1 += 1; }
  const X = (t) => P.l + (t - x0) / (x1 - x0) * (W - P.l - P.r), Y = (v) => H - P.b - (v - y0) / (y1 - y0) * (H - P.t - P.b);
  const NS = "http://www.w3.org/2000/svg";
  for (let i = 0; i <= 4; i++) {
    const v = y0 + (y1 - y0) * i / 4, y = Y(v);
    const ln = document.createElementNS(NS, "line"); ln.setAttribute("x1", P.l); ln.setAttribute("x2", W - P.r);
    ln.setAttribute("y1", y); ln.setAttribute("y2", y); ln.setAttribute("stroke", "#2a313b"); svg.append(ln);
    const tx = document.createElementNS(NS, "text"); tx.setAttribute("x", P.l - 6); tx.setAttribute("y", y + 4);
    tx.setAttribute("text-anchor", "end"); tx.setAttribute("fill", "#8b95a3"); tx.setAttribute("font-size", "11"); tx.textContent = yfmt(v); svg.append(tx);
  }
  [x0, x1].forEach((t, i) => { const tx = document.createElementNS(NS, "text"); tx.setAttribute("x", i ? W - P.r : P.l);
    tx.setAttribute("y", H - 6); tx.setAttribute("text-anchor", i ? "end" : "start"); tx.setAttribute("fill", "#8b95a3");
    tx.setAttribute("font-size", "11"); tx.textContent = new Date(t).toISOString().slice(0, 10); svg.append(tx); });
  series.forEach((s, i) => {
    const pts = s.points.filter((p) => p.y !== null && p.y !== undefined && isFinite(p.y))
      .sort((a, b) => new Date(a.x) - new Date(b.x));
    if (!pts.length) return;
    const pl = document.createElementNS(NS, "polyline");
    pl.setAttribute("points", pts.map((p) => `${X(new Date(p.x).getTime())},${Y(p.y)}`).join(" "));
    pl.setAttribute("fill", "none"); pl.setAttribute("stroke", COLORS[i % COLORS.length]); pl.setAttribute("stroke-width", "2");
    svg.append(pl);
  });
  return h("div", {}, title ? h("div", { class: "small muted" }, title) : null, svg,
    h("div", { class: "legend" }, series.map((s, i) => h("span", {}, h("i", { style: `background:${COLORS[i % COLORS.length]}` }), s.name))));
}

// ---------------------------------------------------------------- tabs
const TABS = [
  ["overview", "Portfolios", renderOverview],
  ["account", "Account analysis", renderAccount],
  ["alerts", "Margin calls & advisories", renderAlerts],
  ["market", "Market data", renderMarket],
  ["models", "Model comparison", renderModels],
  ["backtests", "Backtests", renderBacktests],
  ["policies", "Policies", renderPolicies],
  ["admin", "Tenants & access", renderAdmin, true],
  ["audit", "Audit trail", renderAudit],
  ["infra", "Infrastructure", renderInfra, true],
];

function renderNav() {
  const nav = $("nav"); nav.replaceChildren();
  for (const [id, label, , adminOnly] of TABS) {
    if (adminOnly && S.who.role !== "admin") continue;
    nav.append(h("button", { class: S.tab === id ? "active" : "", onclick: () => go(id) }, label));
  }
}
async function go(tab, account) {
  S.tab = tab; if (account) S.account = account; renderNav();
  const main = $("main"); main.replaceChildren(h("div", { class: "muted" }, "Loading…"));
  const fn = TABS.find((t) => t[0] === tab)[2];
  try { main.replaceChildren(await fn()); } catch (e) { main.replaceChildren(errorBox(e)); }
}

async function renderOverview() {
  const [accts, controls] = await Promise.all([api("/monitoring/accounts"), api("/platform/controls")]);
  const list = accts.accounts || [];
  const byState = {}; list.forEach((a) => { byState[a.last_margin_state || "unknown"] = (byState[a.last_margin_state || "unknown"] || 0) + 1; });
  const rows = await Promise.all(list.map(async (a) => {
    try { const full = await api(`/monitoring/accounts/${encodeURIComponent(a.account_ref)}`); return full.account || full; }
    catch { return a; }
  }));
  return h("div", {},
    h("h2", {}, `Portfolios — tenant ${S.tenant || S.who.tenant_id}`),
    h("div", { class: "grid" }, kpi("Monitored accounts", list.length),
      ...Object.entries(byState).map(([k, v]) => kpi(`State: ${k}`, v)),
      kpi("Lending", controls.controls.lending_enabled ? "ENABLED" : "disabled"),
      kpi("Liquidation execution", controls.controls.liquidation_execution_enabled ? "ENABLED" : "advisory only")),
    h("h3", {}, "Accounts"),
    table([
      { t: "Account", f: (r) => r.account_ref },
      { t: "State", f: (r) => pill(r.last_margin_state) },
      { t: "Collateral value", n: 1, f: (r) => fmt(r.last_evaluation?.portfolio_market_value) },
      { t: "Loan balance", n: 1, f: (r) => `${fmt(r.loan?.principal + (r.loan?.accrued_interest || 0) + (r.loan?.fees || 0), 2)} ${r.loan_currency || ""}` },
      { t: "Approved limit", n: 1, f: (r) => fmt(r.last_evaluation?.approved_credit_limit) },
      { t: "Available credit", n: 1, f: (r) => fmt(r.last_available_credit, 2) },
      { t: "LTV", n: 1, f: (r) => { const e = r.last_evaluation; return e && e.portfolio_market_value ? pct(e.current_outstanding_balance / e.portfolio_market_value) : "—"; } },
      { t: "Last checked", f: (r) => ago(r.last_checked_at) },
    ], rows, (r) => go("account", r.account_ref)));
}

async function renderAccount() {
  const list = (await api("/monitoring/accounts")).accounts || [];
  if (!S.account && list.length) S.account = list[0].account_ref;
  const pick = h("select", { onchange: (e) => go("account", e.target.value) },
    list.map((a) => h("option", { value: a.account_ref, selected: a.account_ref === S.account }, a.account_ref)));
  if (!S.account) return h("div", {}, h("h2", {}, "Account analysis"), h("p", { class: "muted" }, "No accounts."));
  const ref = encodeURIComponent(S.account);
  const [ex, sh, ev, hist] = await Promise.all([
    api(`/explain/accounts/${ref}`), api(`/shadow/accounts/${ref}`),
    api(`/monitoring/events?account_ref=${ref}&limit=30`), api(`/shadow/history/${ref}?limit=2000`)]);
  const x = ex.explanation;
  const histBy = {};
  (hist.history || []).forEach((r) => { if (!r.error) (histBy[r.model_id] = histBy[r.model_id] || []).push({ x: r.evaluated_at, y: r.capacity_ltv }); });
  return h("div", {},
    h("div", { class: "row" }, h("h2", {}, "Account analysis"), pick, h("button", { class: "ghost", onclick: () => go("account") }, "Refresh")),
    h("div", { class: "grid" },
      kpi("Risk state (live)", pill(ex.risk_state)), kpi("Collateral value", fmt(ex.collateral_value, 2)),
      kpi("Loan balance", fmt(ex.loan_balance, 2)), kpi("Approved credit limit", fmt(ex.approved_credit, 2)),
      kpi("Available credit", fmt(ex.available_credit, 2)), kpi("Stressed recovery value", fmt(ex.estimated_recovery, 2)),
      kpi("Current / approved LTV", `${pct(x.current_ltv)} / ${pct(x.approved_ltv)}`),
      kpi("Recovery coverage", x.recovery_coverage_ratio === null ? "—" : `${fmt(x.recovery_coverage_ratio, 2)}×`)),
    h("h3", {}, `Why this limit and state (${ex.model_version})`),
    card(h("ul", { class: "reasons" }, (x.reasons || []).map((r) => h("li", {}, r))),
      h("div", { class: "small muted" }, ex.note)),
    h("h3", {}, "Asset risk and its drivers"),
    table([
      { t: "Asset", f: (r) => r.asset_id }, { t: "Value", n: 1, f: (r) => fmt(r.market_value, 2) },
      { t: "Base LTV", n: 1, f: (r) => pct(r.base_ltv, 0) }, { t: "Effective LTV", n: 1, f: (r) => pct(r.effective_ltv, 1) },
      { t: "Credit reduction", n: 1, f: (r) => fmt(r.credit_reduction_vs_base, 2) },
      { t: "Liquidation days", n: 1, f: (r) => fmt(r.liquidation_horizon_days, 1) },
      { t: "Slippage", n: 1, f: (r) => pct(r.estimated_slippage_rate, 2) },
      { t: "Drivers", f: (r) => [r.eligible ? "" : h("span", { class: "pill s-bad" }, `ineligible${r.data_unavailable_reason ? ": " + r.data_unavailable_reason : ""}`), (r.risk_drivers || []).join("; ")] },
      { t: "Adjustments", f: (r) => h("span", { class: "mono" }, Object.entries(r.adjustments || {}).filter(([, v]) => typeof v === "number" && Math.abs(v) > 1e-9).map(([k, v]) => `${k}=${Number(v).toFixed(4)}`).join("  ")) },
    ], x.assets || []),
    ex.proposed_liquidation && ex.proposed_liquidation.length ? h("div", {},
      h("h3", {}, "Liquidation advisory (not executed)"),
      table([{ t: "Instrument", k: "instrument" }, { t: "Quantity", n: 1, f: (r) => fmt(r.quantity, 2) },
        { t: "Expected net proceeds", n: 1, f: (r) => fmt(r.expected_net_proceeds, 2) }], ex.proposed_liquidation)) : null,
    h("h3", {}, "Shadow models on this account (latest run)"),
    table([
      { t: "Model", f: (r) => `${r.model_id}${r.role === "baseline" ? " (baseline)" : ""}` },
      { t: "Version", f: (r) => h("span", { class: "small mono" }, r.model_version) },
      { t: "State", f: (r) => r.error ? h("span", { class: "pill s-bad", title: r.error }, "error") : pill(r.risk_state) },
      { t: "Capacity LTV", n: 1, f: (r) => pct(r.capacity_ltv) }, { t: "Approved credit", n: 1, f: (r) => fmt(r.approved_credit, 0) },
      { t: "Est. recovery", n: 1, f: (r) => fmt(r.estimated_recovery, 0) },
      { t: "Proposed sale qty", n: 1, f: (r) => r.liquidation && r.liquidation.length ? r.liquidation.map((o) => `${fmt(o.quantity, 1)} ${String(o.instrument).split(":")[1] || o.instrument}`).join(", ") : "—" },
      { t: "Why", f: (r) => h("span", { class: "small" }, r.error || ((r.explanation && r.explanation.reasons) || []).slice(0, 2).join(" · ")) },
    ], sh.evaluations || []),
    (sh.disagreements || []).length ? card(h("div", { class: "small" }, h("b", {}, "Disagreements with baseline: "),
      sh.disagreements.map((d) => h("div", {}, `${d.model_id}: ${d.differences.join("; ")}`)))) : null,
    h("h3", {}, "Capacity LTV over time by model"),
    lineChart(Object.entries(histBy).map(([name, points]) => ({ name, points })), { yfmt: (v) => pct(v, 0) }),
    h("h3", {}, "Recent events"),
    table([{ t: "Time", f: (r) => ago(r.created_at) }, { t: "Type", f: (r) => r.event_type }, { t: "Severity", f: (r) => pill(r.severity) },
      { t: "Detail", f: (r) => h("span", { class: "small" }, r.reason || "") }], ev.events || []));
}

async function renderAlerts() {
  const [mc, lq, dl, rc] = await Promise.all([
    api("/monitoring/events?event_type=margin_call_triggered&limit=100"),
    api("/monitoring/events?event_type=liquidation_triggered&limit=100"),
    api("/webhooks/deliveries?limit=100"), api("/sandbox/webhook-receipts?limit=100").catch(() => ({ receipts: [] }))]);
  const evs = [...(mc.events || []), ...(lq.events || [])].sort((a, b) => b.created_at.localeCompare(a.created_at));
  const st = {}; (dl.deliveries || []).forEach((d) => { st[d.status] = (st[d.status] || 0) + 1; });
  return h("div", {}, h("h2", {}, "Margin calls and liquidation advisories"),
    h("div", { class: "notice" }, "Liquidation is advisory only on this platform: webhooks carry advisory_only=true and execution_authorised=false; CRI places no orders."),
    h("div", { class: "grid" }, kpi("Margin-call events", (mc.events || []).length), kpi("Liquidation advisories", (lq.events || []).length),
      ...Object.entries(st).map(([k, v]) => kpi(`Webhooks ${k}`, v)), kpi("Receipts with valid signature", (rc.receipts || []).filter((r) => r.signature_valid).length)),
    h("h3", {}, "Events"),
    table([{ t: "Time", f: (r) => new Date(r.created_at).toISOString().replace("T", " ").slice(0, 19) }, { t: "Account", f: (r) => r.account_ref },
      { t: "Type", f: (r) => r.event_type }, { t: "Severity", f: (r) => pill(r.severity) },
      { t: "Detail", f: (r) => h("span", { class: "small" }, `${r.previous_margin_state || "—"} → ${r.new_margin_state || "—"}. ${r.reason || ""}`) }], evs,
      (r) => go("account", r.account_ref)),
    h("h3", {}, "Webhook deliveries"),
    table([{ t: "Created", f: (r) => ago(r.created_at) }, { t: "Event", f: (r) => h("span", { class: "mono" }, r.event_id) },
      { t: "Status", f: (r) => pill(r.status) }, { t: "Attempts", n: 1, k: "attempts" }, { t: "HTTP", n: 1, k: "last_status_code" },
      { t: "Error", f: (r) => r.last_error || "" }], dl.deliveries || []));
}

async function renderMarket() {
  const m = await api("/platform/market-data/status");
  let onb = null;
  if (S.who.role === "admin") { try { onb = await api("/admin/markets/onboarding"); } catch { onb = null; } }
  const stale = (m.quotes || []).filter((q) => q.age_hours > 30).length;
  return h("div", {}, h("h2", {}, "Market data freshness, coverage and provider health"),
    h("div", { class: "grid" }, kpi("Instruments", (m.quotes || []).length), kpi("Older than 30h", stale),
      kpi("Ingestion runs (last 48)", m.runs_last_48), kpi("Last run", ago(m.ingestion_runs?.[0]?.finished_at))),
    h("h3", {}, "Latest quotes"),
    table([{ t: "Instrument", f: (r) => r.stable_key }, { t: "Provider", k: "provider" }, { t: "Price", n: 1, f: (r) => fmt(r.price, 2) },
      { t: "Observed", f: (r) => r.observed_at?.slice(0, 19) }, { t: "Age (h)", n: 1, f: (r) => fmt(r.age_hours, 1) },
      { t: "Quality", n: 1, f: (r) => fmt(r.quality, 2) }, { t: "Vol 30d", n: 1, f: (r) => pct(r.volatility_30d) },
      { t: "ADV", n: 1, f: (r) => fmt(r.average_daily_volume) }, { t: "Warnings", f: (r) => h("span", { class: "small" }, (r.warnings || []).join(", ")) }], m.quotes || []),
    h("h3", {}, "Stored history (daily bars)"),
    table([{ t: "Instrument", k: "stable_key" }, { t: "Sessions", n: 1, k: "sessions" }, { t: "First", k: "first_bar" }, { t: "Last", k: "last_bar" },
      { t: "High/low reported", n: 1, f: (r) => `${fmt(r.ranges_reported)} / ${fmt(r.sessions)}` },
      { t: "Volume reported", n: 1, f: (r) => `${fmt(r.volumes_reported)} / ${fmt(r.sessions)}` }], m.coverage || []),
    onb ? h("div", {}, h("h3", {}, "Market onboarding validation (universal estimators; no market-specific model)"),
      ...Object.values(onb.markets || {}).map((mk) => card(
        h("div", { class: "row" }, h("b", {}, mk.market), pill(mk.decision === "usable" ? "ok" : mk.decision === "blocked" ? "bad" : "warning"),
          h("span", { class: "small muted" }, `${mk.decision} · ${mk.instruments_with_history}/${mk.instruments} instruments with history`)),
        table([{ t: "Check", k: "check" }, { t: "Result", f: (r) => pill(r.result === "recorded_only" ? "ok" : "warning") }, { t: "Detail", f: (r) => h("span", { class: "small" }, r.detail) }], mk.findings || [])))) : null,
    h("h3", {}, "Ingestion runs"),
    table([{ t: "Started", f: (r) => String(r.started_at).slice(0, 19) }, { t: "Instruments", n: 1, k: "instruments" }, { t: "Quotes", n: 1, k: "quotes_written" },
      { t: "Errors", f: (r) => { const e = JSON.parse(r.errors || "[]"); return e.length ? h("span", { class: "small" }, e.map((x) => `${x.instrument}: ${x.error}`).join("; ")) : pill("ok"); } }], m.ingestion_runs || []));
}

async function renderModels() {
  const [models, sum, dis] = await Promise.all([api("/shadow/models"), api("/shadow/summary?hours=168"), api("/shadow/disagreements")]);
  let findings = null;
  if (S.who.role === "admin") { try { findings = await api("/admin/research/findings"); } catch { findings = null; } }
  return h("div", {}, h("h2", {}, "Side-by-side model comparison (shadow mode)"),
    h("div", { class: "notice" }, `Baseline drives monitoring; candidates run in shadow and ${models.lending_effect}.`),
    table([{ t: "Model", f: (r) => r.model_id }, { t: "Description", f: (r) => h("span", { class: "small" }, models.descriptions[r.model_id] || "") }],
      models.models.map((m) => ({ model_id: m }))),
    h("h3", {}, `Last 7 days (${sum.runs} shadow runs)`),
    table([{ t: "Model", f: (r) => `${r.model_id}${r.role === "baseline" ? " (baseline)" : ""}` },
      { t: "Version", f: (r) => h("span", { class: "small mono" }, r.model_version) },
      { t: "Evaluations", n: 1, k: "evaluations" }, { t: "Errors", n: 1, k: "errors" },
      { t: "Margin-call frequency", n: 1, f: (r) => pct(r.margin_call_frequency) },
      { t: "Avg capacity LTV", n: 1, f: (r) => pct(r.avg_capacity_ltv) },
      { t: "Avg recovery / collateral", n: 1, f: (r) => pct(r.avg_recovery_ratio) },
      { t: "Accounts disagreeing now", n: 1, k: "accounts_disagreeing_with_baseline_now" }], sum.models || []),
    h("h3", {}, "Current disagreements and their explanations"),
    table([{ t: "Account", k: "account_ref" }, { t: "Model", k: "model_id" },
      { t: "Differences", f: (r) => r.differences.join("; ") },
      { t: "Candidate says", f: (r) => h("span", { class: "small" }, (r.candidate_reasons || []).join(" · ")) },
      { t: "Baseline says", f: (r) => h("span", { class: "small" }, (r.baseline_reasons || []).slice(0, 2).join(" · ")) }],
      dis.disagreements || [], (r) => go("account", r.account_ref)),
    findings ? h("div", {}, h("h3", {}, "Historical research findings (repository: REAL_DATA_FINDINGS.md)"), h("pre", { class: "md" }, findings)) : null);
}

async function renderBacktests() {
  const [accts, list, models] = await Promise.all([api("/monitoring/accounts"), api("/backtests"), api("/shadow/models")]);
  const acct = h("select", {}, (accts.accounts || []).map((a) => h("option", { value: a.account_ref }, a.account_ref)));
  const boxes = models.models.map((m) => h("label", { class: "small" }, h("input", { type: "checkbox", value: m, checked: m !== "C_balanced" }), ` ${m}`));
  const step = h("input", { type: "number", min: 1, max: 21, value: 5, style: "width:70px" });
  const msg = h("span", { class: "small muted" });
  const start = async () => {
    msg.textContent = "Submitting…";
    try {
      const r = await api("/backtests", { method: "POST", body: { account_ref: acct.value, step: Number(step.value),
        models: boxes.map((b) => b.querySelector("input")).filter((i) => i.checked).map((i) => i.value) } });
      msg.textContent = `Queued ${r.run_id}${r.execution ? " (Cloud Run job started)" : ""}`; setTimeout(() => go("backtests"), 1500);
    } catch (e) { msg.textContent = e.message; }
  };
  const detail = h("div");
  const show = async (r) => {
    const b = await api(`/backtests/${r.run_id}`);
    const series = b.series || [];
    const ms = Object.keys(b.summary?.models || {});
    detail.replaceChildren(h("h3", {}, `Backtest ${b.run_id} — ${b.account_ref}`),
      b.error ? h("div", { class: "notice" }, b.error) : null,
      b.summary ? table([{ t: "Model", k: "m" }, { t: "Evaluations", n: 1, k: "evaluations" },
        { t: "Margin-call share", n: 1, f: (x) => pct(x.margin_call_share) }, { t: "First margin call", f: (x) => x.first_margin_call || "—" },
        { t: "State changes", n: 1, k: "state_changes" }, { t: "Mean capacity LTV", n: 1, f: (x) => pct(x.mean_capacity_ltv) },
        { t: "Min available credit", n: 1, f: (x) => fmt(x.min_available_credit) }],
        ms.map((m) => ({ m, ...b.summary.models[m] }))) : null,
      b.summary ? h("div", { class: "small muted" }, `Window ${b.summary.window?.join(" → ")}, every ${b.summary.step} sessions, ${b.summary.sessions} stored sessions.`) : null,
      lineChart(ms.map((m) => ({ name: m, points: series.map((p) => ({ x: p.date, y: p[m]?.capacity_ltv })) })), { yfmt: (v) => pct(v, 0), title: "Capacity LTV by model" }),
      lineChart([{ name: "LTV (loan / collateral)", points: series.map((p) => { const q = p[ms[0]]; return { x: p.date, y: q && q.collateral_value ? q.loan / q.collateral_value : null }; }) }],
        { yfmt: (v) => pct(v, 0), title: "Portfolio LTV over the window" }));
  };
  return h("div", {}, h("h2", {}, "Historical backtesting"),
    card(h("div", { class: "row" }, "Account", acct, "Every", step, "sessions", ...boxes, h("button", { onclick: start }, "Run backtest"), msg),
      h("div", { class: "small muted" }, "Replays stored daily bars; each decision uses only bars up to its date. Runs as the cri-backtest Cloud Run job.")),
    h("h3", {}, "Runs"),
    table([{ t: "Created", f: (r) => ago(r.created_at) }, { t: "Account", k: "account_ref" }, { t: "Models", f: (r) => r.models.join(", ") },
      { t: "Status", f: (r) => pill(r.status) }, { t: "Requested by", f: (r) => h("span", { class: "small mono" }, r.requested_by) },
      { t: "Error", f: (r) => r.error ? h("span", { class: "small" }, r.error.slice(0, 120)) : "" }], list.backtests || [], show),
    detail);
}

async function renderPolicies() {
  const p = await api("/policies");
  const id = h("input", { placeholder: "policy id", value: "standard" });
  const body = h("textarea", {}, JSON.stringify({ base_ltv: { listed_equity: 0.5, etf: 0.6 }, max_participation_rate: 0.1 }, null, 2));
  const msg = h("span", { class: "small muted" });
  const create = async () => { try { const b = JSON.parse(body.value);
    const r = await api("/policies", { method: "POST", body: { policy_id: id.value, body: b } });
    msg.textContent = `Created ${r.policy_id} v${r.version} (draft)`; setTimeout(() => go("policies"), 800); } catch (e) { msg.textContent = e.message; } };
  const approve = async (r) => {
    if (S.who.role !== "admin") return;
    const reason = window.prompt(`Approve ${r.policy_id} v${r.version}? Reason (min 10 characters):`); if (!reason) return;
    try { await api(`/admin/policies/${encodeURIComponent(S.tenant || S.who.tenant_id)}/${encodeURIComponent(r.policy_id)}/${r.version}/approve`, { method: "POST", body: { value: true, reason } }); go("policies"); }
    catch (e) { window.alert(e.message); }
  };
  return h("div", {}, h("h2", {}, "Lending policy versions"),
    h("div", { class: "notice" }, "Policies are client ceilings and controls. Versions are immutable; approval requires a Tiva administrator who did not author the version."),
    table([{ t: "Policy", k: "policy_id" }, { t: "Version", n: 1, k: "version" }, { t: "Status", f: (r) => pill(r.status) },
      { t: "Created by", f: (r) => h("span", { class: "small mono" }, r.created_by) }, { t: "Approved by", f: (r) => r.approved_by || "—" },
      { t: "Body", f: (r) => h("span", { class: "mono" }, JSON.stringify(r.body)) },
      { t: "", f: (r) => S.who.role === "admin" && r.status === "draft" ? h("button", { class: "ghost", onclick: (e) => { e.stopPropagation(); approve(r); } }, "Approve…") : "" }],
      p.policies || []),
    h("h3", {}, "New version"), card(h("div", { class: "row" }, id, h("button", { onclick: create }, "Create draft version"), msg), body));
}

async function renderAdmin() {
  const [t, c] = await Promise.all([api("/admin/tenants"), api("/platform/controls")]);
  const out = h("div", { class: "small mono" });
  const tid = h("input", { placeholder: "tenant-id" }), tname = h("input", { placeholder: "Name" });
  const kind = h("select", {}, h("option", { value: "sandbox" }, "sandbox"), h("option", { value: "client" }, "client"));
  const create = async () => { try { const r = await api("/admin/tenants", { method: "POST", body: { tenant_id: tid.value, name: tname.value, kind: kind.value } });
    out.textContent = `Created ${r.tenant_id}. Initial client key (shown once): ${r.api_key}`; } catch (e) { out.textContent = e.message; } };
  const issue = async (r) => { const role = window.prompt("Role for new key (client | readonly | admin):", "readonly"); if (!role) return;
    try { const k = await api(`/admin/tenants/${encodeURIComponent(r.tenant_id)}/keys`, { method: "POST", body: { role, label: "control-centre" } });
      out.textContent = `New ${k.role} key for ${r.tenant_id} (shown once): ${k.api_key}  [${k.key_id}]`; } catch (e) { out.textContent = e.message; } };
  const revokeId = h("input", { placeholder: "key_..." });
  const revoke = async () => { try { await api(`/admin/keys/${encodeURIComponent(revokeId.value)}`, { method: "DELETE" }); out.textContent = `Revoked ${revokeId.value}`; } catch (e) { out.textContent = e.message; } };
  const toggle = async (key, value) => { const reason = window.prompt(`Set ${key} = ${value}. Reason (min 10 characters):`); if (!reason) return;
    try { await api(`/admin/controls/${key}`, { method: "PUT", body: { value, reason } }); go("admin"); } catch (e) { window.alert(e.message); } };
  return h("div", {}, h("h2", {}, "Tenants, access and controls"),
    h("h3", {}, "Platform controls"),
    table([{ t: "Control", k: "k" }, { t: "Effective", f: (r) => pill(String(r.v)) },
      { t: "", f: (r) => typeof r.v === "boolean" ? h("button", { class: "ghost", onclick: () => toggle(r.k, !r.v) }, r.v ? "Turn off…" : "Turn on…") : "" }],
      Object.entries(c.controls).map(([k, v]) => ({ k, v }))),
    h("div", { class: "small muted" }, `Deployment permits: lending=${c.deployment_permits.lending}, liquidation execution=${c.deployment_permits.liquidation_execution}. Enabling either control needs the permit, which only an approved infrastructure change can set.`),
    h("h3", {}, "Tenants"),
    table([{ t: "Tenant", k: "tenant_id" }, { t: "Name", k: "name" }, { t: "Kind", k: "kind" }, { t: "Status", f: (r) => pill(r.status === "active" ? "ok" : "bad") },
      { t: "Created", f: (r) => String(r.created_at).slice(0, 10) }, { t: "", f: (r) => h("button", { class: "ghost", onclick: () => issue(r) }, "Issue key…") }], t.tenants || []),
    h("h3", {}, "Create tenant"), card(h("div", { class: "row" }, tid, tname, kind, h("button", { onclick: create }, "Create")),
      h("div", { class: "row" }, revokeId, h("button", { class: "ghost", onclick: revoke }, "Revoke key")), out));
}

async function renderAudit() {
  const admin = S.who.role === "admin";
  const [tenantAudit, verify, opAudit, opVerify] = await Promise.all([
    api("/platform/audit?limit=100"), api("/platform/audit/verify"),
    admin ? api("/admin/audit?tenant_id=*&limit=100") : Promise.resolve(null),
    admin ? api("/admin/audit/verify?tenant_id=*") : Promise.resolve(null)]);
  const rows = (x) => table([{ t: "Time", f: (r) => String(r.created_at).slice(0, 19) }, { t: "Actor", f: (r) => h("span", { class: "small mono" }, r.actor) },
    { t: "Action", k: "action" }, { t: "Payload", f: (r) => h("span", { class: "mono" }, JSON.stringify(r.payload).slice(0, 220)) },
    { t: "Hash", f: (r) => h("span", { class: "mono small" }, String(r.record_hash).slice(0, 12)) }], x.records || []);
  return h("div", {}, h("h2", {}, "Audit trail"),
    h("div", { class: "grid" }, kpi(`Tenant chain (${verify.records} records)`, verify.valid ? "valid" : "BROKEN"),
      opVerify ? kpi(`Operator chain (${opVerify.records} records)`, opVerify.valid ? "valid" : "BROKEN") : null),
    h("h3", {}, "Tenant records"), rows(tenantAudit), opAudit ? h("div", {}, h("h3", {}, "Operator records"), rows(opAudit)) : null);
}

async function renderInfra() {
  const [inf, ops] = await Promise.all([api("/admin/infrastructure"), api("/admin/operations")]);
  if (!inf.available) return h("div", {}, h("h2", {}, "Infrastructure"), h("p", {}, inf.reason));
  return h("div", {}, h("h2", {}, `Infrastructure — ${inf.project} (${inf.region})`),
    h("div", { class: "grid" }, kpi("Open alerts", (inf.open_alerts || []).length), kpi("Alert policies", (inf.alert_policies || []).length),
      kpi("Webhook backlog", (ops.webhook_backlog || []).filter((b) => b.status === "pending").reduce((a, b) => a + b.n, 0))),
    ["services_error", "jobs_error", "schedules_error", "monitoring_error"].filter((k) => inf[k]).map((k) => h("div", { class: "notice" }, `${k}: ${inf[k]}`)),
    h("h3", {}, "Cloud Run services"),
    table([{ t: "Service", k: "name" }, { t: "Serving", f: (r) => r.traffic.map((t) => `${t.revision} ${t.percent ?? ""}%${t.tag ? " [" + t.tag + "]" : ""}`).join(", ") },
      { t: "Conditions", f: (r) => r.conditions.map((c) => `${c.type}:${c.state}`).join(" ") }, { t: "Updated", f: (r) => ago(r.update_time) }], inf.services || []),
    h("h3", {}, "Scheduled operations"),
    table([{ t: "Job", k: "name" }, { t: "Schedule", k: "schedule" }, { t: "State", k: "state" }, { t: "Last attempt", f: (r) => ago(r.last_attempt) },
      { t: "Last status", f: (r) => pill(r.status_code ? "failed" : "ok") }], inf.schedules || []),
    h("h3", {}, "Cloud Run jobs"),
    table([{ t: "Job", k: "name" }, { t: "Latest execution", f: (r) => h("span", { class: "mono small" }, r.latest_execution) },
      { t: "Completed", f: (r) => ago(r.latest_completion) }, { t: "Status", f: (r) => r.latest_status || "—" }], inf.jobs || []),
    h("h3", {}, "Alert policies"),
    table([{ t: "Policy", k: "name" }, { t: "Enabled", f: (r) => pill(r.enabled ? "ok" : "bad") }], inf.alert_policies || []),
    (inf.open_alerts || []).length ? h("div", {}, h("h3", {}, "Open alerts"), table([{ t: "Policy", k: "policy" }, { t: "Opened", f: (r) => ago(r.opened) }], inf.open_alerts)) : null);
}

// ---------------------------------------------------------------- session
async function start() {
  S.who = await api("/platform/whoami");
  $("login").hidden = true; $("shell").hidden = false;
  $("env").textContent = S.who.environment;
  $("who").textContent = `${S.who.role} · ${S.who.tenant_id}`;
  const c = await api("/platform/controls");
  $("safety").textContent = (!c.controls.lending_enabled && !c.controls.liquidation_execution_enabled)
    ? "Lending disabled · liquidation advisory only" : "Warning: lending or execution enabled";
  if (S.who.role === "admin") {
    const t = (await api("/admin/tenants")).tenants || [];
    const sel = $("tenant-select"); sel.hidden = false;
    sel.replaceChildren(...t.filter((x) => x.kind !== "internal").map((x) => h("option", { value: x.tenant_id }, x.tenant_id)));
    S.tenant = sessionStorage.getItem("cri_tenant") || (t.find((x) => x.kind === "sandbox") || t[0] || {}).tenant_id;
    sel.value = S.tenant; sel.onchange = () => { S.tenant = sel.value; sessionStorage.setItem("cri_tenant", S.tenant); S.account = null; go(S.tab); };
  }
  go("overview");
}

document.addEventListener("DOMContentLoaded", () => {
  $("login-form").addEventListener("submit", async (e) => {
    e.preventDefault(); S.key = $("login-key").value.trim(); $("login-error").textContent = "";
    try { await start(); sessionStorage.setItem("cri_key", S.key); } catch (err) { $("login-error").textContent = err.message; S.key = null; }
  });
  $("logout").addEventListener("click", () => { sessionStorage.clear(); location.reload(); });
  const saved = sessionStorage.getItem("cri_key");
  if (saved) { S.key = saved; start().catch(() => { sessionStorage.clear(); }); }
});
