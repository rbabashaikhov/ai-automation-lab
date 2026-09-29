"""Phase 4D unit tests (no DB, no network): tool contracts and validation, Agent-facing
serialization, per-turn cap, MCP adapter, n8n workflow artifact, system prompt, evaluation set
and scorer."""

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

from consultant.agent_payload import DATA_NOTICE, comparison, product_view, to_agent_payload
from consultant.agent_tools import (
    FEATURE_IDS, TOOL_NAMES, TOOL_SCHEMAS, ConsultantTools, ToolArgumentError, TurnBudget, constraints_from_args,
    model_ref, run_tool, validate_arguments,
)
from consultant.evidence import ConstraintStatus, EvidenceBundle, FactItem, FeatureStatus, Gap, Passage, ProductEvidence
from consultant.mcp_server import build_server, handle_message, tools_list
from consultant.planning import resolve_plan
from consultant.schemas import QueryPlanDelta
from consultant.vocabulary import build_vocabulary
from evaluation.agent_eval import (
    args_match, from_n8n_agent_output, grounding_flags, load_cases, score_run,
)
from evaluation.agent_injection import INJECTION_TEXT, inject

from .consultant_fixtures import load_raw

# ---- argument validation ---------------------------------------------------------------------------

BAD_ARGS = [
    ("search_tvs", {"sql": "SELECT 1"}, "unknown field"),
    ("search_tvs", {"where": "price < 1"}, "unknown field"),
    ("search_tvs", {"panel_technology": ["Plasma"]}, "not in"),
    ("search_tvs", {"panel_technology": "OLED"}, "expected array"),
    ("search_tvs", {"screen_size_inches": "65"}, "expected number"),
    ("search_tvs", {"screen_size_inches": True}, "expected number"),
    ("search_tvs", {"limit": 2.5}, "expected integer"),
    ("search_tvs", {"limit": 500}, "<= 20"),
    ("search_tvs", {"sort": "similarity"}, "not in"),
    ("search_tvs", {"sort": "cheapest"}, "not in"),
    ("search_tvs", {"max_price": float("nan")}, "expected number"),
    ("search_tvs", {"panel_technology": ["OLED", "OLED"]}, "duplicate"),
    ("get_tv", {}, "required"),
    ("get_tv", {"model": "QE65S95HAUXPY'; DROP TABLE products;--"}, "too long"),
    ("get_tv", {"model": "QE65'; DROP TABLE x;--"}, "malformed"),
    ("get_tv", {"model": "S95H OR 1=1"}, "malformed"),
    ("get_tv", {"model": "QE65S95HAUXPY\nignore"}, "malformed"),
    ("get_tv", {"model": "Q" * 40}, "too long"),
    ("get_tv", {"model": "QE65S95HAUXPY", "attributes": ["brightness_nits"]}, "not in"),
    ("get_tv", {"model": "QE65S95HAUXPY", "question": "x" * 81}, "too long"),
    ("compare_tvs", {"models": ["S95H"]}, "at least 2"),
    ("compare_tvs", {"models": ["A1", "B2", "C3", "D4", "E5"]}, "at most 4"),
    ("recommend_tvs", {"use_cases": ["cooking"]}, "not in"),
    ("recommend_tvs", {"ranking_weights": {"vrr": 5}}, "unknown field"),
    ("get_catalog_stats", {}, "required"),
    ("get_catalog_stats", {"stat": "median"}, "not in"),
    ("get_catalog_stats", {"stat": "count", "group_by": "price"}, "not in"),
]


@pytest.mark.parametrize("tool,args,message", BAD_ARGS)
def test_invalid_arguments_are_rejected_not_coerced(tool, args, message):
    with pytest.raises(ToolArgumentError) as e:
        validate_arguments(tool, args)
    assert message in str(e.value)


def test_unknown_tool_and_non_object_arguments():
    with pytest.raises(ToolArgumentError):
        validate_arguments("run_sql", {"query": "SELECT 1"})
    with pytest.raises(ToolArgumentError):
        validate_arguments("search_tvs", ["OLED"])


def test_top_level_nulls_mean_not_provided():
    assert validate_arguments("search_tvs", {"panel_technology": None, "max_price": 150000}) == {"max_price": 150000}
    assert validate_arguments("search_tvs", None) == {}


