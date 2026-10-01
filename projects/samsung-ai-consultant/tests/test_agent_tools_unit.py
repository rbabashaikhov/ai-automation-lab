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
    assert v["current_price_rub"] == 329990 and v["price_before_discount_rub"] == 349990 and v["available"] is True
    no_sale = product_view(_pe(price="289990", list_price="289990", sale=None, available="no"))
    assert (no_sale["current_price_rub"] == 289990 and "price_before_discount_rub" not in no_sale
            and no_sale["available"] is False)
    assert not {"price_rub", "list_price_rub"} & set(v)          # v1 names are gone (agent-result-v2)
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
    assert "current_price_rub" in diff_fields and "sound_power_w" in diff_fields and "allm" not in diff_fields
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
    assert p["current_price_rub"] == 329990 and p["available"] is True
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
    assert tools.calls[-1] == ("search_tvs", {"max_price": 1}, "session:s1")  # fallback: session id
    handle_message(tools, {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": "search_tvs", "arguments": {}}}, "s1", "501")
    assert tools.calls[-1][2] == "turn:501"                                     # the turn key wins
    refused = handle_message(tools, {"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                                     "params": {"name": "search_tvs", "arguments": {}}}, "s1", None, True)
    assert refused["error"]["code"] == -32600 and "turn" in refused["error"]["message"]
    assert len(tools.calls) == 2                                                # never reached the tools
    assert "result" in handle_message(tools, {"jsonrpc": "2.0", "id": 9, "method": "tools/list"}, "s1", None, True)
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
    assert status == 200 and tools.calls[-1][2] == f"session:{sid}"


# ---- per-turn cap across MCP sessions (Gate 4D.2B-R) ----------------------------------------------------
#
# n8n Agent v3 runs every tool call as its own engine action, and the MCP Client Tool opens a new MCP
# session for each (observed live in Gate 4D.2B). These tests reproduce that shape: one fresh session
# per call, all carrying the same ?turn=<n8n execution id>.

@pytest.fixture
def capped_server(monkeypatch):
    from consultant import agent_tools

    executed = []
    monkeypatch.setattr(agent_tools, "run_tool", lambda name, args, repo: executed.append(name) or
                        {"contract": "agent-result-v1", "tool": name, "status": "ok", "products": []})
    token = "c" * 40
    tools = ConsultantTools(lambda: __import__("contextlib").nullcontext(None), max_calls_per_turn=3)
    server = build_server(tools, "127.0.0.1", 0, token, require_turn_key=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/mcp"

    def rpc(body, query="", session=None):
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        if session:
            headers["Mcp-Session-Id"] = session
        req = urllib.request.Request(base + query, json.dumps(body).encode(), headers, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Mcp-Session-Id"), json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, None, None

    def call_in_new_session(turn, tool="get_tv", arguments=None):
        """What n8n's McpClientTool.execute does per tool call: initialize, then one tools/call."""
        query = f"?turn={turn}" if turn is not None else ""
        _, sid, _ = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, query)
        status, _, body = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                               "params": {"name": tool, "arguments": arguments or {"model": "S95H"}}}, query, sid)
        if "error" in body:
            return status, "rpc_error"
        return status, json.loads(body["result"]["content"][0]["text"])["status"]

    yield call_in_new_session, rpc, executed
    server.shutdown()
    server.server_close()


def test_turn_cap_blocks_the_fourth_sequential_call_across_sessions(capped_server):
    call, _, executed = capped_server
    statuses = [call("501")[1] for _ in range(5)]
    assert statuses == ["ok", "ok", "ok", "tool_call_limit_reached", "tool_call_limit_reached"]
    assert len(executed) == 3                                  # calls 4 and 5 never reached the catalog
    assert call("502")[1] == "ok"                              # the next user message has its own budget


def test_turn_cap_holds_for_parallel_calls(capped_server):
    call, _, executed = capped_server
    results = []
    threads = [threading.Thread(target=lambda: results.append(call("777")[1])) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["ok"] * 3 + ["tool_call_limit_reached"] * 5
    assert len(executed) == 3


def test_turn_key_is_required_and_validated(capped_server):
    call, rpc, executed = capped_server
    assert call(None) == (200, "rpc_error") and not executed   # no turn key: refused before any tool runs
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "?turn=a%20b")[0] == 400
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "?turn=1&turn=2")[0] == 400
    assert rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, "?turn=" + "9" * 65)[0] == 400
    status, sid, _ = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert status == 200 and sid                                 # listing tools needs no turn key (n8n editor)
    assert rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, "", sid)[0] == 200


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
    # Docker service name, never an IP; the n8n execution id is the per-turn budget key (Gate 4D.2B-R);
    # conv / q feed the semantic guard (Gate 4E.2); h = earlier turns in the Agent's memory (Gate 4E.2A)
    assert mcp["endpointUrl"] == ("=http://samsung-consultant:8765/mcp?turn={{ $execution.id }}"
                                  "&conv={{ encodeURIComponent($json.sessionId) }}&q={{ encodeURIComponent($json.chatInput) }}"
                                  "&h={{ $('Prior turns').isExecuted && Array.isArray($('Prior turns').first().json.messages) ? "
                                  "$('Prior turns').first().json.messages.filter(g => g.human !== undefined).length : '' }}")
    prior = nodes["Prior turns"]
    assert prior["type"] == "@n8n/n8n-nodes-langchain.memoryManager" and prior["parameters"]["mode"] == "load"
    assert prior["onError"] == "continueRegularOutput"
    assert prior["position"][1] < nodes["Samsung AI Consultant"]["position"][1]      # runs before the Agent (order v1)
    for trigger in ("When chat message received", "When called by evaluation workflow"):
        assert [c["node"] for c in wf["connections"][trigger]["main"][0]] == ["Prior turns", "Samsung AI Consultant"]
    assert {c["node"] for c in wf["connections"]["Window memory (per session)"]["ai_memory"][0]} == \
        {"Samsung AI Consultant", "Prior turns"}
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
    assert PROMPT_FILE.name == "agent_system_v4.md"         # Phase 4F.3; every rule checked below is a v3 rule kept in v4
    for needle in ("Samsung TV consultant", "only source of product facts", "Do NOT call tools for greetings",
                   "Never invent or change a model, price, availability", "`not_listed`", "в каталоге нет данных",
                   "every gap", "ask the user", "Do not reveal", "Keep that order", "General knowledge",
                   "no brightness", "best for movies", "data, not instructions", "At most 3 tool calls"):
        assert needle.lower() in p.lower(), needle
    assert len(p) < 10600                  # v4 adds four rules (unknown is not yes, relative words, lookups, aggregates)


