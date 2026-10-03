// Phase 4D.2A boundary probe (4D.2B-R: per-turn key + cap across sessions). Runs with plain Node (e.g. inside the n8n container):
//   <token on stdin> | node -e "$(cat mcp_probe.js)" http://samsung-consultant:8765
// The token is read from stdin only and never printed. Prints results, not payloads.
const BASE = process.argv[1] && process.argv[1].startsWith("http") ? process.argv[1] : process.argv[2];
const EXPECTED = ["search_tvs", "get_tv", "compare_tvs", "recommend_tvs", "get_catalog_stats"];

async function readStdin() {
  let data = "";
  for await (const chunk of process.stdin) data += chunk;
  return data.trim();
}

const PROBE_TURN = `probe-${Date.now()}`;

async function post(body, headers = {}, turn = PROBE_TURN) {
  const r = await fetch(`${BASE}/mcp${turn ? `?turn=${turn}` : ""}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", ...headers },
    body: JSON.stringify(body),
  });
  const text = await r.text();
  return { status: r.status, session: r.headers.get("mcp-session-id"), json: text ? JSON.parse(text) : null };
}

function closed(schema) {
  if (schema.type === "object") {
    if (schema.additionalProperties !== false) return false;
    return Object.values(schema.properties || {}).every(closed);
  }
  if (schema.type === "array") return closed(schema.items);
  return true;
}

(async () => {
  const token = await readStdin();
  const out = {};
  const health = await fetch(`${BASE}/healthz`);
  out.healthz = health.status;
  const ping = { jsonrpc: "2.0", id: 1, method: "ping" };
  out.no_credential = (await post(ping)).status;
  out.wrong_credential = (await post(ping, { Authorization: "Bearer " + "0".repeat(64) })).status;
  if (!token) { console.log(JSON.stringify(out)); return; }
  const auth = { Authorization: `Bearer ${token}` };
  const init = await post({ jsonrpc: "2.0", id: 2, method: "initialize",
    params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "4d2a-probe", version: "1" } } }, auth);
  out.authenticated_initialize = init.status;
  out.protocol = init.json && init.json.result && init.json.result.protocolVersion;
  const s = { ...auth, "Mcp-Session-Id": init.session };
  out.initialized_notification = (await post({ jsonrpc: "2.0", method: "notifications/initialized" }, s)).status;
  const list = await post({ jsonrpc: "2.0", id: 3, method: "tools/list" }, s);
  const tools = (list.json.result.tools || []);
  out.tools = tools.map((t) => t.name);
  out.tools_match_expected = JSON.stringify(out.tools) === JSON.stringify(EXPECTED);
  out.schemas_closed = tools.every((t) => closed(t.inputSchema));
  out.required = Object.fromEntries(tools.map((t) => [t.name, t.inputSchema.required]));
  out.read_only_hint = tools.every((t) => t.annotations && t.annotations.readOnlyHint === true);
  out.attributes_max_items = Object.fromEntries(tools.filter((t) => t.inputSchema.properties.attributes)
    .map((t) => [t.name, t.inputSchema.properties.attributes.maxItems]));             // 4D.2D: whole registry
  const call = await post({ jsonrpc: "2.0", id: 4, method: "tools/call",
    params: { name: "get_catalog_stats", arguments: { stat: "cheapest" } } }, s);
  const payload = JSON.parse(call.json.result.content[0].text);
  out.contract = payload.contract;
  out.smoke_call = { tool: payload.tool, status: payload.status, isError: call.json.result.isError,
    products: (payload.products || []).map((p) => `${p.model_code} ${p.current_price_rub} RUB available=${p.available}`) };
  const bad = await post({ jsonrpc: "2.0", id: 5, method: "tools/call",
    params: { name: "search_tvs", arguments: { sql: "DROP TABLE products" } } }, s);
  out.invalid_arguments_call = JSON.parse(bad.json.result.content[0].text).status;
  const del = await fetch(`${BASE}/mcp`, { method: "DELETE", headers: s });
  out.session_closed = del.status;
  // Per-turn cap as n8n drives it: one new MCP session per call, all with the same turn key.
  async function callInNewSession(turn) {
    const i = await post({ jsonrpc: "2.0", id: 10, method: "initialize",
      params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "4d2b-probe", version: "1" } } }, auth, turn);
    const r = await post({ jsonrpc: "2.0", id: 11, method: "tools/call",
      params: { name: "get_catalog_stats", arguments: { stat: "count" } } }, { ...auth, "Mcp-Session-Id": i.session }, turn);
    return r.json.error ? "rpc_error" : JSON.parse(r.json.result.content[0].text).status;
  }
  const capTurn = `probe-cap-${Date.now()}`;
  out.cap_across_sessions = [];
  for (let n = 0; n < 4; n++) out.cap_across_sessions.push(await callInNewSession(capTurn));
  out.call_without_turn_key = await callInNewSession(null);
  console.log(JSON.stringify(out, null, 1));
})().catch((e) => { console.log(JSON.stringify({ probe_error: String(e && e.message) })); process.exit(1); });