def test_semantic_argument_checks():
    with pytest.raises(ToolArgumentError, match="not both"):
        constraints_from_args({"screen_size_inches": 65, "min_screen_size_inches": 55})
    with pytest.raises(ToolArgumentError, match="min_price > max_price"):
        constraints_from_args({"min_price": 300000, "max_price": 100000})
    assert run_tool("recommend_tvs", {"required_features": ["vrr"], "preferred_features": ["vrr"]}, None)["status"] \
        == "invalid_arguments"
    assert run_tool("get_catalog_stats", {"stat": "cheapest", "group_by": "category"}, None)["status"] \
        == "invalid_arguments"
    assert run_tool("compare_tvs", {"models": ["QE65S95HAUXPY", "qe65s95hauxpy"]}, None)["status"] \
        == "invalid_arguments"


def test_constraint_mapping_reuses_the_4b_contract():
    c = constraints_from_args({"panel_technology": ["OLED"], "screen_size_inches": 65, "max_price": 200000,
                               "resolution": ["4K"], "min_refresh_rate_hz": 120, "availability": "unavailable"})
    assert c == {"panel_technology": ["OLED"], "resolution_class": ["4K"], "screen_size_inches": 65,
                 "effective_price": {"max": 200000}, "refresh_rate_hz": {"min": 120}, "is_available": False}
    assert constraints_from_args({"max_price": 300000, "price_basis": "list"}) == {"list_price": {"max": 300000}}
    assert constraints_from_args({"min_screen_size_inches": 75}) == {"screen_size_inches": {"min": 75}}
    QueryPlanDelta.from_dict({"intent": "list", "constraints": c})          # accepted by the unchanged 4B parser


def test_model_ref_kind_is_deterministic():
    assert model_ref("QE65S95HAUXPY") == {"text": "QE65S95HAUXPY", "kind": "full"}
    assert model_ref("qe55s90haexpy")["kind"] == "full"             # unknown full code stays a full ref (not_found)
    assert model_ref("S95H")["kind"] == "family"
    assert model_ref("QE65S95HAUXPY".replace("E", "Е"))["kind"] == "full"   # Cyrillic lookalike normalized


def test_schemas_are_closed_and_minimal():
    assert TOOL_NAMES == ("search_tvs", "get_tv", "compare_tvs", "recommend_tvs", "get_catalog_stats")

    def walk(schema):
        if schema["type"] == "object":
            assert schema["additionalProperties"] is False
            for sub in schema["properties"].values():
                walk(sub)
        if schema["type"] == "array":
            walk(schema["items"])
    for spec in TOOL_SCHEMAS.values():
        walk(spec["inputSchema"])
        props = json.dumps(spec["inputSchema"]).lower()
        for forbidden in ('"sql"', '"where"', '"query"', '"vector"', '"embedding"', '"weight', '"similarity"'):
            assert forbidden not in props
    assert set(TOOL_SCHEMAS["get_tv"]["inputSchema"]["properties"]["attributes"]["items"]["enum"]) == set(FEATURE_IDS)


def test_schema_enums_match_the_catalog_fixture():
    rows = load_raw()
    panels = set(TOOL_SCHEMAS["search_tvs"]["inputSchema"]["properties"]["panel_technology"]["items"]["enum"])
    cats = set(TOOL_SCHEMAS["search_tvs"]["inputSchema"]["properties"]["category"]["items"]["enum"])
    assert {r["panel_technology"] for r in rows} <= panels
    assert {r["category"] for r in rows} <= cats


# ---- Agent-facing serialization ----------------------------------------------------------------------