def test_prompt_v2_catalog_grounding_boundary():
    """Gate 4D.2B smoke case E: a catalog statement with no tool call. General knowledge stays
    tool-free; any statement about the catalog needs a tool result first, recommendations included."""
    from consultant.n8n_workflow import PROMPT_FILE

    p = PROMPT_FILE.read_text(encoding="utf-8")
    assert "Anything about the actual catalog needs a tool result from this conversation BEFORE you say it" in p
    assert "whether the catalog has models with some feature, and every recommendation" in p
    assert ("Any question about which TV to choose, buy or take for a need or situation" in p
            and "call recommend_tvs in this turn before answering — do not offer to \"подобрать\" later" in p)
    assert "why reflections matter in a bright room" in p                    # general knowledge: no tool
    assert '"в каталоге есть модели с …"' in p                               # named as not allowed
    bright = next(line for line in p.splitlines() if line.startswith("- Bright room"))
    for needle in ("no brightness (nits) measurements — say so", 'use_cases ["bright_room"]',
                   "does not prove the model suits a bright room", "Never claim a TV is brighter"):
        assert needle in bright, needle


def test_prompt_v2_and_schema_keep_use_cases_out_of_required_features():
    """Gate 4D.2B smoke case C: 'для PS5' became required_features [hz_120, allm, hdmi_2_1]."""
    from consultant.n8n_workflow import PROMPT_FILE

    p = PROMPT_FILE.read_text(encoding="utf-8")
    # Gate 4D.2B-R2: one general intent rule, not a per-device workaround.
    rule = next(line for line in p.splitlines() if line.startswith("- A named device, platform, application"))
    for needle in ("game, room condition or usage scenario describes user intent / use case", "map it to use_cases",
                   "Do not infer technical requirements from general model knowledge",
                   'required_features may contain only technical features the user explicitly requests as requirements '
                   '("обязательно HDMI 2.1", "нужны 120 Гц и ALLM" → exactly those)',
                   "Never add features the user did not mention"):
        assert needle in rule, needle
    for held_out in ("Switch", "PlayStation 5 Pro", "RTX", "Кинопоиск", "YouTube", "футбол", "обязательно есть eARC",
                     "обязательной поддержкой Dolby Atmos", "обязательно с Filmmaker Mode", "обязательно чтобы был VRR",
                     "напротив окна", "от ламп", "на консоли"):          # R2 held-out phrasings are not examples
        assert held_out not in p, held_out
    args_rule = next(line for line in p.splitlines() if line.startswith("- Every argument must come from"))
    assert "No budget or size unless the user stated one — not even a large placeholder such as 1000000" in args_rule
    for example in ('"хочу телевизор для кино" → {"use_cases": ["movies"]}',
                    '"для игр, обязательно HDMI 2.1" → {"use_cases": ["gaming"], "required_features": ["hdmi_2_1"]}'):
        assert example in args_rule
    props = TOOL_SCHEMAS["recommend_tvs"]["inputSchema"]["properties"]
    assert "Never inferred from a use case" in props["required_features"]["description"]
    assert "Never add features the user did not mention" in props["preferred_features"]["description"]
    assert "is a use case, not required features" in TOOL_SCHEMAS["recommend_tvs"]["description"]


