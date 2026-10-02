"""Phase 4F.3 MVP demo gate: targeted live regression and the demo suite (docs/PHASE_4F_3_MVP_HARDENING.md §7).

    python -m evaluation.mvp_demo run <suite> <raw_dir> <driver_id> <run_id> [scenario ...]
    python -m evaluation.mvp_demo collect <suite> <raw_dir> <consultant_log.txt> <catalog_codes.txt> [label]
    python -m evaluation.mvp_demo render          # results.json, transcripts/ and the report from committed inputs
    python -m evaluation.mvp_demo --check         # fail if a generated file is stale

Suites: ``targeted`` (PA-02, PA-04, PA-08, PA-15 of ``acceptance_scenarios.json``, turns unchanged) and ``demo``
(``mvp_demo_scenarios.json``). Conversations are driven by the unchanged live tooling (``model_bakeoff.run_case``:
a temporary inactive driver with the committed Consultant workflow inline, one session per scenario, one n8n
execution per turn). This is a separate gate: it does not use the 4F.1 release thresholds and does not touch the
Phase 4F.2 results.

``collect`` records what was said and done (user turn, answer, tool calls with the arguments the model proposed,
the guard's logged decision, the arguments the Core received, the tool result) plus deterministic *supporting*
checks. Verdicts are manual: ``manual_review.json``. ``render`` is offline and mechanical. No model reasoning is
recorded.
"""
import json
import re
import sys
import time
from pathlib import Path

from consultant.semantic_guard import NUMERIC_ARGS, MessageEvidence

from . import acceptance
from . import model_bakeoff as mb
from .acceptance_report import REQUEST_H, SECRET, _product_line
from .acceptance_run import prior_user_turns
from .agent_eval import MODEL_CODE, from_n8n_agent_output, grounding_flags, validate_cases
from .agent_live import PROJECT, claim_check, fetch, load_catalog_codes
from .bakeoff_report import final_arguments, guard_decisions

MODEL = "gpt-4.1-mini"                        # the accepted production model; never overridden
SCENARIOS = PROJECT / "evaluation/mvp_demo_scenarios.json"
OUT = PROJECT / "evaluation/results/phase_4f_3_demo"
REPORT = OUT / "PHASE_4F_3_MVP_DEMO_REPORT.md"
SUITES = {"targeted": acceptance.DATASET, "demo": SCENARIOS}
FINAL_LINES = {True: "4F.3 MVP DEMO READY — PORTFOLIO READY", False: "4F.3 MVP DEMO HOLD — FURTHER HARDENING REQUIRED"}
MIN_DEMO_PASSES = 6
# Issue kinds of the manual review. Blocking kinds are the MVP readiness criteria; "noted" never blocks.
BLOCKING = ("invented_model", "invented_price", "invented_availability", "unsupported_claim", "invented_numeric_constraint",
            "incorrect_aggregate", "hard_constraint_violation", "constraint_change_failed", "runtime_error")
NOTED = "noted"


def load() -> dict:
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    validate_cases(data["cases"])
    cases = {c["id"]: c for c in data["cases"]}
    accepted = {c["id"]: c for c in acceptance.load()["cases"]}
    for s in data["scenarios"]:
        turns = (accepted if s["suite"] == "targeted" else cases)[s["scenario_id"]]["turns"]
        assert s["suite"] in SUITES and len(s["limits"]) == len(turns), s["scenario_id"]
        assert s["suite"] == "targeted" or len(s["constraint_state"]) == len(turns), s["scenario_id"]
    assert {s["scenario_id"] for s in data["scenarios"] if s["suite"] == "demo"} == set(cases)
    return data


def suite_ids(data: dict, suite: str) -> list:
    return [s["scenario_id"] for s in data["scenarios"] if s["suite"] == suite]


def turns_of(data: dict, scenario: dict) -> list:
    source = acceptance.load()["cases"] if scenario["suite"] == "targeted" else data["cases"]
    return next(c for c in source if c["id"] == scenario["scenario_id"])["turns"]


def constraint_states(scenario: dict) -> list:
    """Active constraints after each turn: the demo file's own, or the 4F.1 card of a targeted scenario."""
    if scenario["suite"] == "demo":
        return scenario["constraint_state"]
    return next(s for s in acceptance.load()["scenarios"] if s["scenario_id"] == scenario["scenario_id"])["constraint_state"]


# ---- live run ---------------------------------------------------------------------------------------