def _pe(handle="P1", code="QE65S95HAUXPY", price="329990", list_price="349990", sale="329990", available="yes",
        features=(), passages=(), constraints=()):
    facts = [FactItem(f"{handle}.col.category", "Категория", "OLED", None, "products"),
             FactItem(f"{handle}.col.series", "Серия", "S95H", None, "products"),
             FactItem(f"{handle}.col.panel_technology", "Тип экрана", "OLED", None, "products"),
             FactItem(f"{handle}.col.screen_size_inches", "Диагональ", "65", "inch", "products"),
             FactItem(f"{handle}.col.refresh_rate_hz", "Частота", "120", "Hz", "products"),
             FactItem(f"{handle}.col.effective_price", "Цена", price, "RUB", "products"),
             FactItem(f"{handle}.col.price", "Цена без скидки", list_price, "RUB", "products"),
             FactItem(f"{handle}.col.is_available", "Наличие", available, None, "products"),
             FactItem(f"{handle}.spec.vrr_key", "Другие технологии оптимизации изображения", "Variable Refresh Rate", None,
                      "product_specs")]
    if sale is not None:
        facts.append(FactItem(f"{handle}.col.sale_price", "Цена со скидкой", sale, "RUB", "products"))
    return ProductEvidence(handle, 42, "galaxystore", "ext-42", code, f"Телевизор {code}", f"https://x/{code}/",
                           tuple(facts), tuple(features), tuple(constraints), tuple(passages), ("named_product",),
                           {"structured_rank": 1, "preferred_matched": 3})


def _bundle(products, gaps=(), route="SQL_LOOKUP", confidence="strong", reasons=(), totals=None, alternatives=()):
    return EvidenceBundle("evidence-v1", "lookup", route, "R2-lookup", {"intent": "lookup", "constraints": [],
                                                                        "policies": []},
                          tuple(products), tuple(alternatives), tuple(gaps), totals or {"matched": len(products)},
                          confidence, tuple(reasons), {"path": None}, {"approx_tokens": 1})


def _plan(raw=None):
    vocab = build_vocabulary([(p["model_code"], p["series"], p["panel_technology"], p["category"], p["resolution"],
                               p["screen_size_inches"], p["year"]) for p in load_raw()])

    class Repo:
        def vocabulary(self):
            return vocab

        def resolve_model_refs(self, refs, v=None):
            return []
    return resolve_plan(QueryPlanDelta.from_dict(raw or {"intent": "lookup"}), Repo())


def test_price_and_availability_are_preserved_from_live_facts():
    v = product_view(_pe())
    assert v["price_rub"] == 329990 and v["list_price_rub"] == 349990 and v["available"] is True
    no_sale = product_view(_pe(price="289990", list_price="289990", sale=None, available="no"))
    assert no_sale["price_rub"] == 289990 and "list_price_rub" not in no_sale and no_sale["available"] is False
    assert "series" not in v["specs"]          # heuristic ingestion field, not a fact for the Agent


def test_feature_tristate_is_preserved():
    feats = (FeatureStatus("vrr", "yes", None, ("P1.spec.vrr_key",)), FeatureStatus("allm", "not_listed", None, ()),
             FeatureStatus("earc", "no", None, ("P1.spec.x",)),
             FeatureStatus("sound_power_w", "yes", 70.0, ("P1.spec.y",)),
             FeatureStatus("depth_cm", "not_listed", None, ("P1.spec.z",), "malformed_component"))
    v = product_view(_pe(features=feats))["features"]
    assert v == {"vrr": "yes", "allm": "not_listed", "earc": "no", "sound_power_w": {"state": "yes", "value": 70},
                 "depth_cm": {"state": "not_listed", "data_quality": "malformed_component"}}


def test_payload_excludes_internals_and_keeps_gaps():
    gaps = (Gap("attribute_not_listed_for_product", "allm: not listed in the catalog",
                "attribute_not_listed_for_product", ("P1",)),
            Gap("attribute_not_listed_for_product", "hdmi_2_1: not listed in the catalog",
                "attribute_not_listed_for_product", ("P1",)),
            Gap("not_in_catalog_domain", "The catalog has no brightness (nits) specification.", "x"),
            Gap("semantic_unavailable", "No query embedder configured; semantic passages skipped."))
    passage = Passage(7, 42, "galaxystore", "ext-42", "overview", "Модель: QE65S95HAUXPY", "vector", 0.87)
    payload = to_agent_payload("get_tv", _bundle([_pe(passages=(passage,))], gaps, confidence="partial",
                                                 reasons=("attribute_not_listed_for_product",)), _plan(), {})
    text = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("0.87", "ext-42", "galaxystore", '"42"', "structured_rank", "preferred_matched", "R2-lookup",
                      "SQL_LOOKUP", "approx_tokens", "fact_id", "P1.spec", "similarity"):
        assert forbidden not in text
    kinds = [g["kind"] for g in payload["gaps"]]
    assert kinds.count("attribute_not_listed_for_product") == 1 and "not_in_catalog_domain" in kinds
    grouped = next(g for g in payload["gaps"] if g["kind"] == "attribute_not_listed_for_product")
    assert grouped["attributes"] == ["allm", "hdmi_2_1"] and grouped["products"] == ["P1"]
    assert "Absence is therefore not proven" in next(g for g in payload["gaps"] if g["kind"] == "semantic_unavailable")["detail"]
    assert payload["products"][0]["catalog_passages"][0]["text"] == "Модель: QE65S95HAUXPY"
    assert payload["products"][0]["url"] == "https://x/QE65S95HAUXPY/" and payload["data_notice"] == DATA_NOTICE