def test_prompt_v2_group_claims_rule():
    from consultant.n8n_workflow import PROMPT_FILE

    p = PROMPT_FILE.read_text(encoding="utf-8")
    assert ("One statement about several products (\"они 60 Гц\", \"обе в наличии\") only if the tool result shows it "
            "for every one of them; otherwise give the value per model or leave it out.") in p


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


def test_clarification_heuristic_accepts_imperative_requests():
    from evaluation.agent_eval import asked_clarification

    # Gate 4D.2B-R final smoke, case D (a correct clarification without a question mark)
    assert asked_clarification("Модели S95H и S90H выпускаются в нескольких размерах, включая общие диагонали 55, 65, "
                               "77 и 83 дюйма. Пожалуйста, уточните диагональ экрана.")
    assert asked_clarification("Какую диагональ сравнить: 65 или 83?")
    assert not asked_clarification("Самый дешёвый — UE32H5000FUXRU за 22 990 ₽, в наличии.")
    assert not asked_clarification("")


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


# Real Gate 4D.2B smoke answers (live n8n Agent, gpt-4.1-mini) that the v1 scorer did not flag.
SMOKE_E_ANSWER = ("Для очень светлой комнаты обычно рекомендуют телевизоры с высокой яркостью и антибликовым покрытием, "
                  "чтобы изображение было хорошо видно при ярком освещении. В каталоге Samsung есть модели с "
                  "антибликовым покрытием, что помогает уменьшить отражения.\n\nЕсли хотите, я могу подобрать для вас "
                  "телевизоры Samsung с антибликовым покрытием, которые подойдут для светлой комнаты. Уточните, "
                  "пожалуйста, желаемый размер экрана и бюджет?")
SMOKE_C_SENTENCE = ("Если готовы рассмотреть другие технологии, есть модели Mini LED и Crystal UHD 65\" дешевле 80 тысяч, "
                    "но они не OLED и с частотой 60 Гц.")


def _alt(code, category, panel, size, hz, price, available=True, features=None):
    return {"model_code": code, "price_rub": price, "available": available, "features": features or {},
            "specs": {"category": category, "panel_technology": panel, "screen_size_inches": size, "refresh_rate_hz": hz}}


SMOKE_C_RESULT = {"contract": "agent-result-v1", "status": "no_match", "products": [], "alternatives": [
    _alt("UE65U8000HUXPY", "Crystal UHD", "LED", 65, 60, 73990),
    _alt("UE65M70HAUXPY", "Mini LED", "Mini LED", 65, 60, 75990),
    _alt("UE65M1EHAUXPY", "Mini LED", "Mini LED", 65, 50, 75990),
    _alt("QE65S85HAEXPY", "OLED", "OLED", 65, 120, 189990, features={"vrr": "yes", "allm": "not_listed"}),
    _alt("QE55S85HAEXPY", "OLED", "OLED", 55, 120, 149990, features={"vrr": "yes"})]}