def run(suite: str, out: Path, driver_id: str, run_id: str, only=()) -> dict:
    data = load()
    ids = [i for i in suite_ids(data, suite) if not only or i in only]
    assert ids and set(only) <= set(suite_ids(data, suite)), only
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    log, done, t0 = [], [], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for sid in ids:
        trace_file = out / suite / f"{sid}.trace.json"
        if trace_file.exists():                                      # resume: a scenario is never executed twice
            done.append(sid)
            continue
        again = len(list((out / suite).glob(f"{sid}.failed-*.json")))
        session = f"mvp4f3-{run_id}-{sid}" + (f"-x{again}" if again else "")
        try:
            rec = mb.run_case(MODEL, suite, SUITES[suite], sid, session, driver_id, out, log)
        except mb.ExecutionFailed as e:
            (out / f"stopped-{int(time.time())}.json").write_text(json.dumps(
                {"run_id": run_id, "suite": suite, "started": t0, "completed": done, "stopped_at": sid,
                 "infrastructure_events": log}, ensure_ascii=False, indent=1))
            raise SystemExit(f"STOPPED {run_id}: {e}")
        wrong = [t.get("models") for t in rec["turns"] if t.get("models") not in ([MODEL], [], None)]
        if wrong:
            raise SystemExit(f"{sid}: answered by {wrong}, expected {MODEL}")
        for t in rec["turns"]:
            t["prior_user_turns"] = mb._retry(f"{sid}: memory evidence",
                                              lambda t=t: prior_user_turns(fetch(str(t["sub_execution"]))), log)
        rec["run_id"] = run_id
        trace_file.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
        print(sid, session, [(t.get("status"), t.get("latency_s"), t.get("model_rounds"), len(t.get("tool_ms") or []),
                              t["prior_user_turns"]) for t in rec["turns"]], flush=True)
        done.append(sid)
    record = {"run_id": run_id, "suite": suite, "started": t0, "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "model": MODEL, "scenarios": done, "infrastructure_events": log}
    (out / f"run-{suite}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
    return record


# ---- deterministic supporting checks -----------------------------------------------------------------

# Boolean registry features as an answer names them. Narrower than the guard's mention table (which reads the
# user's wording): an answer check must not fire on ordinary words.
CLAIM_PATTERNS = {
    "hz_120": r"(?<!\d)1[24]\d\s*(?:Гц|Hz)", "vrr": r"\bVRR\b|переменн\w+ частот", "freesync_premium": r"FreeSync",
    "allm": r"\bALLM\b", "game_bar": r"Game Bar|игров\w+ панел", "hdmi_2_1": r"HDMI\s*2[.,]1", "earc": r"\beARC\b",
    "anti_glare": r"антиблик", "filmmaker_mode": r"Filmmaker|режим\w* режисс", "dolby_atmos": r"Atmos", "vesa": r"\bVESA\b",
}
# Wording that makes a line something other than a positive claim about the products: unknown, absent, a
# condition, a question, an offer to check.
NOT_A_CLAIM = re.compile(r"(?i)не указ|не подтвержд|нет данных|нет информации|нет сведений|неизвестн|не приводит|не содерж|"
                         r"не значит|отсутству|\bне\b|\bнет\b|\bбез\b|\bесли\b|уточн|провер|\?")
COUNT_CLAIM = re.compile(r"(?i)(?P<of>\bиз\s+)?(?<![\d.,\w])(?P<n>\d{1,3})(?![\d.,]|\s*(?:гц|hz|вт|дюйм|\"|″|”|₽|руб|тыс|млн|%|см|шт|k\b))\s+"
                         r"(?:(?!из\b)[^\d\s.,;:!?()]+\s+){0,3}?(модел\w*|телевизор\w*|вариант\w*|товар\w*|позици\w*)")
LIMIT_KEYS = ("panel", "size", "min_size", "max_size", "max_price", "available")


def _products(results: list) -> dict:
    """model code -> the product view with every feature state the session's evidence holds for it."""
    out: dict = {}
    for r in results:
        for p in [*((r or {}).get("products") or []), *((r or {}).get("alternatives") or [])]:
            merged = out.setdefault(p["model_code"], {**p, "features": {}})
            merged["features"].update(p.get("features") or {})
            merged.setdefault("catalog_specs", []).extend(x for x in p.get("catalog_specs") or []
                                                          if x not in merged["catalog_specs"])
    return out


def _state(product: dict, feature: str):
    v = (product.get("features") or {}).get(feature)
    if feature == "hz_120" and v is None:
        hz = (product.get("specs") or {}).get("refresh_rate_hz")
        return None if hz is None else "yes" if hz >= 120 else "no"
    return v if isinstance(v, str) or v is None else v.get("state")


def unsupported_feature_claims(answer: str, results: list) -> list:
    """A line that names a registry feature as a fact while the session's evidence does not say ``yes`` for
    every product the line is about: the products it names, or -- for a line without a model code, such as an
    introduction -- every evidenced product the answer names. Unknown and negative wording is not a claim."""
    products = _products(results)
    named = [c for c in dict.fromkeys(MODEL_CODE.findall(answer)) if c in products]
    issues = []
    for line in re.split(r"\n+", answer):
        hits = [f for f, pattern in CLAIM_PATTERNS.items() if re.search(pattern, line, re.I)]
        if not hits or NOT_A_CLAIM.search(line):
            continue
        targets = [c for c in dict.fromkeys(MODEL_CODE.findall(line)) if c in products] or named
        for feature in hits:
            text = lambda c: json.dumps(products[c].get("catalog_specs", []) + [products[c].get("name", "")],   # noqa: E731
                                        ensure_ascii=False)
            bad = {c: _state(products[c], feature) or "no_evidence" for c in targets
                   if _state(products[c], feature) != "yes"
                   and not (_state(products[c], feature) is None and re.search(CLAIM_PATTERNS[feature], text(c), re.I))}
            if bad:
                issues.append({"feature": feature, "line": line.strip()[:200], "not_yes_for": bad})
    return issues


def invented_numeric_arguments(final_args, user_messages: list) -> list:
    """Numeric constraints the Core received whose value no user message of the conversation states (digits,
    spelled or slang): the guard's own reading, applied to the final arguments whatever mode the guard was in."""
    if not isinstance(final_args, dict):
        return []
    evidence = [MessageEvidence.from_text(m) for m in user_messages]
    if any(e.spelled_numbers for e in evidence):
        return [f"{k}={final_args[k]:g}: not judged (a number word in the conversation could not be read)"
                for k in NUMERIC_ARGS if k in final_args]
    numbers = frozenset().union(*(e.numbers for e in evidence))
    return [f"{k}={final_args[k]:g} is stated in no user message" for k in NUMERIC_ARGS
            if k in final_args and float(final_args[k]) not in numbers]


def listed_values(result: dict, feature: str) -> list:
    """``{value: products}`` dictionaries a count result holds for a feature, one per availability bucket. They sit
    in the count entry itself (``values``); the superseded images of this phase had a separate ``attribute_values``."""
    nested = [b["values"] for b in ((result.get("attribute_counts") or {}).get(feature) or {}).values() if b.get("values")]
    return nested + list(((result.get("attribute_values") or {}).get(feature) or {}).values())


def _evidence_counts(results: list) -> tuple:
    """``(every count in the session's evidence, {feature: counts that are about that feature})``."""
    general, by_feature = set(), {}
    for r in results:
        r = r or {}
        general.update(v for v in (r.get("counts") or {}).values() if isinstance(v, int))
        general.update(v for v in (r.get("totals") or {}).values() if isinstance(v, int) and not isinstance(v, bool))
        for g in r.get("groups") or []:
            general.update(v for k, v in g.items() if k in ("count", "available") and isinstance(v, int))
        general.add(len(r.get("products") or []))
        for feature, buckets in (r.get("attribute_counts") or {}).items():
            for states in buckets.values():
                by_feature.setdefault(feature, set()).update(v for v in states.values() if isinstance(v, int))
            for values in listed_values(r, feature):
                counts = {v for k, v in values.items() if k[:1].isdigit() and isinstance(v, int)}
                by_feature.setdefault(feature, set()).update(counts)
                general.update(counts)
        for feature, states in ((r.get("feature_summary") or {}).get("counts") or {}).items():
            by_feature.setdefault(feature, set()).update(states.values())
    general.discard(0)
    return general, by_feature


def count_claims(answer: str, results: list) -> list:
    """«N моделей …» statements: N must be a count of this session's evidence, and when the sentence is about a
    feature, a count *of that feature* (4F.2 PA-15: 66 available products stated as 66 models with Dolby Atmos)."""
    general, by_feature = _evidence_counts(results)
    issues = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer):
        for m in COUNT_CLAIM.finditer(sentence):
            n = int(m.group("n"))
            features = [f for f, pattern in CLAIM_PATTERNS.items() if re.search(pattern, sentence, re.I)]
            if m.group("of") and n in general:
                continue                                  # «из 75 моделей …»: the size of the counted set, not a feature count
            if features and not NOT_A_CLAIM.search(sentence):
                bad = [f for f in features if n not in by_feature.get(f, ())]
                if bad:
                    issues.append({"count": n, "sentence": sentence.strip()[:200],
                                   "problem": f"no count of {n} for {bad} in the evidence"})
            elif n not in general and not any(n in v for v in by_feature.values()):
                issues.append({"count": n, "sentence": sentence.strip()[:200], "problem": "no such count in the evidence"})
    return issues


GROUP_WORD = re.compile(r"(?i)(?<![а-яё])(все|всех|всем|каждый|каждая|каждого|любой|любая)(?![а-яё])")
HZ_VALUE = re.compile(r"(?i)(?<!\d)((?:\d{2,3}\s*(?:-|/|и|или)\s*)*\d{2,3})\s*(?:Гц|Hz)")      # "50 или 60 Гц" is two values
NEGATED = re.compile(r"(?i)(?<![а-яё])(не|нет|без|ни)(?![а-яё])")


def uniform_value_claims(answer: str, turn_results: list, session_results: list) -> list:
    """«Все … N Гц»: one refresh rate stated for a whole group. The evidence of the turn (else of the session)
    must hold refresh rates -- listed values of a count, a refresh-rate grouping, or product rows -- and only that
    one (4F.3 targeted run 1, PA-15 turn 2: "no model is 120 Hz" became "all are 60 Hz"; two are 50 Hz)."""
    rates, all_120 = set(), False
    for r in [x for x in turn_results if x] or [x for x in session_results if x]:
        counted = ((r.get("attribute_counts") or {}).get("hz_120") or {}).values()
        all_120 = all_120 or any(c["yes"] and not c["no"] and not c["not_listed"] for c in counted)   # "yes" = 120 Hz or more
        for bucket in listed_values(r, "hz_120"):
            rates.update(int(float(k)) for k in bucket if k[:1].isdigit())
        if r.get("group_by") == "refresh_rate_hz":
            rates.update(g["value"] for g in r.get("groups") or [] if g.get("value") is not None)
        rates.update(hz for p in r.get("products") or [] for hz in [(p.get("specs") or {}).get("refresh_rate_hz")] if hz)
    issues = []
    for clause in re.split(r"[,;:—–.!?\n]+", answer):
        values = {int(v) for m in HZ_VALUE.findall(clause) for v in re.findall(r"\d{2,3}", m)}
        if len(values) != 1 or not GROUP_WORD.search(clause) or NEGATED.search(clause):
            continue
        n = next(iter(values))
        if rates != {n} and not (n == 120 and all_120 and not rates):
            issues.append({"claim": clause.strip()[:200], "stated_hz": n,
                           "evidence_hz": sorted(rates) or "no refresh-rate values in the evidence"})
    return issues


BOUND_WORD = (r"(?:до|от|свыше|максимум|минимум|дешевле|дороже|в\s+пределах|не\s+(?:больше|более|дороже|выше|меньше|менее|дешевле))")


def unapplied_limits_stated(answer: str, calls: list) -> list:
    """A price or size limit the guard removed or (Phase 4F.3A) rejected, because no user message states it, that the
    answer still words as a limit: «до 50 000 ₽», «до 55 дюймов». The value did not reach the Core, but the user is
    shown a threshold they never gave."""
    issues = []
    for call in calls:
        for c in [*((call["result"].get("request") or {}).get("not_applied") or {}).get("constraints", []),
                  *(call["result"].get("unsupported_numeric_constraints") or [])]:
            arg, v = c["argument"], c["value"]
            if arg not in ("min_price", "max_price", "screen_size_inches", "min_screen_size_inches", "max_screen_size_inches"):
                continue
            n = int(v)
            if "price" in arg:
                forms = [f"{n:,}".replace(",", r"[\s\u00a0]?")] + ([rf"{n // 1000}\s*(?:тыс|к\b|k\b)"] if n % 1000 == 0 and n >= 1000 else [])
                pattern = rf"(?i){BOUND_WORD}\s+(?:{'|'.join(forms)})(?!\d)"
            else:
                pattern = rf"(?i){BOUND_WORD}\s+{n}\s*(?:дюйм|\"|″|”)"
            m = re.search(pattern, answer)
            if m:
                issues.append({"argument": arg, "value": v, "stated_as": m.group(0)})
    return issues


def products_outside_limits(answer: str, results: list, limits) -> list:
    """Products the answer names that break the turn's hard limits, by the evidence's own facts."""
    if not limits:
        return []
    assert set(limits) <= set(LIMIT_KEYS), limits
    products, out = _products(results), []
    for code in dict.fromkeys(MODEL_CODE.findall(answer)):
        p = products.get(code)
        if p is None:
            continue
        specs, why = p.get("specs") or {}, []
        size, price = specs.get("screen_size_inches"), p.get("current_price_rub")
        if "panel" in limits and specs.get("panel_technology") not in limits["panel"]:
            why.append(f"panel {specs.get('panel_technology')}")
        if size is not None and ((("size" in limits) and size != limits["size"]) or size < limits.get("min_size", 0)
                                 or size > limits.get("max_size", 1e9)):
            why.append(f"size {size}")
        if price is not None and price > limits.get("max_price", 1e12):
            why.append(f"price {price}")
        if limits.get("available") and p.get("available") is not True:
            why.append("not available")
        if why:
            out.append({"model_code": code, "outside": why})
    return out


def automated_checks(answer: str, calls: list, session_results: list, user_messages: list, limits, catalog_codes: frozenset,
                     status: str) -> dict:
    g = grounding_flags({"tool": None}, {"user": user_messages[-1], "answer": answer, "session_user_text": user_messages},
                        catalog_codes, session_results)
    statuses = [c["result"].get("status") for c in calls]
    # Per line that names exactly one product: its price, availability and features must be that product's own
    # (targeted run 2: QE55QN80HAUXPY shown with the price of QE65QN80HAUXPY -- a real price, of another product).
    per_model = claim_check(answer, session_results)[1]
    # An amount the user stated ("до сотки" restated as 100 000 ₽) is not an invented price; read it as the guard does.
    stated = frozenset().union(*(MessageEvidence.from_text(m).numbers for m in user_messages))
    invented_prices = [x for x in g["fabricated_prices"] if float(re.sub(r"\D", "", x.split("₽")[0]) or 0) not in stated]
    return {
        "invented_models": g["fabricated_models"], "models_not_in_evidence": g["ungrounded_models"],
        "invented_prices": invented_prices, "price_mismatches": [x for x in per_model if ": price " in x],
        "per_model_claims": [x for x in per_model if ": price " not in x],
        "availability_mismatches": g["availability_mismatches"],
        "mislabelled_prices": g["mislabelled_prices"],
        "unsupported_feature_claims": unsupported_feature_claims(answer, session_results),
        "invented_numeric_arguments": [f"{c['tool']}: {x}" for c in calls
                                       for x in invented_numeric_arguments(c["final_args"], user_messages)],
        "unapplied_limits_stated": unapplied_limits_stated(answer, calls),
        "count_claims": count_claims(answer, session_results),
        "uniform_value_claims": uniform_value_claims(answer, [c["result"] for c in calls], session_results),
        "products_outside_limits": products_outside_limits(answer, session_results, limits),
        "runtime": {"execution": status, "tool_statuses": statuses, "tool_calls": len(calls),
                    "errors": [s for s in statuses if s in ("error", None)] + ([] if status == "success" else [status])},
    }


# ---- collect -----------------------------------------------------------------------------------------

def run_window(log_text: str, run_id: str) -> str:
    """The Consultant log from this run's first request on. ``guard_decisions`` pairs each logged decision with
    the next request line that has a conversation key, so earlier calls without one (deployment probes) must not
    be in the text."""
    lines = log_text.splitlines()
    first = next((i for i, line in enumerate(lines) if f"conv=mvp4f3-{run_id}-" in line), None)
    if first is None:
        raise SystemExit(f"no request of run {run_id} in the Consultant log")
    return "\n".join(lines[first:])


def collect(suite: str, run_dir: Path, log_file: Path, codes_file: Path, label: str = None) -> dict:
    """``label`` names the evidence file (default: the suite); a superseded run is kept under its own label."""
    data, run_dir = load(), Path(run_dir)
    record = json.loads((run_dir / f"run-{suite}.json").read_text())
    log_text = run_window(Path(log_file).read_text(), record["run_id"])
    decisions = guard_decisions(log_text)
    seen = {m.group(1): {"conv": m.group(2), "h": m.group(3)} for m in REQUEST_H.finditer(log_text)}
    codes = load_catalog_codes(codes_file)
    scenarios = []
    for sid in record["scenarios"]:
        spec = next(s for s in data["scenarios"] if s["scenario_id"] == sid)
        trace = json.loads((run_dir / suite / f"{sid}.trace.json").read_text())
        turns, session_results, users = [], [], []
        for i, tt in enumerate(trace["turns"]):
            norm = from_n8n_agent_output(tt["user"], tt.get("agent_item") or {})
            guards = list(decisions.get(str(tt["sub_execution"]), []))
            calls = []
            for call in norm["tool_calls"]:
                proposed = {k: v for k, v in (call.get("args") or {}).items() if k != "tool"} \
                    if isinstance(call.get("args"), dict) else call.get("args")
                result = call.get("result") if isinstance(call.get("result"), dict) else {"status": None}
                # Parallel calls are logged in arrival order, not call order: match the guard decision by tool and
                # outcome, and among those by what the result itself says was not applied.
                same = [x for x in guards if x["tool"] == call["tool"] and x["result_status"] == result.get("status")]
                dropped = {(c["argument"], _j(c["value"])) for c in ((result.get("request") or {}).get("not_applied") or {})
                           .get("constraints", [])}
                g = next((x for x in same if {(a["argument"], _j(a["value"])) for a in x["actions"]
                                              if a["action"] == "removed"} == dropped), same[0] if same else None)
                if g is not None:
                    guards.remove(g)
                calls.append({"tool": call["tool"], "proposed_args": proposed,
                              "guard": {k: g[k] for k in ("status", "detail", "actions")} if g else {"status": "not_found_in_log"},
                              "final_args": None if g and g["status"] == "rejected" else final_arguments(proposed, g),
                              "result": {k: v for k, v in result.items() if k not in ("data_notice", "scope_note")}})
            users.append(tt["user"])
            session_results.extend(c["result"] for c in calls)
            options = (tt.get("model_options") or [{}])[0]
            turns.append({
                "turn": i + 1, "execution_id": str(tt["sub_execution"]), "timestamp_utc": fetch(str(tt["sub_execution"])).get("startedAt"),
                "model_as_sent": tt.get("models"), "temperature": options.get("temperature"), "user": tt["user"],
                "answer": norm["answer"], "prior_user_turns": tt.get("prior_user_turns"),
                "consultant_saw": seen.get(str(tt["sub_execution"])), "tool_calls": calls,
                "latency_s": tt.get("latency_s"), "model_rounds": tt.get("model_rounds"), "tokens": tt.get("tokens"),
                "cost_usd": tt.get("cost_usd"),
                "automated_checks": automated_checks(norm["answer"], calls, list(session_results), list(users),
                                                     spec["limits"][i], codes, tt.get("status"))})
        scenarios.append({"scenario_id": sid, "session_id": trace["session"], "started": trace["started"], "turns": turns})
    evidence = {"suite": suite, "run_id": record["run_id"], "started": record["started"], "finished": record["finished"],
                "model": record["model"], "infrastructure_events": record["infrastructure_events"], "scenarios": scenarios}
    text = json.dumps(evidence, ensure_ascii=False, indent=1)
    assert not SECRET.search(text), "secret-like text in the evidence"
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"evidence_{label or suite}.json").write_text(text + "\n", encoding="utf-8")
    return evidence