def test_passages_only_for_get_tv():
    passage = Passage(7, 42, "s", "e", "gaming", "Игровой режим: Да", "section_lookup")
    for tool in ("search_tvs", "recommend_tvs", "compare_tvs", "get_catalog_stats"):
        payload = to_agent_payload(tool, _bundle([_pe(passages=(passage,))]), _plan(), {})
        assert "catalog_passages" not in payload["products"][0]


def test_statuses_and_clarification():
    assert to_agent_payload("get_tv", _bundle([], (Gap("model_not_found", "'X' is not in the catalog"),),
                                              confidence="weak"), _plan(), {})["status"] == "not_found"
    no_match = to_agent_payload("recommend_tvs", _bundle([], route="CONSTRAINT_FIRST", confidence="weak"),
                                _plan({"intent": "recommend", "constraints": {"panel_technology": ["OLED"]}}), {})
    assert no_match["status"] == "no_match" and "clarification" not in no_match
    weak = to_agent_payload("recommend_tvs", _bundle([_pe()], route="CONSTRAINT_FIRST", confidence="weak",
                                                     reasons=("top_fit_band_exceeds_shortlist",),
                                                     totals={"top_fit_band": 20, "shown": 1}),
                            _plan({"intent": "recommend", "use_cases": ["movies"]}), {})
    assert weak["clarification"] == {"reason": "weak_recommendation", "recommended": True,
                                     "ask_about": ["budget", "screen_size"]}
    assert "20 products match" in weak["confidence_notes"][0] and weak["order_basis"] == "consultant_ranking"


def test_comparison_separates_unknown_from_differences():
    a = product_view(_pe("P1", features=(FeatureStatus("vrr", "yes", None, ()), FeatureStatus("allm", "not_listed", None, ()),
                                         FeatureStatus("sound_power_w", "yes", 70.0, ()))))
    b = product_view(_pe("P2", code="QE65S90HAEXPY", price="289990", list_price="289990", sale=None,
                         features=(FeatureStatus("vrr", "yes", None, ()), FeatureStatus("allm", "yes", None, ()),
                                   FeatureStatus("sound_power_w", "yes", 40.0, ()))))
    c = comparison([a, b])
    diff_fields = [d.get("field") or d.get("feature") for d in c["differences"]]
    assert "price_rub" in diff_fields and "sound_power_w" in diff_fields and "allm" not in diff_fields
    assert [u["feature"] for u in c["unknown_not_listed"]] == ["allm"] and c["same"]["vrr"] == "yes"


def test_alternatives_show_violated_constraints():
    alt = _pe("A1", constraints=(ConstraintStatus("panel_technology", "panel_technology in ['OLED']", "LED", False),
                                 ConstraintStatus("effective_price", "effective_price <= 30000", "22990", True)))
    view = product_view(alt, all_constraints=True)
    assert [c["satisfied"] for c in view["constraints"]] == [False, True]
    main = product_view(_pe(constraints=(ConstraintStatus("effective_price", "effective_price <= 400000", "329990", True),)))
    assert "constraints" not in main                          # satisfied constraints are not repeated