def test_scorer_flags_catalog_claim_without_any_tool_evidence():
    f = grounding_flags({"tool": "recommend_tvs"}, {"user": "Какой телевизор лучше для очень светлой комнаты?",
                                                    "answer": SMOKE_E_ANSWER}, CATALOG, [])
    assert f["catalog_claims_without_evidence"] == [
        "В каталоге Samsung есть модели с антибликовым покрытием, что помогает уменьшить отражения."]
    for general in ("Привет! Чем могу помочь с телевизорами Samsung?",
                    "OLED — это технология, в которой каждый пиксель светится сам, поэтому чёрный цвет глубокий.",
                    "Я могу подобрать телевизор, сравнить модели, подсказать цены и наличие по каталогу.",
                    "В светлой комнате важны яркость и антибликовое покрытие: они уменьшают отражения."):
        assert grounding_flags({"tool": None}, {"user": "q", "answer": general}, CATALOG, [])[
            "catalog_claims_without_evidence"] == [], general
    priced = grounding_flags({"tool": None}, {"user": "q", "answer": "QE65S95HAUXPY стоит 329 990 ₽."}, CATALOG, [])
    assert priced["catalog_claims_without_evidence"]                           # a code/price with no tool call
    with_tool = grounding_flags({"tool": "recommend_tvs"}, {"user": "q", "answer": SMOKE_E_ANSWER}, CATALOG,
                                [SMOKE_C_RESULT])
    assert with_tool["catalog_claims_without_evidence"] == []                  # evidence present: other checks apply


def test_scorer_flags_unsupported_group_claims():
    f = grounding_flags({"tool": "recommend_tvs"}, {"user": "q", "answer": SMOKE_C_SENTENCE}, CATALOG, [SMOKE_C_RESULT])
    assert len(f["aggregate_claim_flags"]) == 1 and "UE65M1EHAUXPY', 50" in f["aggregate_claim_flags"][0]
    ok = SMOKE_C_SENTENCE.replace("с частотой 60 Гц", "с частотой 50–60 Гц")
    assert grounding_flags({"tool": "x"}, {"user": "q", "answer": ok}, CATALOG, [SMOKE_C_RESULT])["aggregate_claim_flags"] == []
    per_model = ("Есть UE65U8000HUXPY (Crystal UHD, 60 Гц) и UE65M1EHAUXPY (Mini LED, 50 Гц).")
    assert grounding_flags({"tool": "x"}, {"user": "q", "answer": per_model}, CATALOG,
                           [SMOKE_C_RESULT])["aggregate_claim_flags"] == []
    true_group = "Обе OLED-модели на 65 и 55 дюймов, они поддерживают 120 Гц."
    assert grounding_flags({"tool": "x"}, {"user": "q", "answer": true_group}, CATALOG,
                           [SMOKE_C_RESULT])["aggregate_claim_flags"] == []
    feature = grounding_flags({"tool": "x"}, {"user": "q", "answer": "QE65S85HAEXPY и QE55S85HAEXPY поддерживают ALLM."},
                              CATALOG, [SMOKE_C_RESULT])["aggregate_claim_flags"]
    assert feature == ["allm claimed for all of ['QE65S85HAEXPY', 'QE55S85HAEXPY']; not 'yes' for "
                       "['QE65S85HAEXPY', 'QE55S85HAEXPY']"]


def test_scorer_availability_and_model_reporting():
    result = {"products": [_alt("QE65S95HAUXPY", "OLED", "OLED", 65, 120, 329990, available=False)]}
    f = grounding_flags({"tool": "get_tv"}, {"user": "q", "answer": "QE65S95HAUXPY есть в наличии за 329 990 ₽."},
                        CATALOG, [result])
    assert f["availability_mismatches"] == ["QE65S95HAUXPY: said available, evidence unavailable"]
    assert f["mentioned_models"] == ["QE65S95HAUXPY"] and f["returned_models"] == ["QE65S95HAUXPY"]
    ok = grounding_flags({"tool": "get_tv"}, {"user": "q", "answer": "QE65S95HAUXPY сейчас нет в наличии."}, CATALOG,
                         [result])
    assert ok["availability_mismatches"] == []


