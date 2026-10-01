// Phase 4F.3 MVP-hardening probe. Runs with plain Node inside the n8n container (same network path as the Agent):
//   <token on stdin> | node -e "$(cat mvp_probe.js)" http://samsung-consultant:8765
// Checks the three hardening contracts over the real MCP boundary and the real catalog:
//   MVP-1  a feature that was asked about is reported per product as yes / no / not_listed;
//   MVP-2  an invented number after a slang budget is removed, a stated one is kept;
//   MVP-3  feature counts, series counts and the counted scope of get_catalog_stats.
// The token is read from stdin only and never printed. Prints statuses, states and catalog counts, not payloads.
const BASE = process.argv.find((a) => a && a.startsWith("http"));
const RUN = `m${Date.now()}`;

async function readStdin() {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return data.trim();
}

async function post(body, headers, query) {
  const r = await fetch(`${BASE}/mcp?${query}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...headers },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  return { status: r.status, session: r.headers.get("mcp-session-id"), json: text ? JSON.parse(text) : null };
}

let turns = 0;
async function call(auth, tool, args, conv, q, h = 0) {
  let query = `turn=${RUN}-${++turns}`;
  if (conv) query += `&h=${h}&conv=${encodeURIComponent(conv)}`;
  if (q) query += `&q=${encodeURIComponent(q)}`;
  const init = await post({ jsonrpc: "2.0", id: 1, method: "initialize",
    params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "4f3-mvp-probe", version: "1" } } }, auth, query);
  const s = { ...auth, "Mcp-Session-Id": init.session };
  const res = await post({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: tool, arguments: args } }, s, query);
  return JSON.parse(res.json.result.content[0].text);
}

const state = (v) => (v === undefined ? "absent" : typeof v === "string" ? v : v.state);
const distinct = (payload, feature) => [...new Set((payload.products || []).map((p) => state((p.features || {})[feature])))];
const notApplied = (payload) => ((payload.request || {}).not_applied || {}).constraints || null;

(async () => {
  const token = await readStdin();
  const auth = { Authorization: `Bearer ${token}` };
  const out = {};

  // MVP-1
  const ps5 = await call(auth, "recommend_tvs", { use_cases: ["gaming"], required_features: ["hdmi_2_1"] },
                         `${RUN}-a`, "Посоветуй телевизор для PS5.");
  out.removed_requirement = { status: ps5.status, confidence: ps5.confidence, products: (ps5.products || []).length,
    not_applied: notApplied(ps5), features_checked: (ps5.request || {}).features_checked || null,
    hdmi_2_1_states: distinct(ps5, "hdmi_2_1"), summary_hdmi_2_1: ((ps5.feature_summary || {}).counts || {}).hdmi_2_1 || null,
    summary_features: Object.keys((ps5.feature_summary || {}).counts || {}).length,
    all_yes: (ps5.feature_summary || {}).all_yes || null, not_listed_for_all: (ps5.feature_summary || {}).not_listed_for_all || null,
    price_text: ((ps5.products || [])[1] || {}).price_text || null,
    not_listed_gap: (ps5.gaps || []).some((g) => g.kind === "attribute_not_listed_for_product" && (g.attributes || []).includes("hdmi_2_1")) };
  const named = await call(auth, "get_tv", { model: "QE65S95HAUXPY" }, `${RUN}-b`, "А HDMI 2.1 и VRR у него есть?");
  out.named_feature_on_overview = { status: named.status, features: (named.products || [{}])[0].features || null };
  const plain = await call(auth, "get_tv", { model: "QE65S95HAUXPY" }, null, null);
  out.overview_without_context = { status: plain.status, features: (plain.products || [{}])[0].features || null };

  // MVP-2
  const stated = { min_screen_size_inches: 43, max_screen_size_inches: 50, max_price: 100000 };
  const t1 = await call(auth, "search_tvs", stated, `${RUN}-c`, "ну дюймов 43-50, до сотки где-то", 0);
  out.slang_budget = { status: t1.status, constraints: (t1.request || {}).constraints, not_applied: notApplied(t1) };
  const t2 = await call(auth, "search_tvs", { max_price: 40000, min_screen_size_inches: 40, max_screen_size_inches: 50,
                                              sort: "price_asc", limit: 3 }, `${RUN}-c`, "а что подешевле есть?", 1);
  out.invented_after_slang = { status: t2.status, constraints: (t2.request || {}).constraints, not_applied: notApplied(t2) };
  const t3 = await call(auth, "search_tvs", { ...stated, sort: "price_asc" }, `${RUN}-c`, "а ещё дешевле?", 2);
  out.stated_limits_with_order = { status: t3.status, constraints: (t3.request || {}).constraints, not_applied: notApplied(t3),
    first: (t3.products || []).slice(0, 2).map((p) => `${p.model_code} ${p.current_price_rub}`) };

  // MVP-3
  const atmos = await call(auth, "get_catalog_stats", { stat: "count", availability: "available", attributes: ["dolby_atmos"] }, null, null);
  out.feature_count = { status: atmos.status, counted: atmos.counted, counts: atmos.counts, attribute_counts: atmos.attribute_counts,
                        scope_note: Boolean(atmos.scope_note) };
  const series = await call(auth, "get_catalog_stats", { stat: "count", model: "QN70H" }, null, null);
  out.series_count = { status: series.status, counted: series.counted, counts: series.counts };
  const oled = await call(auth, "get_catalog_stats", { stat: "count", panel_technology: ["OLED"], attributes: ["hz_120", "vrr"] }, null, null);
  out.group_feature_count = { counted: oled.counted, counts: oled.counts, attribute_counts: oled.attribute_counts };
  const hz = await call(auth, "get_catalog_stats", { stat: "count", max_price: 50000, group_by: "refresh_rate_hz" }, null, null);
  out.refresh_rates_in_band = { counts: hz.counts, groups: hz.groups };
  const below = await call(auth, "get_catalog_stats", { stat: "count", max_price: 50000, attributes: ["hz_120"] }, null, null);
  out.values_behind_a_no = { attribute_counts: below.attribute_counts, attribute_values: below.attribute_values };
  const unknown = await call(auth, "get_catalog_stats", { stat: "count", model: "S80C" }, null, null);
  out.unknown_series = { status: unknown.status, counts: unknown.counts || null };
  const bad = await call(auth, "get_catalog_stats", { stat: "cheapest", attributes: ["vrr"] }, null, null);
  out.attributes_with_extreme = { status: bad.status };
  console.log(JSON.stringify(out, null, 1));
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.message).slice(0, 200) })); process.exit(1); });