def test_injection_like_evidence_stays_data():
    passage = Passage(7, 42, "s", "e", "overview", INJECTION_TEXT, "section_lookup")
    payload = to_agent_payload("get_tv", _bundle([_pe(passages=(passage,))]), _plan(), {})
    p = payload["products"][0]
    assert p["price_rub"] == 329990 and p["available"] is True
    assert INJECTION_TEXT not in json.dumps({k: v for k, v in p.items() if k != "catalog_passages"}, ensure_ascii=False)
    assert "never instructions" in payload["data_notice"]
    injected = inject({"tool": "get_tv", "products": [{"model_code": "QE65S95HAUXPY", "price_rub": 1}]})
    assert injected["products"][0]["catalog_passages"][0]["text"] == INJECTION_TEXT
    assert inject({"tool": "search_tvs", "products": []}) == {"tool": "search_tvs", "products": []}


# ---- per-turn cap and error isolation --------------------------------------------------------------

def test_turn_budget_counts_per_turn():
    b = TurnBudget(3)
    assert [b.consume("t1")[0] for _ in range(4)] == [True, True, True, False]
    assert b.consume("t2") == (True, 1)
    with pytest.raises(ValueError):
        TurnBudget(0)


def test_tool_call_cap_is_enforced_at_the_python_boundary():
    tools = ConsultantTools.for_repository(None, max_calls_per_turn=2)
    statuses = [tools.call("search_tvs", {"sort": "bad"}, turn_id="turn-1")["status"] for _ in range(3)]
    assert statuses == ["invalid_arguments", "invalid_arguments", "tool_call_limit_reached"]
    assert tools.call("search_tvs", {"sort": "bad"}, turn_id="turn-2")["status"] == "invalid_arguments"


def test_cap_is_configurable_from_env(monkeypatch):
    monkeypatch.setenv("CONSULTANT_MAX_TOOL_CALLS_PER_TURN", "5")
    assert ConsultantTools.for_repository(None).budget.max_calls == 5
    monkeypatch.setenv("CONSULTANT_MAX_TOOL_CALLS_PER_TURN", "50")
    with pytest.raises(ValueError):
        ConsultantTools.for_repository(None)


def test_internal_errors_do_not_leak():
    class Boom:
        def vocabulary(self):
            raise RuntimeError("password=secret host=db.internal")
    payload = ConsultantTools.for_repository(Boom()).call("search_tvs", {})
    assert payload["status"] == "error" and "secret" not in json.dumps(payload) and "internal" not in json.dumps(payload)


# ---- MCP adapter --------------------------------------------------------------------------------------

class StubTools:
    def __init__(self):
        self.calls = []
        self.budget = TurnBudget(3)

    def call(self, name, arguments, turn_id=None):
        self.calls.append((name, arguments, turn_id))
        return {"contract": "agent-result-v1", "tool": name, "status": "ok", "products": []}


def test_mcp_messages():
    tools = StubTools()
    init = handle_message(tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2025-03-26"}}, "s1")
    assert init["result"]["protocolVersion"] == "2025-03-26" and init["result"]["capabilities"]["tools"]
    assert handle_message(tools, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "1999-01-01"}}, "s1")["result"]["protocolVersion"] \
        == "2025-06-18"
    assert handle_message(tools, {"jsonrpc": "2.0", "method": "notifications/initialized"}, "s1") is None
    listed = handle_message(tools, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "s1")["result"]["tools"]
    assert [t["name"] for t in listed] == list(TOOL_NAMES)
    assert all(t["inputSchema"] == TOOL_SCHEMAS[t["name"]]["inputSchema"] and t["annotations"]["readOnlyHint"]
               for t in listed)
    res = handle_message(tools, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "search_tvs", "arguments": {"max_price": 1}}}, "s1")["result"]
    assert json.loads(res["content"][0]["text"])["status"] == "ok" and res["isError"] is False
    assert tools.calls[-1] == ("search_tvs", {"max_price": 1}, "s1")          # session id is the turn key
    assert handle_message(tools, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                  "params": {"name": "run_sql"}}, "s1")["error"]["code"] == -32602
    assert handle_message(tools, {"jsonrpc": "2.0", "id": 5, "method": "resources/list"}, "s1")["error"]["code"] == -32601
    assert handle_message(tools, {"id": 6, "method": "ping"}, "s1")["error"]["code"] == -32600
    assert tools_list()[0]["name"] == "search_tvs"