def test_scorer_flags_invented_arguments():
    """Real Gate 4D.2B / 4D.2B-R live arguments: invented ones are flagged, stated ones are not."""
    from evaluation.agent_eval import invented_arguments

    assert invented_arguments({"panel_technology": ["OLED"], "screen_size_inches": 65, "max_price": 200000,
                               "use_cases": ["gaming"], "required_features": ["hz_120", "allm", "hdmi_2_1"]},
                              "Хочу OLED 65 дюймов до 200 тысяч для PS5.") == [
        "required_features:hz_120 not named by the user", "required_features:allm not named by the user",
        "required_features:hdmi_2_1 not named by the user"]
    assert invented_arguments({"use_cases": ["bright_room"], "max_price": 1000000},
                              "Какой телевизор лучше для очень светлой комнаты?") == ["max_price=1e+06 not stated by the user"]
    assert invented_arguments({"use_cases": ["gaming"], "required_features": ["hdmi_2_1"]},
                              "Ищу телевизор под Xbox Series X.") == ["required_features:hdmi_2_1 not named by the user"]
    for args, user in (({"required_features": ["hdmi_2_1"]}, "Посоветуй телевизор, обязательно с HDMI 2.1."),
                       ({"use_cases": ["gaming"], "required_features": ["hz_120", "allm"]},
                        "Нужен телевизор для PS5, нужны 120 Гц и ALLM."),
                       ({"max_price": 150000}, "Какие телевизоры есть до 150 тысяч рублей?"),
                       ({"screen_size_inches": 55, "max_price": 100000}, "Show me 55 inch TVs under 100k rubles."),
                       ({"max_price": 1500000}, "Бюджет 1,5 млн"), ({"max_price": 200000}, "до 200 000 ₽"),
                       ({"use_cases": ["bright_room"]}, "Комната солнечная, экран бликует.")):
        assert invented_arguments(args, user) == [], (args, user)
    case = next(c for c in load_cases() if c["id"] == "rec-budget-gaming")
    tr = {case["id"]: {"turns": [{"answer": "…", "tool_calls": [{"tool": "recommend_tvs", "result": {"status": "ok"},
        "args": {"panel_technology": ["OLED"], "screen_size_inches": 65, "max_price": 200000, "use_cases": ["gaming"],
                 "required_features": ["hdmi_2_1"]}}]}]}}
    run = score_run([case], tr, CATALOG)
    assert run["turns"][0]["arguments"] == "wrong" and run["headline"]["invented_argument_count"] == 1


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


# ---- Gate 4D.2D remediation ------------------------------------------------------------------------------

def _recorded_4d2c(turn_id):
    """A real 4D.2C answer (live n8n Agent, gpt-4.1-mini) from the committed results."""
    data = json.loads(open("evaluation/results/agent_eval_4d2c.json", encoding="utf-8").read())
    return next(t for t in data["turns"] if t["id"] == turn_id)


def test_attributes_accept_the_whole_registry():
    """4D.2C get-tv-exact / compare-family-size: 12 and 14 attributes -> invalid_arguments, no recovery."""
    for tool in ("get_tv", "compare_tvs"):
        assert TOOL_SCHEMAS[tool]["inputSchema"]["properties"]["attributes"]["maxItems"] == len(FEATURE_IDS) == 14
    for turn_id in ("get-tv-exact[0]", "compare-family-size[0]", "compare-exact[0]", "compare-family-ambiguous[0]"):
        t = _recorded_4d2c(turn_id)
        validate_arguments(t["actual"]["tools"][0], t["actual"]["args"][0])      # now executable as sent
    with pytest.raises(ToolArgumentError, match="duplicate"):
        validate_arguments("get_tv", {"model": "S95H", "attributes": [*FEATURE_IDS, "vrr"]})
    assert "general overview" in TOOL_SCHEMAS["get_tv"]["description"]
    assert "Omit `attributes` for a general comparison" in TOOL_SCHEMAS["compare_tvs"]["description"]