# ---- review + decision (offline) ---------------------------------------------------------------------

class ReviewError(ValueError):
    pass


def _load(name: str) -> dict:
    return json.loads((OUT / name).read_text(encoding="utf-8"))


def graded(data: dict, evidence: dict, review: dict) -> list:
    out = []
    for ev in evidence["scenarios"]:
        sid = ev["scenario_id"]
        spec = next(s for s in data["scenarios"] if s["scenario_id"] == sid)
        rv = review.get(sid)
        if rv is None or set(rv["turns"]) != {str(t["turn"]) for t in ev["turns"]}:
            raise ReviewError(f"{sid}: the review must cover every turn")
        if [t["user"] for t in ev["turns"]] != [t["user"] for t in turns_of(data, spec)]:
            raise ReviewError(f"{sid}: executed turns differ from the scenario")
        states, turns = constraint_states(spec), []
        for t in ev["turns"]:
            tr = rv["turns"][str(t["turn"])]
            for i in tr.get("issues", []):
                if set(i) != {"kind", "text"} or i["kind"] not in (*BLOCKING, NOTED):
                    raise ReviewError(f"{sid}[{t['turn']}]: bad issue {i}")
            blocking = [i for i in tr.get("issues", []) if i["kind"] != NOTED]
            state = states[t["turn"] - 1]
            turns.append({**t, "active_constraints": {k: state[k] for k in ("hard", "soft", "replaced", "released", "note") if state.get(k)},
                          "limits": spec["limits"][t["turn"] - 1], "issues": tr.get("issues", []), "notes": tr.get("note", ""),
                          "verdict": "FAIL" if blocking else "PASS"})
        verdict = "FAIL" if any(t["verdict"] == "FAIL" for t in turns) else "PASS"
        if spec["suite"] == "targeted" and not isinstance(rv["mvp_defect_gone"], bool):
            raise ReviewError(f"{sid}: mvp_defect_gone must be stated")      # the 4F.2 defect itself; a turn can fail on another issue
        out.append({"scenario_id": sid, "suite": spec["suite"], "title": spec["title"], "session_id": ev["session_id"],
                    "started": ev["started"], "run_id": evidence["run_id"], "model": evidence["model"], "verdict": verdict,
                    "demo_ready": verdict == "PASS", "summary": rv["summary"],
                    **({"mvp_target": spec["mvp_target"], "old_failure": spec["old_failure"], "criterion": spec["criterion"],
                        "new_behaviour": rv["new_behaviour"], "evidence": rv["evidence"],
                        "mvp_defect_gone": rv["mvp_defect_gone"]} if spec["suite"] == "targeted" else {"shows": spec["shows"]}),
                    "turns": turns})
    return out