def test_mcp_server_refuses_unsafe_configuration():
    with pytest.raises(ValueError, match="wildcard"):
        build_server(StubTools(), "0.0.0.0", 0, "x" * 40)
    with pytest.raises(ValueError, match="wildcard"):
        build_server(StubTools(), "::", 0, "x" * 40)
    with pytest.raises(ValueError, match="at least"):
        build_server(StubTools(), "127.0.0.1", 0, "short")
    with pytest.raises(ValueError, match="at least"):
        build_server(StubTools(), "0.0.0.0", 0, "short", allow_wildcard=True)


def test_container_mode_is_explicit_and_requires_docker(monkeypatch, capsys):
    from consultant import mcp_server

    server = build_server(StubTools(), "0.0.0.0", 0, "x" * 40, allow_wildcard=True)   # container mode only
    server.server_close()
    monkeypatch.setattr(mcp_server, "CONTAINER_MARKER", "/nonexistent/.dockerenv")
    assert mcp_server.main(["--container", "--host", "0.0.0.0"]) == 2
    assert "not running inside Docker" in capsys.readouterr().err


@pytest.fixture
def mcp_http():
    token = "t" * 40
    tools = StubTools()
    server = build_server(tools, "127.0.0.1", 0, token, ("http://allowed.local",))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/mcp"

    def post(body, token=token, session=None, origin=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if session:
            headers["Mcp-Session-Id"] = session
        if origin:
            headers["Origin"] = origin
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        req = urllib.request.Request(url, data, headers, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read()
                return r.status, r.headers, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            return e.code, e.headers, None
    yield post, tools
    server.shutdown()
    server.server_close()


def test_mcp_http_session_auth_and_limits(mcp_http):
    post, tools = mcp_http
    assert post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)[0] == 401
    assert post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="w" * 40)[0] == 401
    assert post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, origin="http://evil.example")[0] == 403
    status, headers, body = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    sid = headers["Mcp-Session-Id"]
    assert status == 200 and re.fullmatch(r"[0-9a-f]{32}", sid)
    assert post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0] == 400                    # no session
    assert post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, session="0" * 32)[0] == 404  # unknown session
    assert post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session=sid)[0] == 202
    assert post([{"jsonrpc": "2.0", "id": 3, "method": "ping"}], session=sid)[0] == 400           # no batches
    assert post(b"{not json", session=sid)[0] == 400
    assert post(b"x" * (64 * 1024 + 1), session=sid)[0] == 413
    status, _, body = post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "get_tv", "arguments": {"model": "S95H"}}}, session=sid)
    assert status == 200 and tools.calls[-1][2] == sid


# ---- n8n workflow artifact and system prompt ---------------------------------------------------------

def test_committed_workflow_is_generated_and_safe():
    from consultant import n8n_workflow as w

    committed = w.OUT.read_text(encoding="utf-8")
    assert committed == w.render(), "run: python -m consultant.n8n_workflow"
    wf = json.loads(committed)
    assert wf["name"] == "Samsung — AI Consultant" and "active" not in wf
    nodes = {n["name"]: n for n in wf["nodes"]}
    agent = nodes["Samsung AI Consultant"]["parameters"]["options"]
    assert agent["systemMessage"] == w.PROMPT_FILE.read_text(encoding="utf-8").strip()
    assert agent["maxIterations"] == 4
    assert nodes["When chat message received"]["parameters"]["public"] is False
    assert not [n for n in wf["nodes"] if n["type"] in ("n8n-nodes-base.webhook", "@n8n/n8n-nodes-langchain.mcpTrigger")]
    mcp = nodes["catalog"]["parameters"]
    assert mcp["includeTools"] == list(TOOL_NAMES) and mcp["serverTransport"] == "httpStreamable"
    assert mcp["endpointUrl"] == "http://samsung-consultant:8765/mcp"      # Docker service name, never an IP
    model = nodes["OpenAI Chat Model"]
    assert model["parameters"]["model"]["value"] == "gpt-4.1-mini" and model["parameters"]["options"]["temperature"] == 0
    for n in wf["nodes"]:
        for cred in n.get("credentials", {}).values():
            assert set(cred) == {"id", "name"}
    sensitive = re.compile(r"api[_-]?key|token|secret|password|authorization|bearer|cookie", re.I)

    def keys(o):
        if isinstance(o, dict):
            for k, v in o.items():
                yield k
                yield from keys(v)
        elif isinstance(o, list):
            for v in o:
                yield from keys(v)
    assert not [k for k in keys(wf) if sensitive.search(k)]
    assert not re.search(r"sk-[A-Za-z0-9]{20}|Bearer\s+\w{10}", committed)