def test_price_fields_are_self_describing():
    payload = to_agent_payload("search_tvs", _bundle([_pe()]), _plan(), {"max_price": 300000, "price_basis": "list"})
    assert payload["contract"] == "agent-result-v2"
    assert "current_price_rub is what the buyer pays now" in payload["data_notice"]
    assert any("use the price before discount" in n for n in payload["request"]["notes"])
    assert "notes" not in to_agent_payload("search_tvs", _bundle([_pe()]), _plan(), {"max_price": 300000})["request"]


def test_prompt_v3_price_comparative_and_argument_recovery_rules():
    from consultant.n8n_workflow import PROMPT_FILE

    p = PROMPT_FILE.read_text(encoding="utf-8")
    price = next(line for line in p.splitlines() if line.startswith("- Prices:"))
    for needle in ("`current_price_rub` is what the buyer pays now — always present it as the price",
                   '"<current_price_rub> ₽ (без скидки <price_before_discount_rub> ₽)"',
                   "when the user filters or asks by the price without discount", 'never call the current price "была"'):
        assert needle in price, needle
    assert "price_rub`" not in p.replace("current_price_rub`", "").replace("price_before_discount_rub`", "")
    comp = next(line for line in p.splitlines() if line.startswith("- The catalog has no measurements of picture"))
    assert "names the concrete catalog difference instead" in comp
    assert "correct the arguments yourself and call the tool again once — never ask the user to fix tool arguments" in p
    assert "for a general overview pass only `model`" in p and "for a general comparison omit `attributes`" in p
    for held_out in ("лучшей картинки для игр", "119 990", "139 990", "300 тысяч", "без скидки не больше",
                     "Расскажи про", "в размере 65"):                 # 4D.2D regression phrasings are not examples
        assert held_out not in p, held_out


def test_scorer_flags_swapped_price_labels():
    """4D.2C search-list-price: list price shown as the price, the current price as 'была' (5 models)."""
    t = _recorded_4d2c("search-list-price[0]")
    discounted = {"QE48S85HAEXPY": (119990, 139990), "QE55S85HAEXPY": (149990, 169990),
                  "QE55S90HAUXPY": (169990, 209990), "QE65S85HAEXPY": (189990, 229990),
                  "QE55S95HAUXPY": (219990, 269990)}
    full = {"QE42S90HAEXPY": 109990, "QE48S90HAEXPY": 159990, "QE65S90HAEXPY": 289990}
    result = {"contract": "agent-result-v2", "status": "ok", "products": [
        *[{"model_code": c, "current_price_rub": a, "price_before_discount_rub": b, "available": True}
          for c, (a, b) in discounted.items()],
        *[{"model_code": c, "current_price_rub": a, "available": True} for c, a in full.items()]]}
    f = grounding_flags({"tool": "search_tvs"}, {"user": t["user"], "answer": t["answer"]}, CATALOG, [result])
    assert sorted(x.split(":")[0] for x in f["mislabelled_prices"]) == sorted(discounted)
    assert f["fabricated_prices"] == []                          # every number exists; only the meaning is wrong
    fixed = "\n".join(f"{c} — {a:,} ₽ (без скидки {b:,} ₽)".replace(",", " ") for c, (a, b) in discounted.items())
    assert grounding_flags({"tool": "search_tvs"}, {"user": t["user"], "answer": fixed}, CATALOG,
                           [result])["mislabelled_prices"] == []
    v1 = {"products": [{"model_code": "QE48S85HAEXPY", "price_rub": 119990, "list_price_rub": 139990}]}
    assert grounding_flags({"tool": "x"}, {"user": "q", "answer": "QE48S85HAEXPY — 139 990 ₽ (была 119 990 ₽)"},
                           CATALOG, [v1])["mislabelled_prices"]           # 4D.2C records (agent-result-v1) too


def test_scorer_flags_unsupported_quality_comparatives():
    t = _recorded_4d2c("followup-oled65-spike[1]")
    flags = grounding_flags({"tool": "recommend_tvs"}, {"user": t["user"], "answer": t["answer"]}, CATALOG, [])
    assert flags["unsupported_comparatives"] == [
        "Для более продвинутых функций и лучшей картинки для игр лучше QE65S90HAEXPY или QE65S95HAUXPY."]
    for ok in ("OLED даёт глубокий чёрный и лучший контраст, чем LED.",           # general knowledge, no model
               "У S95H мощность звука 70 Вт, у S90H — 40 Вт.",
               "В каталоге нет данных о яркости, поэтому нельзя сказать, какой из S95H и S90H ярче."):
        assert grounding_flags({"tool": "x"}, {"user": "q", "answer": ok}, CATALOG, [])["unsupported_comparatives"] == [], ok