def decide(targeted: list, demo: list, manifest: dict) -> dict:
    """The MVP gate of docs/PHASE_4F_3_MVP_HARDENING.md §7, applied mechanically to the reviewed runs."""
    scenarios = [*targeted, *demo]
    issues = [(s["scenario_id"], t["turn"], i) for s in scenarios for t in s["turns"] for i in t["issues"]]
    count = lambda *kinds: sum(i["kind"] in kinds for _, _, i in issues)             # noqa: E731
    runtime_errors = [f"{s['scenario_id']}[{t['turn']}]" for s in scenarios for t in s["turns"]
                      if t["automated_checks"]["runtime"]["errors"]]
    criteria = {
        "targeted_defects_gone": all(s["mvp_defect_gone"] for s in targeted) and len(targeted) == 4,
        "zero_invented_product_or_model_codes": count("invented_model") == 0,
        "zero_invented_prices": count("invented_price") == 0,
        "zero_invented_availability": count("invented_availability") == 0,
        "zero_unsupported_material_claims": count("unsupported_claim") == 0,
        "zero_invented_numeric_constraints": count("invented_numeric_constraint") == 0,
        "zero_incorrect_exact_aggregates": count("incorrect_aggregate") == 0,
        "no_hard_constraint_violations": count("hard_constraint_violation") == 0,
        "multi_turn_constraint_changes_work": count("constraint_change_failed") == 0,
        "at_least_6_successful_demo_conversations": sum(s["verdict"] == "PASS" for s in demo) >= MIN_DEMO_PASSES,
        "no_infrastructure_or_runtime_errors": not runtime_errors and count("runtime_error") == 0
        and not manifest["run"]["unresolved_infrastructure_failures"],
    }
    ready = all(criteria.values())
    return {"ready": ready, "final_line": FINAL_LINES[ready], "criteria": criteria,
            "targeted": {s["scenario_id"]: s["verdict"] for s in targeted},
            "demo": {s["scenario_id"]: s["verdict"] for s in demo},
            "demo_passed": sum(s["verdict"] == "PASS" for s in demo), "demo_total": len(demo),
            "blocking_issues": [{"scenario_id": sid, "turn": n, **i} for sid, n, i in issues if i["kind"] != NOTED],
            "noted_issues": len([1 for _, _, i in issues if i["kind"] == NOTED]), "runtime_errors": runtime_errors}