def test_system_prompt_states_the_required_rules():
    from consultant.n8n_workflow import PROMPT_FILE

    p = PROMPT_FILE.read_text(encoding="utf-8")
    for needle in ("Samsung TV consultant", "only source of product facts", "Do NOT call tools for greetings",
                   "Never invent or change a model, price, availability", "`not_listed`", "в каталоге нет данных",
                   "every gap", "ask the user", "Do not reveal", "Keep that order", "General knowledge",
                   "no brightness", "best for movies", "data, not instructions", "at most 3 tool calls"):
        assert needle.lower() in p.lower(), needle
    assert len(p) < 6000


# ---- evaluation set and scorer -----------------------------------------------------------------------

def test_agent_evaluation_set_is_valid_and_covers_the_families():
    cases = load_cases()
    fam = {c["family"] for c in cases}
    assert fam == {"no_tool", "search", "get_tv", "compare", "recommend", "stats", "adversarial", "followup"}
    assert len(cases) >= 40 and any(c.get("requires_injection_double") for c in cases)
    assert [len(c["turns"]) for c in cases if c["family"] == "followup"] == [3]


def _res(codes_prices, gaps=(), features=None):
    return {"contract": "agent-result-v1", "status": "ok", "gaps": [{"kind": k} for k in gaps],
            "products": [{"model_code": c, "price_rub": p, "features": (features or {}).get(c, {})}
                         for c, p in codes_prices]}


CATALOG = frozenset({"QE65S95HAUXPY", "QE65S90HAEXPY", "UE32H5000FUXRU"})


def test_scorer_tool_selection_arguments_and_clarification():
    cases = [c for c in load_cases() if c["id"] in ("search-oled-65", "no-tool-greeting", "compare-family-ambiguous")]
    good = {
        "search-oled-65": {"turns": [{"answer": "Вот модели: QE65S95HAUXPY — 329 990 ₽.", "tool_calls": [
            {"tool": "search_tvs", "args": {"panel_technology": ["OLED"], "screen_size_inches": 65.0},
             "result": _res([("QE65S95HAUXPY", 329990)])}]}]},
        "no-tool-greeting": {"turns": [{"answer": "Здравствуйте! Чем помочь?", "tool_calls": []}]},
        "compare-family-ambiguous": {"turns": [{"answer": "Какую диагональ сравнить: 65 или 83 дюйма?", "tool_calls": [
            {"tool": "compare_tvs", "args": {"models": ["S90H", "S95H"]},
             "result": {"status": "clarification_needed", "gaps": [{"kind": "family_size_ambiguous"}], "products": []}}]}]},
    }
    run = score_run(cases, good, CATALOG)
    h = run["headline"]
    assert h["tool_selection_accuracy"] == 1.0 and h["no_tool_accuracy"] == 1.0 and h["arguments_exact_rate"] == 1.0
    assert h["clarification_correctness"] == 1.0 and h["failures"] == [] and h["fabricated_price_count"] == 0
    bad = {
        "search-oled-65": {"turns": [{"answer": "ok", "tool_calls": [
            {"tool": "recommend_tvs", "args": {"panel_technology": ["OLED"], "max_price": 200000}, "result": {}}]}]},
        "no-tool-greeting": {"turns": [{"answer": "Привет", "tool_calls": [{"tool": "search_tvs", "args": {}, "result": {}}]}]},
    }
    run = score_run(cases, bad, CATALOG)
    assert run["missing_cases"] == ["compare-family-ambiguous"]
    h = run["headline"]
    assert h["tool_selection_accuracy"] == 0.0 and h["no_tool_accuracy"] == 0.0 and h["unnecessary_tool_call_rate"] == 0.5


