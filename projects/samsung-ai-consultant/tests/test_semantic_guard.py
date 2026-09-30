"""Gate 4E.2 semantic guard: rules, fail-safe, runtime integration point, MCP transport, replay."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from consultant import agent_tools, semantic_guard
from consultant.agent_tools import ConsultantTools, TurnBudget
from consultant.features import FEATURES
from consultant.mcp_server import (
    ConversationStore, guard_context, handle_message, make_handler, parse_guard_params, parse_target,
)
from consultant.semantic_guard import GuardResult, MessageEvidence, guard_tool_arguments
from evaluation import guard_replay

ROOT = Path(__file__).resolve().parents[1]


def guard(tool, args, *conversation, act=True) -> GuardResult:
    return guard_tool_arguments(tool, args, list(conversation), act)


# ---- brief tests A-I --------------------------------------------------------------------------

def test_A_implicit_gaming_does_not_keep_hdmi_2_1_required():
    g = guard("recommend_tvs", {"use_cases": ["gaming"], "required_features": ["hdmi_2_1"]}, "Посоветуй телевизор для PS5.")
    assert g.status == "modified" and g.arguments == {"use_cases": ["gaming"]}
    assert [(a.action, a.argument, a.value, a.reason) for a in g.actions] == [
        ("removed", "required_features", "hdmi_2_1", "required_feature_not_mentioned")]


def test_A_gaming_ranking_signal_is_kept_when_the_agent_omitted_it():
    g = guard("recommend_tvs", {"required_features": ["hdmi_2_1"]}, "для PS5")
    assert g.arguments == {"use_cases": ["gaming"]}


def test_B_explicit_hdmi_2_1_stays_required():
    args = {"use_cases": ["gaming"], "required_features": ["hdmi_2_1"]}
    g = guard("recommend_tvs", args, "для PS5, обязательно HDMI 2.1")
    assert g.status == "unchanged" and g.arguments == args


def test_C_invented_price_is_removed():
    g = guard("recommend_tvs", {"use_cases": ["movies"], "max_price": 150000}, "хочу телевизор для кино с хорошей картинкой")
    assert g.arguments == {"use_cases": ["movies"]} and g.removed[0].reason == "unsupported_price_value"


@pytest.mark.parametrize("text, args", [
    ("до 150 тысяч", {"max_price": 150000}),
    ("до 150к", {"max_price": 150000}),
    ("бюджет 150 000 ₽", {"max_price": 150000}),
    ("до 0,15 млн", {"max_price": 150000}),
    ("under 100k rubles", {"max_price": 100000}),
    ("от 100 до 150 тысяч", {"min_price": 100000, "max_price": 150000}),     # 4E.1 parser misses the minimum
    ("бюджет сто тысяч", {"max_price": 100000}),                              # spelled number: left alone
    ("OLED с ценой без скидки не больше 300 тысяч", {"max_price": 300000, "price_basis": "list"}),
])
def test_D_explicit_price_is_kept(text, args):
    g = guard("search_tvs", args, text)
    assert g.status == "unchanged" and g.arguments == args


def test_price_basis_goes_with_its_removed_price():
    g = guard("search_tvs", {"max_price": 150000, "price_basis": "list"}, "покажи OLED")
    assert g.arguments == {} and {a.argument for a in g.removed} == {"max_price", "price_basis"}


@pytest.mark.parametrize("text", ["нужен телевизор 120 Гц", "не меньше 120 Гц", "120 Гц обязательно"])
def test_E_explicit_refresh_rate_is_kept(text):
    assert guard("recommend_tvs", {"min_refresh_rate_hz": 120}, text).status == "unchanged"


@pytest.mark.parametrize("text", ["телевизор для игр", "для PS5", "чтобы картинка была плавной"])
def test_F_gaming_does_not_imply_a_refresh_rate(text):
    g = guard("recommend_tvs", {"min_refresh_rate_hz": 120}, text)
    assert "min_refresh_rate_hz" not in g.arguments and g.removed[0].reason == "unsupported_refresh_rate_value"


def test_G_thin_wall_survives():
    args = {"screen_size_inches": 55, "use_cases": ["thin_wall"]}
    assert guard("recommend_tvs", args, "Нужен тонкий телевизор 55 дюймов, чтобы повесить на стену.").arguments == args
    g = guard("recommend_tvs", {"use_cases": ["thin_wall"], "max_price": 90000}, "тонкий телевизор на стену")
    assert g.arguments == {"use_cases": ["thin_wall"]}


def test_H_compact_survives():
    args = {"max_screen_size_inches": 32, "use_cases": ["compact"]}
    assert guard("recommend_tvs", args, "Компактный телевизор на кухню, не больше 32 дюймов.").arguments == args
    g = guard("recommend_tvs", {"use_cases": ["compact"], "max_price": 50000}, "компактный телевизор на кухню")
    assert g.arguments == {"use_cases": ["compact"]}


@pytest.mark.parametrize("tool, args, text", [
    ("compare_tvs", {"models": ["QE65S95HAUXPY", "QE65S90HAEXPY"], "screen_size_inches": 65},
     "Сравни QE65S95HAUXPY и QE65S90HAEXPY."),
    ("get_tv", {"model": "S95H", "screen_size_inches": 55}, "Сколько стоит S95H на 55 дюймов?"),
    ("search_tvs", {"min_screen_size_inches": 55}, "не меньше 55 дюймов"),
    ("search_tvs", {"screen_size_inches": 65}, "65-дюймовый OLED"),
])
def test_I_stated_or_model_code_size_is_kept(tool, args, text):
    assert guard(tool, args, text).status == "unchanged"


def test_invented_size_is_removed():
    g = guard("recommend_tvs", {"min_screen_size_inches": 65}, "Хочу большой телевизор в гостиную.")
    assert g.arguments == {} and g.removed[0].reason == "unsupported_size_value"


@pytest.mark.parametrize("text, feature", [
    ("нужен VRR", "vrr"), ("хочу VRR", "vrr"), ("обязательно HDMI 2.1", "hdmi_2_1"), ("с HDMI2.1", "hdmi_2_1"),
    ("120 Гц обязательно", "hz_120"), ("144 Гц", "hz_120"), ("чтобы был ALLM", "allm"), ("FreeSync Premium Pro", "freesync_premium_pro"),
    ("Dolby Atmos обязательно", "dolby_atmos"), ("eARC для саундбара", "earc"), ("антибликовое покрытие", "anti_glare"),
    ("режим Filmmaker", "filmmaker_mode"), ("мощность звука от 40 Вт", "sound_power_w"), ("крепление VESA", "vesa"),
])
def test_explicit_feature_requirements_are_kept(text, feature):
    assert guard("recommend_tvs", {"required_features": [feature]}, text).status == "unchanged"


def test_every_registry_feature_has_a_mention_pattern():
    assert set(semantic_guard.FEATURE_MENTIONS) == set(FEATURES)


def test_implied_feature_requirement_is_removed():
    g = guard("recommend_tvs", {"required_features": ["vrr", "allm"]}, "хороший игровой режим")
    assert "required_features" not in g.arguments and g.arguments["use_cases"] == ["gaming"]


def test_partial_feature_list_keeps_the_stated_one():
    g = guard("recommend_tvs", {"required_features": ["hdmi_2_1", "vrr"]}, "для PS5, обязательно HDMI 2.1")
    assert g.arguments["required_features"] == ["hdmi_2_1"]


# ---- conversation (carried constraints) -------------------------------------------------------

def test_known_4d2e_followup_invention_is_removed_and_carried_constraints_kept():
    args = {"panel_technology": ["OLED"], "screen_size_inches": 65, "use_cases": ["gaming"], "max_price": 150000}
    g = guard("recommend_tvs", args, "Покажи OLED 65 дюймов.", "Какой из них лучше для игр?", "А подешевле?")
    assert g.arguments == {"panel_technology": ["OLED"], "screen_size_inches": 65, "use_cases": ["gaming"]}


def test_carried_budget_and_feature_are_kept():
    assert guard("recommend_tvs", {"max_price": 200000, "use_cases": ["gaming"]},
                 "Хочу OLED до 200 тысяч.", "А какой лучше для игр?").status == "unchanged"
    assert guard("recommend_tvs", {"required_features": ["hdmi_2_1"]},
                 "Обязательно HDMI 2.1.", "А подешевле есть?").status == "unchanged"


def test_report_only_mode_changes_nothing():
    args = {"use_cases": ["gaming"], "required_features": ["hdmi_2_1"]}
    g = guard("recommend_tvs", args, "для PS5", act=False)
    assert g.status == "report_only" and g.arguments is args and g.actions[0].action == "would_remove"


# ---- never invents; untouched arguments -------------------------------------------------------

@pytest.mark.parametrize("tool, args", [
    ("recommend_tvs", {"panel_technology": ["OLED"], "use_cases": ["gaming"]}),       # an invented panel is not the guard's call
    ("search_tvs", {"resolution": ["4K"], "availability": "available", "sort": "price_asc", "limit": 5}),
    ("get_catalog_stats", {"stat": "cheapest", "category": ["OLED"]}),
    ("get_tv", {"model": "QE65S95HAUXPY", "attributes": ["vrr"], "question": "AirPlay"}),
    ("recommend_tvs", {"preferred_features": ["sound_power_w"], "use_cases": ["sound"]}),
])
def test_non_numeric_arguments_pass_through(tool, args):
    g = guard(tool, args, "для PS5")
    assert g.status == "unchanged" and g.arguments == args


def test_guard_never_adds_hard_constraints():
    for tool, args, text in [("recommend_tvs", {}, "OLED 65 дюймов до 150 тысяч 120 Гц с HDMI 2.1"),
                             ("recommend_tvs", {"max_price": 99}, "для PS5 в светлой комнате, 4K OLED")]:
        g = guard(tool, args, text)
        assert set(g.arguments) - {"use_cases"} <= set(args)


def test_use_cases_are_merged_never_replaced():
    g = guard("recommend_tvs", {"use_cases": ["thin_wall", "compact"], "max_price": 1}, "для PS5 и кино")
    assert g.arguments["use_cases"] == ["thin_wall", "compact", "gaming", "movies"]


# ---- J: fail-safe -----------------------------------------------------------------------------

@pytest.mark.parametrize("conversation, detail", [(None, "no_conversation_context"), ([], "no_conversation_context")])
def test_no_context_is_skipped(conversation, detail):
    args = {"required_features": ["hdmi_2_1"]}
    g = guard_tool_arguments("recommend_tvs", args, conversation)
    assert g.status == "skipped" and g.arguments is args and g.detail == detail


def test_original_invalid_arguments_are_left_to_the_tool_boundary():
    args = {"max_price": "150000", "sql": "DROP"}
    g = guard("recommend_tvs", args, "для PS5")
    assert g.status == "skipped" and g.arguments is args


def test_J_internal_error_falls_back_to_original(monkeypatch):
    monkeypatch.setattr(semantic_guard, "_rules", lambda *a: 1 / 0)
    args = {"required_features": ["hdmi_2_1"]}
    g = guard("recommend_tvs", args, "для PS5")
    assert g.status == "fallback" and g.arguments is args and g.detail == "guard_error:ZeroDivisionError"


def test_J_guarded_arguments_that_fail_validation_fall_back(monkeypatch):
    monkeypatch.setattr(semantic_guard, "_rules",
                        lambda t, a, c: ({**a, "min_screen_size_inches": 80, "max_screen_size_inches": 50},
                                         (semantic_guard.GuardAction("x", "removed", 1, "test"),)))
    args = {"use_cases": ["gaming"]}
    g = guard("recommend_tvs", args, "для PS5")
    assert g.status == "fallback" and g.arguments is args and g.detail.startswith("guarded_arguments_invalid")


class RecordingTools(ConsultantTools):
    """ConsultantTools with ``run_tool`` replaced by a recorder (no DB)."""


@pytest.fixture
def recorded(monkeypatch):
    seen = []

    def fake_run_tool(name, arguments, repo):
        seen.append((name, arguments))
        return {"contract": "agent-result-v2", "tool": name, "status": "ok", "request": {}, "products": []}
    monkeypatch.setattr(agent_tools, "run_tool", fake_run_tool)
    return seen


def test_runtime_integration_point_applies_guard_before_the_tool_boundary(recorded):
    tools = ConsultantTools.for_repository(object())
    conv = (MessageEvidence.from_text("Посоветуй телевизор для PS5."),)
    payload = tools.call("recommend_tvs", {"use_cases": ["gaming"], "required_features": ["hdmi_2_1"]}, "t1", conv)
    assert recorded == [("recommend_tvs", {"use_cases": ["gaming"]})]
    assert payload["request"]["not_applied"]["constraints"] == [{"argument": "required_features", "value": "hdmi_2_1"}]


def test_runtime_without_context_is_the_4d_path(recorded):
    args = {"use_cases": ["gaming"], "required_features": ["hdmi_2_1"]}
    payload = ConsultantTools.for_repository(object()).call("recommend_tvs", args, "t1")
    assert recorded == [("recommend_tvs", args)] and "not_applied" not in payload["request"]


def test_J_runtime_survives_a_crashing_guard(recorded, monkeypatch, caplog):
    def boom(*a, **k):
        raise RuntimeError("guard exploded")
    monkeypatch.setattr(semantic_guard, "guard_tool_arguments", boom)
    args = {"required_features": ["hdmi_2_1"]}
    with caplog.at_level(logging.ERROR):
        payload = ConsultantTools.for_repository(object()).call(
            "recommend_tvs", args, "t1", (MessageEvidence.from_text("для PS5"),))
    assert payload["status"] == "ok" and recorded == [("recommend_tvs", args)]
    assert "semantic guard failed" in caplog.text


def test_guard_decisions_are_logged_without_user_text(recorded, caplog):
    with caplog.at_level(logging.INFO, logger="consultant.agent_tools"):
        ConsultantTools.for_repository(object()).call(
            "recommend_tvs", {"max_price": 150000}, "t1", (MessageEvidence.from_text("секретная фраза для кино"),))
    assert "unsupported_price_value" in caplog.text and "секретная" not in caplog.text


def test_message_evidence_keeps_no_text():
    e = MessageEvidence.from_text("Меня зовут Иван, нужен OLED до 150 тысяч")
    assert "Иван" not in repr(e) and 150000.0 in e.numbers


# ---- MCP transport ----------------------------------------------------------------------------

class StubTools:
    def __init__(self):
        self.calls = []
        self.budget = TurnBudget(3)

    def call(self, name, arguments, turn_id=None, **guard_kwargs):
        self.calls.append((name, arguments, turn_id, guard_kwargs))
        return {"status": "ok"}


CALL = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "recommend_tvs", "arguments": {}}}


def test_parse_guard_params():
    assert parse_guard_params("/mcp?turn=5&conv=abc-1&q=%D0%B4%D0%BB%D1%8F%20PS5") == ("abc-1", "для PS5")
    assert parse_guard_params("/mcp?turn=5") == (None, None)
    assert parse_guard_params("/mcp?turn=5&conv=bad key!&q=" + "x" * 1001) == (None, None)
    assert parse_target("/mcp?turn=5&conv=a&q=x") == ("/mcp", "5", True)        # turn parsing unchanged


def test_without_q_the_tool_call_shape_is_unchanged():
    tools = StubTools()
    handle_message(tools, CALL, "s1", "5", True, lambda: guard_context(ConversationStore(), "5", None, None))
    assert tools.calls == [("recommend_tvs", {}, "turn:5", {})]


def test_q_with_conv_acts_and_q_alone_is_report_only():
    tools, store = StubTools(), ConversationStore()
    handle_message(tools, CALL, "s1", "5", True, lambda: guard_context(store, "5", "c1", "для PS5"))
    handle_message(tools, CALL, "s1", "6", True, lambda: guard_context(store, "6", None, "для PS5"))
    (_, _, _, kw1), (_, _, _, kw2) = tools.calls
    assert kw1["act"] is True and len(kw1["conversation"]) == 1
    assert kw2["act"] is False


def test_conversation_store_one_entry_per_turn_window_and_isolation():
    store = ConversationStore(window=3)
    assert len(store.record("c", "t1", "Покажи OLED 65 дюймов.")) == 1
    assert len(store.record("c", "t1", "Покажи OLED 65 дюймов.")) == 1          # several calls of one turn
    for i in range(2, 6):
        conv = store.record("c", f"t{i}", f"сообщение {i}")
    assert len(conv) == 3
    assert len(store.record("other", "t9", "для PS5")) == 1


def test_conversation_store_ttl():
    store = ConversationStore(ttl=-1)
    store.record("c", "t1", "до 200 тысяч")
    assert len(store.record("c", "t2", "а подешевле?")) == 1


def test_guard_context_never_raises(monkeypatch):
    class Broken(ConversationStore):
        def record(self, *a):
            raise RuntimeError("x")
    assert guard_context(Broken(), "5", "c", "для PS5") == (None, True)


def test_request_log_redacts_the_user_message(caplog):
    handler = make_handler(StubTools(), "t" * 40, None, frozenset())
    h = handler.__new__(handler)
    with caplog.at_level(logging.INFO, logger="consultant.mcp_server"):
        h.log_message('"%s" %s %s', "POST /mcp?turn=5&conv=c1&q=%D1%81%D0%B5%D0%BA%D1%80%D0%B5%D1%82 HTTP/1.1", "200", "-")
    assert "q=[redacted]" in caplog.text and "%D1%81" not in caplog.text and "turn=5" in caplog.text


# ---- replay and isolation ---------------------------------------------------------------------

@pytest.fixture(scope="module")
def replay():
    return guard_replay.run(out=None)


def test_frozen_4d2e_replay(replay):
    r = replay["replay_4d2e"]
    assert r["passed"] and r["tool_calls"] == 35 and r["modified"] == 2 and r["unchanged"] == 33
    assert not r["unexpected_modifications"] and not r["validation_failures"] and not r["invented_by_guard"]


def test_supplementary_replay(replay):
    s = replay["supplementary"]
    assert s["failed"] == [] and s["invented_by_guard"] == [] and s["validation_failures"] == []


def test_committed_replay_result_is_current(replay):
    committed = json.loads((ROOT / "evaluation" / "results" / "guard_replay_4e2.json").read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(replay, ensure_ascii=False))


@pytest.mark.parametrize("module", ["planning", "router", "retrieval", "ranking", "evidence", "features",
                                    "catalog_repository", "agent_payload", "n8n_workflow"])
def test_pipeline_modules_do_not_use_the_guard(module):
    source = (ROOT / "consultant" / f"{module}.py").read_text(encoding="utf-8")
    assert "semantic_guard" not in source and "query_semantics" not in source