def build() -> dict:
    data = load()
    manifest, review = _load("run_manifest.json"), _load("manual_review.json")
    ev_t, ev_d = _load("evidence_targeted.json"), _load("evidence_demo.json")
    targeted, demo = graded(data, ev_t, review["scenarios"]), graded(data, ev_d, review["scenarios"])
    turns = [t for s in (*targeted, *demo) for t in s["turns"]]
    calls = [c for t in turns for c in t["tool_calls"]]
    tally = lambda values: {str(k): values.count(k) for k in sorted(set(values), key=str)}       # noqa: E731
    lat = sorted(t["latency_s"] for t in turns)
    summary = {
        "targeted": {"scenarios": len(targeted), "turns": sum(len(s["turns"]) for s in targeted)},
        "demo": {"scenarios": len(demo), "turns": sum(len(s["turns"]) for s in demo)},
        "models_as_sent": sorted({m for t in turns for m in (t["model_as_sent"] or [])}),
        "temperatures": sorted({t["temperature"] for t in turns if t["temperature"] is not None}),
        "sessions": [s["session_id"] for s in (*targeted, *demo)],
        "memory_carried": all(t["prior_user_turns"] == t["turn"] - 1 for t in turns),
        "tool_calls": len(calls), "max_tool_calls_per_turn": max(len(t["tool_calls"]) for t in turns),
        "tool_result_statuses": tally([c["result"].get("status") for c in calls]),
        "guard_decisions": tally([c["guard"]["status"] for c in calls]),
        "latency_s": {"median": lat[len(lat) // 2], "max": lat[-1]},
        "cost_usd_n8n_estimate": round(sum(t["cost_usd"] or 0 for t in turns), 4),
        "automated_flags": {k: sum(bool(t["automated_checks"][k]) for t in turns)
                            for k in ("invented_models", "invented_prices", "price_mismatches", "availability_mismatches",
                                      "unsupported_feature_claims", "invented_numeric_arguments", "unapplied_limits_stated",
                                      "count_claims",
                                      "uniform_value_claims", "products_outside_limits")},
    }
    measurements = _load("measurements.json")
    return {"_meta": {"phase": "4F.3", "gate": "docs/PHASE_4F_3_MVP_HARDENING.md §7",
                      "scenarios": {"version": data["_meta"]["version"], "sha256": acceptance.sha256(SCENARIOS)},
                      "generated_by": "python -m evaluation.mvp_demo render",
                      "inputs": ["evidence_targeted.json", "evidence_demo.json", "manual_review.json", "measurements.json",
                                 "run_manifest.json"],
                      "contents": "No model reasoning is recorded."},
            "decision": decide(targeted, demo, manifest), "summary": summary, "targeted": targeted, "demo": demo,
            "measurements": measurements["summary"], "limitations": review["limitations"]}


# ---- transcripts and report ---------------------------------------------------------------------------

def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False)


def _tool_lines(call: dict, n: int) -> list:
    g, r = call["guard"], call["result"]
    acts = "; ".join(f"{a['action']} {a['argument']}={_j(a['value'])} ({a['reason']})" for a in g.get("actions", []))
    req = r.get("request") or {}
    out = [f"- Tool call {n}: `{call['tool']}`",
           f"  - arguments proposed by the model: `{_j(call['proposed_args'])}`",
           f"  - guard: **{g['status']}**" + (f" — {acts}" if acts else ""),
           "  - arguments the Core received: none — the call was rejected and not run" if g["status"] == "rejected" else
           f"  - arguments the Core received: `{_j(call['final_args'])}`",
           f"  - result: status `{r.get('status')}`, confidence `{r.get('confidence')}`, totals `{_j(r.get('totals'))}`"]
    for key in ("constraints", "use_cases", "required_features", "preferred_features", "attributes_asked", "question",
                "model_resolution", "features_checked", "not_applied"):
        if req.get(key):
            out.append(f"  - request.{key}: `{_j(req[key])}`")
    for key in ("counted", "counts", "attribute_counts", "attribute_values", "groups", "confidence_notes", "comparison",
                "clarification", "errors", "unsupported_numeric_constraints"):
        if r.get(key):
            out.append(f"  - {key}: `{_j(r[key])}`")
    if r.get("feature_summary"):
        out.append(f"  - feature_summary ({r['feature_summary']['products']} shown): `{_j(r['feature_summary']['counts'])}`")
    for gap in r.get("gaps") or []:
        extra = f" {_j(gap['attributes'])} for {_j(gap.get('products'))}" if gap.get("attributes") else ""
        out.append(f"  - gap `{gap.get('kind')}`{extra}: {gap.get('detail', '')}")
    for label in ("products", "alternatives"):
        for p in r.get(label) or []:
            out.append(f"  - {label[:-1]}: {_product_line(p)}")
            rows = "; ".join(f"{x['name']}: {x['value']}" for x in p.get("catalog_specs") or [])
            if rows:
                out.append(f"    - catalog rows: {rows}")
            for ps in p.get("catalog_passages") or []:
                out.append(f"    - catalog passage ({ps.get('section')}): " + " / ".join(ps.get("text", "").splitlines()))
    return out


def _turn_lines(t: dict) -> list:
    ac = t["active_constraints"]
    out = [f"### Turn {t['turn']} — {t['verdict']}", "", f"**User:** {t['user']}", "",
           f"- Execution `{t['execution_id']}`, {t['timestamp_utc']}; model as sent `{', '.join(t['model_as_sent'] or [])}`, "
           f"temperature {t['temperature']}; earlier user turns in the Agent's memory: {t['prior_user_turns']}; "
           f"latency {t['latency_s']} s",
           f"- Active constraints — hard: {'; '.join(ac.get('hard', [])) or 'none'}"
           + (f" · soft: {'; '.join(ac['soft'])}" if ac.get("soft") else "")
           + (f" · replaced: {'; '.join(ac['replaced'])}" if ac.get("replaced") else "")
           + (f" · released: {'; '.join(ac['released'])}" if ac.get("released") else "")
           + (f" · note: {ac['note']}" if ac.get("note") else "")]
    if not t["tool_calls"]:
        out.append("- Tool calls: none")
    for n, call in enumerate(t["tool_calls"], start=1):
        out += _tool_lines(call, n)
    out += ["", "**Consultant:**", ""] + [f"> {x}" if x else ">" for x in (t["answer"] or "(empty)").splitlines()]
    out += ["", "**Review**", ""]
    for i in t["issues"]:
        out.append(f"- **{'noted' if i['kind'] == NOTED else 'BLOCKING · ' + i['kind']}** — {i['text']}")
    if t["notes"]:
        out.append(f"- Notes: {t['notes']}")
    flags = {k: v for k, v in t["automated_checks"].items() if v and k != "runtime"}
    runtime = t["automated_checks"]["runtime"]
    out += [f"- Automated checks (supporting only): `{_j(flags)}`; runtime `{_j(runtime)}`", f"- **{t['verdict']}**", ""]
    return out


def render_transcript(s: dict) -> str:
    out = [f"# {s['scenario_id']} — {s['title']}", "",
           "Phase 4F.3 conversation transcript. Generated by `python -m evaluation.mvp_demo render` from the committed evidence "
           "and review files; not edited by hand. No model reasoning is recorded.", "",
           f"- Suite: {'targeted regression (the Phase 4F.2 turns, unchanged)' if s['suite'] == 'targeted' else 'MVP demo suite'}",
           f"- Model: `{s['model']}` · session `{s['session_id']}` · run `{s['run_id']}` · started {s['started']}",
           f"- **Demo readiness: {s['verdict']}** — {s['summary']}"]
    if s["suite"] == "targeted":
        out += [f"- Target: {s['mvp_target']} — {s['criterion']}", f"- Phase 4F.2 failure: {s['old_failure']}",
                f"- New behaviour: {s['new_behaviour']}", f"- Evidence: {s['evidence']}"]
    else:
        out.append("- Shows: " + "; ".join(s["shows"]))
    out += ["", "## Conversation", ""]
    for t in s["turns"]:
        out += _turn_lines(t)
    return "\n".join(out).rstrip() + "\n"


def _table(header: list, rows: list) -> list:
    return ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)] + \
        ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]


