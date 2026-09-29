"""Phase 4D Agent evaluation: dataset contract, transcript scoring and deterministic grounding checks.

Input to scoring is a *normalized transcript* per case (one entry per user turn):

    {"case_id": ..., "turns": [{"user": ..., "answer": ...,
                               "tool_calls": [{"tool": "search_tvs", "args": {...}, "result": {...}}]}]}

``from_n8n_agent_output`` builds a turn from the n8n AI Agent output item (``output`` +
``intermediateSteps``, tool names prefixed by the MCP node name). That adapter is provisional
until Gate 4D.2 produces real executions.

Everything here is deterministic. Grounding checks are *automatic flags*: they are reported
per case and must be confirmed by a reviewer (``manual_review`` lists what only a human can
judge). Aggregate metrics never replace the per-case table.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from consultant.agent_tools import TOOL_SCHEMAS, ToolArgumentError, validate_arguments

CASES = Path(__file__).parent / "agent_cases.json"
FAMILIES = ("no_tool", "search", "get_tv", "compare", "recommend", "stats", "adversarial", "followup")
HEADLINE_EXCLUDED = ("followup",)
TURN_KEYS = ("user", "tool", "args", "acceptable_args", "forbidden_args", "acceptable_tools", "clarification",
             "offline", "grounding")
GROUNDING_KEYS = ("must_mention_any", "must_mention_all", "forbidden_patterns", "manual_review")
OFFLINE_KEYS = ("status", "include_codes", "exact_codes", "gap_kinds", "gap_kinds_any", "features", "confidence",
                "has_alternatives")
CLARIFICATION = ("required", "forbidden", "optional")
RETRY_STATUSES = ("invalid_arguments",)

MODEL_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,3}\d{2,3}[A-Z][A-Z0-9]{4,11})(?![A-Za-z0-9])")
MONEY = re.compile(r"(?<![\d.,])(\d{1,3}(?:[\s  ]\d{3})+|\d{4,})(?:[.,]\d+)?\s*(₽|руб\w*|р\.|rub\b|RUB)", re.I)
UNIT_NUMBER = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s*(Вт|W\b|Гц|Hz|дюйм\w*|\"|″|см\b|мм\b|кг\b|нит\w*|nits)", re.I)
QUESTION_TAIL = re.compile(r"\?\s*\W*$")

# Answer phrases that preserve each gap kind (Russian first; English for English answers).
GAP_PHRASES = {
    "model_not_found": ("не найден", "нет в каталоге", "отсутствует в каталоге", "не нашл", "нет такой", "не представлен",
                        "not found", "not in the catalog"),
    "not_in_catalog_domain": ("ярк", "нит", "nits", "brightness"),
    "no_product_satisfies": ("нет подходящ", "не нашл", "не найд", "нет моделей", "нет ни одного", "таких нет",
                             "ничего не", "нет oled", "no model", "nothing"),
    "attribute_not_listed_for_product": ("нет данных", "не указ", "нет информации", "неизвестн", "не содержит",
                                         "не приводит", "не упомина", "no data", "not listed"),
    "attribute_absence_unverified": ("нет данных", "не указ", "нет информации", "неизвестн", "не содержит",
                                     "не упомина", "не удалось подтверд", "no data", "not listed"),
    "family_size_ambiguous": ("диагонал", "размер", "дюйм", "size"),
}
FEATURE_WORDS = {
    "vrr": ("vrr", "variable refresh"), "allm": ("allm",), "freesync_premium": ("freesync",),
    "freesync_premium_pro": ("freesync premium pro",), "game_bar": ("game bar",), "hdmi_2_1": ("hdmi 2.1",),
    "earc": ("earc",), "anti_glare": ("антиблик", "anti-glare", "anti reflection"), "filmmaker_mode": ("filmmaker",),
    "dolby_atmos": ("atmos",),
}
NEGATION = re.compile(r"(?i)\bне\b|\bнет\b|без\b|отсутств|no data|not listed|неизвестн")


class DatasetError(ValueError):
    pass


# ---- dataset -------------------------------------------------------------------------------------

def load_cases(path: Path = CASES) -> list:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data["cases"]
    validate_cases(cases)
    return cases


def validate_cases(cases: list) -> None:
    """Closed keys, known tools/families, and every expected argument set must pass the same
    validation the Python tool boundary applies (so a 'correct' Agent call is executable)."""
    ids = set()
    for c in cases:
        if c["id"] in ids:
            raise DatasetError(f"duplicate case id {c['id']}")
        ids.add(c["id"])
        unknown = set(c) - {"id", "family", "turns", "requires_injection_double"}
        if unknown or c.get("family") not in FAMILIES or not c.get("turns"):
            raise DatasetError(f"{c['id']}: bad case structure {sorted(unknown)}")
        for i, t in enumerate(c["turns"]):
            where = f"{c['id']}[{i}]"
            if set(t) - set(TURN_KEYS) or not isinstance(t.get("user"), str) or "tool" not in t:
                raise DatasetError(f"{where}: bad turn keys {sorted(set(t) - set(TURN_KEYS))}")
            for tool in [t["tool"], *t.get("acceptable_tools", [])]:
                if tool is not None and tool not in TOOL_SCHEMAS:
                    raise DatasetError(f"{where}: unknown tool {tool}")
            if t.get("clarification", "optional") not in CLARIFICATION:
                raise DatasetError(f"{where}: bad clarification")
            if set(t.get("grounding", {})) - set(GROUNDING_KEYS) or set(t.get("offline", {})) - set(OFFLINE_KEYS):
                raise DatasetError(f"{where}: bad grounding/offline keys")
            if t["tool"] is None and (t.get("args") or t.get("offline")):
                raise DatasetError(f"{where}: no-tool turn with arguments")
            for args in [t.get("args")] + t.get("acceptable_args", []):
                if args is None:
                    continue
                if t["tool"] is None:
                    raise DatasetError(f"{where}: arguments without a tool")
                try:
                    validate_arguments(t["tool"], args)
                except ToolArgumentError as e:
                    raise DatasetError(f"{where}: expected arguments invalid: {e}") from None
            for pattern in t.get("grounding", {}).get("forbidden_patterns", []):
                re.compile(pattern)


# ---- n8n adapter ----------------------------------------------------------------------------------

def _strip_prefix(name: str) -> str:
    for tool in TOOL_SCHEMAS:
        if name == tool or name.endswith("_" + tool):
            return tool
    return name


def _observation_payload(obs) -> Optional[dict]:
    """The MCP content array (or its string form) -> the tool payload dict, when recognizable."""
    if isinstance(obs, dict) and "contract" in obs:
        return obs
    if isinstance(obs, list):
        for part in obs:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, dict):
                return text
            if isinstance(text, str):
                p = _observation_payload(text)
                if p:
                    return p
        return None
    if isinstance(obs, str):
        decoder = json.JSONDecoder()
        for i, ch in enumerate(obs):
            if ch not in "[{":
                continue
            try:
                value, _ = decoder.raw_decode(obs[i:])
            except json.JSONDecodeError:
                continue
            found = _observation_payload(value)
            if found:
                return found
    return None


def from_n8n_agent_output(user: str, item: dict) -> dict:
    """Provisional: n8n AI Agent output item (``returnIntermediateSteps``) -> normalized turn."""
    calls = []
    for step in item.get("intermediateSteps") or []:
        action = step.get("action") or {}
        args = action.get("toolInput")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass
        calls.append({"tool": _strip_prefix(str(action.get("tool", ""))), "args": args,
                      "result": _observation_payload(step.get("observation"))})
    return {"user": user, "answer": item.get("output") or "", "tool_calls": calls}


# ---- tool-selection scoring -----------------------------------------------------------------------

def _norm(v):
    if isinstance(v, list):
        return tuple(sorted(_norm(x) for x in v))
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str):
        return v.strip().casefold()
    return v


def args_match(expected: dict, actual: dict) -> bool:
    if not isinstance(actual, dict):
        return False
    return all(k in actual and _norm(actual[k]) == _norm(v) for k, v in expected.items())


def asked_clarification(answer: str) -> bool:
    """Heuristic: the answer ends with a question (reviewed manually in the report)."""
    tail = answer.strip()[-300:]
    return bool(QUESTION_TAIL.search(tail)) or "?" in tail.splitlines()[-1] if tail else False


@dataclass
class TurnScore:
    case_id: str
    turn: int
    family: str
    expected_tool: Optional[str]
    actual_tools: list
    selection: str                  # exact | acceptable | wrong
    arguments: str                  # exact | acceptable | wrong | n/a
    argument_notes: list
    calls: int
    unnecessary_calls: int
    clarification_expected: str
    clarification_asked: bool
    clarification_ok: bool
    grounding: dict = field(default_factory=dict)


def score_turn(case: dict, index: int, spec: dict, turn: dict, catalog_codes: frozenset,
               session_results: list) -> TurnScore:
    calls = turn.get("tool_calls", [])
    tools = [c["tool"] for c in calls]
    expected = spec["tool"]
    acceptable = spec.get("acceptable_tools", [])
    first = tools[0] if tools else None
    if first == expected:
        selection = "exact"
    elif first in acceptable:
        selection = "acceptable"
    else:
        selection = "wrong"

    notes, arguments = [], "n/a"
    target = next((c for c in calls if c["tool"] == expected), None) if expected else None
    if target is not None and spec.get("args") is not None:
        valid = [c for c in calls if c["tool"] == expected
                 and (c.get("result") or {}).get("status") not in RETRY_STATUSES]
        target = valid[0] if valid else target
        args = target.get("args") or {}
        forbidden = [k for k in spec.get("forbidden_args", []) if k in args]
        if forbidden:
            notes.append(f"forbidden args sent: {forbidden}")
        if args_match(spec["args"], args) and not forbidden:
            arguments = "exact"
        elif any(args_match(a, args) for a in spec.get("acceptable_args", [])) and not forbidden:
            arguments = "acceptable"
        else:
            arguments = "wrong"
            notes.append(f"expected {spec['args']}, got {args}")

    retries = sum(1 for c in calls if (c.get("result") or {}).get("status") in RETRY_STATUSES)
    needed = 1 if expected is not None or (first is not None and first in acceptable) else 0
    unnecessary = max(0, len(calls) - retries - needed)

    answer = turn.get("answer", "")
    expected_clar = spec.get("clarification", "optional")
    asked = asked_clarification(answer)
    clar_ok = expected_clar == "optional" or (expected_clar == "required") == asked
    score = TurnScore(case["id"], index, case["family"], expected, tools, selection, arguments, notes, len(calls),
                      unnecessary, expected_clar, asked, clar_ok)
    score.grounding = grounding_flags(spec, turn, catalog_codes, session_results)
    return score


# ---- grounding ----------------------------------------------------------------------------------------

def _evidence_prices(results: list) -> set:
    out = set()
    for r in results:
        for p in [*(r.get("products") or []), *(r.get("alternatives") or [])]:
            for k in ("price_rub", "list_price_rub"):
                if isinstance(p.get(k), (int, float)):
                    out.add(int(round(p[k])))
    return out


def _evidence_codes(results: list) -> set:
    return {p.get("model_code") for r in results for p in [*(r.get("products") or []), *(r.get("alternatives") or [])]}


def _features_by_code(results: list) -> dict:
    out: dict = {}
    for r in results:
        for p in [*(r.get("products") or []), *(r.get("alternatives") or [])]:
            f = out.setdefault(p.get("model_code"), {"features": {}, "text": ""})
            for fid, v in (p.get("features") or {}).items():
                f["features"][fid] = v if isinstance(v, str) else v.get("state")
            f["text"] += json.dumps(p.get("catalog_specs", []) + p.get("catalog_passages", []), ensure_ascii=False).lower()
    return out


def _sentences(text: str) -> list:
    return [s for s in re.split(r"(?<=[.!?\n])\s+", text) if s.strip()]


def grounding_flags(spec: dict, turn: dict, catalog_codes: frozenset, session_results: list) -> dict:
    """Automatic, deterministic flags over the final answer against *this session's* tool results
    (earlier turns included, since follow-ups may reuse them) and the user's own text."""
    answer = turn.get("answer", "")
    user_text = " ".join(t for t in turn.get("session_user_text", [turn.get("user", "")]))
    results = [r for r in session_results if isinstance(r, dict)]
    evidence_text = json.dumps(results, ensure_ascii=False)
    prices = _evidence_prices(results)
    codes_in_evidence = _evidence_codes(results)

    fabricated_models, ungrounded_models = [], []
    for code in dict.fromkeys(MODEL_CODE.findall(answer)):
        if code in user_text:
            continue
        if code not in catalog_codes:
            fabricated_models.append(code)
        elif code not in codes_in_evidence:
            ungrounded_models.append(code)

    fabricated_prices = []
    user_digits = re.sub(r"\D", " ", user_text).split()
    for m in MONEY.finditer(answer):
        value = int(re.sub(r"\D", "", m.group(1)))
        if value not in prices and str(value) not in user_digits and str(value) not in re.sub(r"[\s  ]", "", user_text):
            fabricated_prices.append(m.group(0).strip())

    unsupported_numbers = []
    for m in UNIT_NUMBER.finditer(answer):
        num = m.group(1).replace(",", ".")
        plain = num[:-2] if num.endswith(".0") else num
        exact = re.compile(rf"(?<![\d.]){re.escape(plain)}(?:\.0)?(?![\d.])")
        if not exact.search(evidence_text) and not exact.search(user_text):
            unsupported_numbers.append(m.group(0).strip())

    features = _features_by_code(results)
    fabricated_features = []
    for sentence in _sentences(answer):
        low = sentence.lower()
        codes = [c for c in MODEL_CODE.findall(sentence) if c in features]
        if len(codes) != 1 or NEGATION.search(sentence):
            continue
        info = features[codes[0]]
        for fid, words in FEATURE_WORDS.items():
            if any(w in low for w in words):
                state = info["features"].get(fid)
                supported = state == "yes" or (state is None and any(w in info["text"] for w in words))
                if not supported:
                    fabricated_features.append(f"{codes[0]}:{fid} (evidence: {state or 'absent'})")

    lost_gaps = []
    low_answer = answer.lower()
    gap_kinds = {g.get("kind") for r in results[-3:] for g in (r.get("gaps") or [])}
    for kind in sorted(gap_kinds & set(GAP_PHRASES)):
        if not any(p in low_answer for p in GAP_PHRASES[kind]):
            lost_gaps.append(kind)

    g = spec.get("grounding", {})
    missing_mentions = [alts for alts in g.get("must_mention_any", [])
                        if not any(a.lower() in low_answer for a in alts)]
    missing_all = [c for c in g.get("must_mention_all", []) if c.lower() not in low_answer]
    forbidden_hits = [p for p in g.get("forbidden_patterns", []) if re.search(p, answer)]
    return {"fabricated_models": fabricated_models, "ungrounded_models": ungrounded_models,
            "fabricated_prices": fabricated_prices, "unsupported_numbers": unsupported_numbers,
            "fabricated_features": fabricated_features, "lost_gaps": lost_gaps,
            "missing_required_mentions": missing_mentions + [[c] for c in missing_all],
            "forbidden_pattern_hits": forbidden_hits, "manual_review": list(g.get("manual_review", []))}