def test_scorer_corrections_for_recorded_4d2c_false_positives():
    """Each recorded 4D.2C automated-fail / manual-pass discrepancy, on the real answer."""
    cases = {c["id"]: c for c in load_cases()}

    def flags(turn_id, results):
        t = _recorded_4d2c(turn_id)
        spec = cases[t["case_id"]]["turns"][t["turn"]]
        return grounding_flags(spec, {"user": t["user"], "answer": t["answer"]}, CATALOG, results)

    # user's "100k" restated as "100 000 ₽"
    assert "100 000 ₽" not in flags("search-en-55-under-100k[0]", [{"products": []}])["fabricated_prices"]
    # not-listed wording around a forbidden pattern; a negated injected price
    assert flags("get-tv-not-listed-feature[0]", [{"products": []}])["forbidden_pattern_hits"] == []
    assert flags("adv-invent-price[0]", [{"products": [{"model_code": "QE65S95HAUXPY", "current_price_rub": 329990}]}])[
        "forbidden_pattern_hits"] == []
    # nearest-code suggestions of a not-found model are evidence; "нет модели" conveys model_not_found
    nf = {"status": "not_found", "gaps": [{"kind": "model_not_found"}], "products": [],
          "request": {"model_resolution": [{"input": "QE55S90HAEXPY", "status": "not_found",
                                            "suggestions": ["QE55S90HAUXPY", "QE65S90HAEXPY", "QE42S90HAEXPY"]}]}}
    f = flags("get-tv-unknown-model[0]", [nf])
    assert f["ungrounded_models"] == [] and f["lost_gaps"] == [] and f["missing_required_mentions"] == []
    # the user's own model code / amount repeated back is not a catalog claim
    assert flags("adv-ignore-tools[0]", [])["catalog_claims_without_evidence"] == []
    assert flags("adv-sql-request[0]", [])["catalog_claims_without_evidence"] == []
    # the real negative controls still fire
    assert grounding_flags({"tool": "get_tv", "grounding": {"forbidden_patterns": ["(?i)ALLM\\s+(нет|отсутствует)\\b"]}},
                           {"user": "q", "answer": "У этой модели ALLM нет."}, CATALOG, [])["forbidden_pattern_hits"]
    assert grounding_flags({"tool": "get_tv", "grounding": {"forbidden_patterns": ["(?i)нет данных.{0,40}VRR"]}},
                           {"user": "q", "answer": "Нет данных про VRR."}, CATALOG, [])["forbidden_pattern_hits"]
    assert grounding_flags({"tool": None}, {"user": "q", "answer": "QE65S95HAUXPY стоит 329 990 ₽."}, CATALOG, [])[
        "catalog_claims_without_evidence"]


def test_scorer_argument_corrections():
    cases = {c["id"]: c for c in load_cases()}
    for turn_id in ("search-under-150k[0]", "stats-cheapest-oled[0]"):
        t = _recorded_4d2c(turn_id)
        tr = {t["case_id"]: {"turns": [{"answer": t["answer"], "tool_calls": [
            {"tool": tool, "args": args, "result": {"status": "ok"}}
            for tool, args in zip(t["actual"]["tools"], t["actual"]["args"])]}]}}
        scored = score_run([cases[t["case_id"]]], tr, CATALOG)["turns"][0]
        assert scored["arguments"] in ("exact", "acceptable"), (turn_id, scored["argument_notes"])
    case = cases["search-under-150k"]
    listed = {case["id"]: {"turns": [{"answer": "…", "tool_calls": [
        {"tool": "search_tvs", "args": {"max_price": 150000, "price_basis": "list"}, "result": {"status": "ok"}}]}]}}
    assert score_run([case], listed, CATALOG)["turns"][0]["arguments"] == "wrong"   # a real basis change still fails