def render_report(res: dict, manifest: dict) -> str:
    d, s = res["decision"], res["summary"]
    yes = lambda v: "yes" if v else "**no**"                                         # noqa: E731
    out = ["# Phase 4F.3 — MVP Demo Report", "", f"**{d['final_line']}**", "",
           "The Samsung AI Consultant after the Phase 4F.3 hardening, run live through four Phase 4F.2 acceptance scenarios "
           "(targeted regression) and eight demo conversations. This is the MVP demo gate of "
           "[docs/PHASE_4F_3_MVP_HARDENING.md](../../../docs/PHASE_4F_3_MVP_HARDENING.md) §7. It is not the 4F.1 release gate: "
           "**Phase 4F.2 remains `4F.2 HOLD — PRODUCT ACCEPTANCE FAILED`**, and the product is not accepted as "
           "production-ready under that rule. Generated by `python -m evaluation.mvp_demo render` from the files in this "
           "directory.", "",
           *_table(["File", "Contents"], [
               ["[run_manifest.json](run_manifest.json)", "deployment identity, preflight and post-run checks, hashes, sessions, cleanup"],
               ["[evidence_targeted.json](evidence_targeted.json), [evidence_demo.json](evidence_demo.json)",
                "per turn: user turn, answer, tool calls (proposed arguments, guard decision, arguments the Core received, tool result), memory evidence, automated checks"],
               ["[manual_review.json](manual_review.json)", "the reviewer's verdict, notes and issues per turn; known limitations"],
               ["[measurements.json](measurements.json)", "repeated-session measurements: per configuration and question, every answer and its classification"],
               ["[evidence_targeted_run1.json](evidence_targeted_run1.json), [run2](evidence_targeted_run2.json), [run3](evidence_targeted_run3.json)",
                "the earlier targeted runs of this phase on superseded builds, kept as recorded"],
               ["[results.json](results.json)", "graded turns, scenario verdicts, counts, the mechanical gate decision"],
               ["[transcripts/](transcripts/)", "one transcript per conversation"]]), "",
           "## 1. Deployment under test", "",
           *_table(["Item", "Value"], [[k, v] for k, v in manifest["deployment"].items()]), "",
           "## 2. Run", "",
           *_table(["Item", "Value"], [
               ["Targeted regression", f"{s['targeted']['scenarios']} scenarios, {s['targeted']['turns']} turns; run `{manifest['run']['targeted_run_id']}`, {manifest['run']['targeted_window_utc']}"],
               ["Demo suite", f"{s['demo']['scenarios']} conversations, {s['demo']['turns']} turns; run `{manifest['run']['demo_run_id']}`, {manifest['run']['demo_window_utc']}"],
               ["Model", f"`{', '.join(s['models_as_sent'])}`, temperature {', '.join(map(str, s['temperatures']))}, as sent by n8n in every turn"],
               ["Sessions", f"{len(s['sessions'])}, one fresh session per conversation (`mvp4f3-<run>-<scenario>`); memory carried within a conversation: {yes(s['memory_carried'])}"],
               ["Tool calls", f"{s['tool_calls']}; at most {s['max_tool_calls_per_turn']} in one turn; result statuses `{_j(s['tool_result_statuses'])}`"],
               ["Guard decisions", f"`{_j(s['guard_decisions'])}`"],
               ["Runtime errors", manifest["run"]["runtime_errors"]],
               ["Latency per turn", f"median {s['latency_s']['median']} s, max {s['latency_s']['max']} s"],
               ["Cost (n8n estimate)", f"${s['cost_usd_n8n_estimate']}"],
               ["Other runs", manifest["run"]["other_runs"]]]), "",
           "## 3. Targeted live regression", "",
           *_table(["Scenario", "Target", "Phase 4F.2 failure", "New behaviour", "Result"],
                   [[f"[{x['scenario_id']}](transcripts/{x['scenario_id']}.md)", x["mvp_target"], x["old_failure"], x["new_behaviour"],
                     f"defect gone: **{'yes' if x['mvp_defect_gone'] else 'no'}**; conversation: **{x['verdict']}**"] for x in res["targeted"]]), ""]
    for x in res["targeted"]:
        out += [f"- **{x['scenario_id']}** — evidence: {x['evidence']}"]
    out += ["", "## 4. Demo suite", "",
            *_table(["Conversation", "Shows", "Turns", "Session", "Demo readiness", "Summary"],
                    [[f"[{x['scenario_id']}](transcripts/{x['scenario_id']}.md) — {x['title']}", "; ".join(x["shows"]), len(x["turns"]),
                      f"`{x['session_id']}`", f"**{x['verdict']}**", x["summary"]] for x in res["demo"]]), "",
            f"**{d['demo_passed']} of {d['demo_total']} demo conversations pass.**", "",
            "## 5. Issues", ""]
    if d["blocking_issues"]:
        out += _table(["Scenario", "Turn", "Kind", "Issue"], [[i["scenario_id"], i["turn"], i["kind"], i["text"]] for i in d["blocking_issues"]])
    else:
        out.append("No blocking issue in any turn of either suite.")
    noted = [(x["scenario_id"], t["turn"], i["text"]) for x in (*res["targeted"], *res["demo"]) for t in x["turns"]
             for i in t["issues"] if i["kind"] == NOTED]
    out += ["", f"Noted, not blocking ({len(noted)}):", ""] + ([f"- {sid} turn {n}: {text}" for sid, n, text in noted] or ["- none"])
    out += ["", "Automated supporting checks (turns with at least one flag; every flag was read by the reviewer):", "",
            *_table(["Check", "Turns flagged"], [[k.replace("_", " "), v] for k, v in s["automated_flags"].items()]), "",
            "## 6. Repeated-session measurements", "",
            "One recorded conversation says little about a model that does not answer the same way twice at temperature 0. "
            "Each row is one question asked in that many fresh sessions; the answers are in "
            "[measurements.json](measurements.json). Rows marked *superseded* are builds of this phase that were replaced.", "",
            *_table(["Configuration", "Question", "Sessions", "Result"],
                    [[x["configuration"], x["question"], x["sessions"], x["result"]] for x in res["measurements"]]), "",
            "## 7. Known limitations", "", *[f"- {x}" for x in res["limitations"]], "",
            "## 8. Safety", "", *[f"- {x}" for x in manifest["safety"]], "",
            "## 9. MVP gate", "",
            *_table(["Criterion", "Met"], [[k.replace("_", " "), yes(v)] for k, v in d["criteria"].items()]), "",
            f"**{d['final_line']}**"]
    return "\n".join(out) + "\n"