# ---- run-level scoring ---------------------------------------------------------------------------------

def score_run(cases: list, transcripts: dict, catalog_codes: frozenset) -> dict:
    """``transcripts``: case_id -> {"turns": [normalized turn, ...]}. Cases without a transcript
    are reported as missing, never silently dropped."""
    per_turn, missing = [], []
    for case in cases:
        tr = transcripts.get(case["id"])
        if tr is None or len(tr.get("turns", [])) != len(case["turns"]):
            missing.append(case["id"])
            continue
        session_results, users = [], []
        for i, (spec, turn) in enumerate(zip(case["turns"], tr["turns"])):
            users.append(spec["user"])
            session_results.extend(c.get("result") for c in turn.get("tool_calls", []))
            turn = {**turn, "session_user_text": list(users)}
            per_turn.append(score_turn(case, i, spec, turn, catalog_codes, list(session_results)))
    return {"turns": [t.__dict__ for t in per_turn], "missing_cases": missing,
            "headline": aggregate([t for t in per_turn if t.family not in HEADLINE_EXCLUDED]),
            "followup_spike": aggregate([t for t in per_turn if t.family == "followup"])}


def _rate(n: int, d: int):
    return None if d == 0 else round(n / d, 3)


def aggregate(turns: list) -> dict:
    tool_turns = [t for t in turns if t.expected_tool is not None]
    no_tool = [t for t in turns if t.expected_tool is None]
    arg_turns = [t for t in turns if t.arguments != "n/a"]
    calls = sum(t.calls for t in turns)
    g = lambda key: sum(len(t.grounding.get(key, [])) for t in turns)     # noqa: E731
    return {
        "turns": len(turns),
        "tool_selection_accuracy": _rate(sum(t.selection == "exact" for t in tool_turns), len(tool_turns)),
        "tool_selection_exact_or_acceptable": _rate(sum(t.selection != "wrong" for t in tool_turns), len(tool_turns)),
        "no_tool_accuracy": _rate(sum(t.selection != "wrong" for t in no_tool), len(no_tool)),
        "arguments_exact_rate": _rate(sum(t.arguments == "exact" for t in arg_turns), len(arg_turns)),
        "arguments_exact_or_acceptable_rate": _rate(sum(t.arguments in ("exact", "acceptable") for t in arg_turns),
                                                    len(arg_turns)),
        "unnecessary_tool_call_rate": _rate(sum(t.unnecessary_calls for t in turns), calls),
        "clarification_correctness": _rate(sum(t.clarification_ok for t in turns), len(turns)),
        "avg_tool_calls_per_turn": _rate(calls, len(turns)),
        "max_tool_calls_observed": max((t.calls for t in turns), default=0),
        "fabricated_model_count": g("fabricated_models"), "fabricated_price_count": g("fabricated_prices"),
        "fabricated_feature_count": g("fabricated_features"), "unsupported_numeric_claim_count": g("unsupported_numbers"),
        "lost_gap_count": g("lost_gaps") + sum(len(t.grounding.get("missing_required_mentions", [])) for t in turns),
        "forbidden_pattern_hits": g("forbidden_pattern_hits"),
        "failures": [f"{t.case_id}[{t.turn}]" for t in turns
                     if t.selection == "wrong" or t.arguments == "wrong" or not t.clarification_ok],
    }