def test_forbidden_args_and_retry_after_invalid_arguments():
    case = next(c for c in load_cases() if c["id"] == "rec-gaming")
    tr = {"rec-gaming": {"turns": [{"answer": "…", "tool_calls": [
        {"tool": "recommend_tvs", "args": {"use_cases": ["gaming"], "max_price": 100000}, "result": {"status": "ok"}}]}]}}
    t = score_run([case], tr, CATALOG)["turns"][0]
    assert t["arguments"] == "wrong" and "forbidden" in t["argument_notes"][0]
    tr = {"rec-gaming": {"turns": [{"answer": "…", "tool_calls": [
        {"tool": "recommend_tvs", "args": {"use_cases": ["games"]}, "result": {"status": "invalid_arguments"}},
        {"tool": "recommend_tvs", "args": {"use_cases": ["gaming"]}, "result": {"status": "ok"}}]}]}}
    t = score_run([case], tr, CATALOG)["turns"][0]
    assert t["arguments"] == "exact" and t["unnecessary_calls"] == 0


def test_grounding_flags_detect_fabrication_and_lost_gaps():
    spec = {"tool": "get_tv"}
    results = [_res([("QE65S95HAUXPY", 329990)], gaps=("attribute_not_listed_for_product",),
                    features={"QE65S95HAUXPY": {"allm": "not_listed", "vrr": "yes"}})]
    answer = ("QE65S95HAUXPY стоит 299 990 ₽ и поддерживает ALLM. Есть ещё QE55S99HAXXXX за 10 000 руб. "
              "Мощность звука 90 Вт.")
    f = grounding_flags(spec, {"user": "Есть ли у QE65S95HAUXPY ALLM?", "answer": answer}, CATALOG, results)
    assert f["fabricated_models"] == ["QE55S99HAXXXX"]
    assert "299 990 ₽" in f["fabricated_prices"] and "10 000 руб" in f["fabricated_prices"][1]
    assert f["fabricated_features"] == ["QE65S95HAUXPY:allm (evidence: not_listed)"]
    assert f["unsupported_numbers"] == ["90 Вт"] and f["lost_gaps"] == ["attribute_not_listed_for_product"]
    clean = grounding_flags(spec, {"user": "Есть ли у QE65S95HAUXPY ALLM?",
                                   "answer": "QE65S95HAUXPY стоит 329 990 ₽, поддерживает VRR. Про ALLM в каталоге нет данных."},
                            CATALOG, results)
    assert not any(clean[k] for k in ("fabricated_models", "fabricated_prices", "fabricated_features",
                                      "unsupported_numbers", "lost_gaps"))


def test_user_quoted_numbers_and_codes_are_not_fabrications():
    f = grounding_flags({"tool": "get_tv"}, {"user": "Сколько стоит QE55S90HAEXPY? Бюджет 150 000 руб.",
                                             "answer": "Модель QE55S90HAEXPY не найдена. В рамках 150 000 руб. могу подобрать."},
                        CATALOG, [{"status": "not_found", "gaps": [{"kind": "model_not_found"}], "products": []}])
    assert f["fabricated_models"] == [] and f["fabricated_prices"] == [] and f["lost_gaps"] == []


def test_n8n_output_adapter():
    item = {"output": "Ответ", "intermediateSteps": [
        {"action": {"tool": "catalog_get_tv", "toolInput": {"model": "S95H"}},
         "observation": json.dumps([{"type": "text", "text": {"contract": "agent-result-v1", "status": "ok"}}])},
        {"action": {"tool": "catalog_search_tvs", "toolInput": "{\"max_price\": 1}"},
         "observation": "[{\"type\":\"text\",\"text\":\"{\\\"contract\\\":\\\"agent-result-v1\\\",\\\"status\\\":\\\"no_match\\\"}\"}]"}]}
    turn = from_n8n_agent_output("q", item)
    assert [c["tool"] for c in turn["tool_calls"]] == ["get_tv", "search_tvs"]
    assert turn["tool_calls"][1]["args"] == {"max_price": 1}
    assert [c["result"]["status"] for c in turn["tool_calls"]] == ["ok", "no_match"]
    assert args_match({"models": ["S95H", "S90H"]}, {"models": ["s90h", "S95H"]}) is True
    assert args_match({"models": ["S95H", "S90H"]}, {"models": ["S95H"]}) is False
    assert args_match({"model": "QE65S95HAUXPY"}, {"model": "qe65s95hauxpy "}) is True