def render_all() -> dict:
    res, manifest = build(), _load("run_manifest.json")
    files = {OUT / "results.json": json.dumps(res, ensure_ascii=False, indent=1) + "\n", REPORT: render_report(res, manifest)}
    for s in (*res["targeted"], *res["demo"]):
        files[OUT / "transcripts" / f"{s['scenario_id']}.md"] = render_transcript(s)
    for text in files.values():
        assert not SECRET.search(text), "secret-like text in a generated file"
    return files


def stale_files() -> list:
    return [str(p.relative_to(PROJECT)) for p, text in render_all().items()
            if not p.exists() or p.read_text(encoding="utf-8") != text]


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        r = run(sys.argv[2], Path(sys.argv[3]), sys.argv[4], sys.argv[5], tuple(sys.argv[6:]))
        print("DONE", r["run_id"], r["suite"], len(r["scenarios"]), "scenarios; infrastructure events:", len(r["infrastructure_events"]))
    elif cmd == "collect":
        ev = collect(sys.argv[2], Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]), *sys.argv[6:7])
        print(ev["suite"], ev["run_id"], len(ev["scenarios"]), "scenarios,", sum(len(s["turns"]) for s in ev["scenarios"]), "turns")
    elif cmd == "render":
        (OUT / "transcripts").mkdir(parents=True, exist_ok=True)
        generated = render_all()
        for path, text in generated.items():
            path.write_text(text, encoding="utf-8")
        print(f"wrote {len(generated)} files;", json.loads((OUT / "results.json").read_text())["decision"]["final_line"])
    elif cmd == "--check":
        stale = stale_files()
        if stale:
            raise SystemExit(f"stale: {stale}; run python -m evaluation.mvp_demo render")
        print("MVP demo results are current")
    else:
        raise SystemExit(__doc__)
