// Gate 4E.2 semantic-guard probe. Runs with plain Node inside the n8n container (same network path as the Agent):
//   <token on stdin> | node -e "$(cat guard_probe.js)" http://samsung-consultant:8765 [phase]
// phase: "calls" (default) -- guard decisions over the real MCP boundary;
//        "restart-1" / "restart-2" -- the two halves of the in-memory-evidence restart check (same conversation key).
// The token is read from stdin only and never printed. Prints decisions and statuses, not payloads.
const BASE = process.argv.find((a) => a && a.startsWith("http"));
const PHASE = process.argv[process.argv.length - 1].startsWith("http") ? "calls" : process.argv[process.argv.length - 1];
const RUN = `g${Date.now()}`;

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

async function call(auth, tool, args, turn, conv, q, h = null) {
  let query = `turn=${turn}`;
  if (h !== null) query += `&h=${h}`;             // Gate 4E.2A: earlier turns in the Agent's memory
  if (conv) query += `&conv=${encodeURIComponent(conv)}`;
  if (q) query += `&q=${encodeURIComponent(q)}`;
  const init = await post({ jsonrpc: "2.0", id: 1, method: "initialize",
    params: { protocolVersion: "2025-03-26", capabilities: {}, clientInfo: { name: "4e2-guard-probe", version: "1" } } }, auth, query);
  const s = { ...auth, "Mcp-Session-Id": init.session };
  const res = await post({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: tool, arguments: args } }, s, query);
  const payload = JSON.parse(res.json.result.content[0].text);
  const req = payload.request || {};
  return { http: res.status, status: payload.status, required_features: req.required_features || null,
           use_cases: req.use_cases || null,
           constraints: (req.constraints || []).map((c) => (typeof c === "string" ? c : JSON.stringify(c))),
           not_applied: req.not_applied ? req.not_applied.constraints : null,
           products: (payload.products || []).length };
}

(async () => {
  const token = await readStdin();
  const auth = { Authorization: `Bearer ${token}` };
  const conv = `probe-${RUN}`;
  const out = { phase: PHASE };
  if (PHASE === "calls") {
    const invented = { use_cases: ["gaming"], required_features: ["hdmi_2_1"] };
    out.ps5_no_q = await call(auth, "recommend_tvs", invented, `${RUN}-1`, null, null);
    out.ps5_with_q = await call(auth, "recommend_tvs", invented, `${RUN}-2`, `${conv}-a`, "Посоветуй телевизор для PS5.", 0);
    out.ps5_explicit_hdmi = await call(auth, "recommend_tvs", invented, `${RUN}-3`, `${conv}-b`,
                                       "Нужен телевизор для PS5, обязательно HDMI 2.1.", 0);
    out.ps5_q_without_conv = await call(auth, "recommend_tvs", invented, `${RUN}-4`, null, "Посоветуй телевизор для PS5.");
    out.explicit_budget = await call(auth, "recommend_tvs", { max_price: 150000 }, `${RUN}-5`, `${conv}-c`,
                                     "Посоветуй телевизор до 150 тысяч.", 0);
    out.invented_budget = await call(auth, "recommend_tvs", { use_cases: ["movies"], max_price: 150000 }, `${RUN}-6`,
                                     `${conv}-d`, "Хочу телевизор для кино с хорошей картинкой.", 0);
  } else if (PHASE === "restart-1") {
    out.turn1 = await call(auth, "recommend_tvs", { panel_technology: ["OLED"], max_price: 200000 }, "restart-t1",
                           "probe-restart-check", "Хочу OLED до 200 тысяч.", 0);
    out.turn2_before_restart = await call(auth, "recommend_tvs", { panel_technology: ["OLED"], max_price: 200000,
                                          use_cases: ["gaming"] }, "restart-t2", "probe-restart-check", "А какой лучше для игр?", 1);
  } else if (PHASE === "restart-2") {
    out.turn3_after_restart = await call(auth, "recommend_tvs", { panel_technology: ["OLED"], max_price: 200000,
                                         use_cases: ["gaming"] }, "restart-t3", "probe-restart-check", "А какой лучше для игр?", 2);
  }
  console.log(JSON.stringify(out, null, 1));
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.message).slice(0, 200) })); process.exit(1); });
