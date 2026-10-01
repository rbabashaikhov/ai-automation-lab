"""Phase 4F.1 product acceptance suite: validation, coverage and the generated part of the design document.

    python -m evaluation.acceptance             # validate, print coverage
    python -m evaluation.acceptance render      # rewrite the generated blocks of docs/PHASE_4F_PRODUCT_ACCEPTANCE.md
    python -m evaluation.acceptance --check     # fail if one of them is stale
    python -m evaluation.acceptance manifest    # sha256 of the dataset and of the frozen product files

``acceptance_scenarios.json`` is the single source of the scenarios. Its ``cases`` list has the shape the
unchanged live tooling reads (``agent_live.build_driver`` / ``extract`` / ``analyze`` with ``cases_path``), so
Phase 4F.2 can drive the conversations without touching the runtime; ``scenarios`` holds the manual rubric.

Offline only: no database, n8n or LLM. Nothing in ``consultant/`` imports this module.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

from .agent_eval import validate_cases

PROJECT = Path(__file__).resolve().parents[1]
DATASET = PROJECT / "evaluation/acceptance_scenarios.json"
DOCUMENT = PROJECT / "docs/PHASE_4F_PRODUCT_ACCEPTANCE.md"
MARKERS = "<!-- BEGIN GENERATED: {} -->", "<!-- END GENERATED: {} -->"
# Earlier evaluation sets: an acceptance turn must not be copied from them.
EARLIER_DATASETS = ("agent_cases.json", "bakeoff_cases.json", "guard_cases.json", "semantic_cases.json",
                    "retrieval_cases.json")
# What Phase 4F.2 compares against at preflight: the product is frozen.
FROZEN_FILES = ("consultant/prompts/agent_system_v3.md", "workflows/ai-consultant.json", "consultant/semantic_guard.py",
                "consultant/agent_tools.py", "evaluation/agent_eval.py", "evaluation/agent_live.py")

SCENARIO_COUNT = (12, 15)
CATEGORY_COUNTS = {"A": (2, 3), "B": (3, 4), "C": (2, 2), "D": (2, 3), "E": (1, 2), "F": (1, 2), "G": (1, 15)}
TURNS_PER_SCENARIO = (2, 5)                 # the Agent's memory window is 6 exchanges
MIN_LONG_SCENARIOS = 4                      # "at least several" scenarios of 3-5 turns
MIN_SCENARIOS_PER_DIMENSION = 2
REQUIRED_FIELDS = ("scenario_id", "title", "category", "dimensions", "purpose", "constraint_state", "expected_behavior",
                   "forbidden_behavior", "grounding_expectation", "pass_criteria", "severity_if_failed")
OPTIONAL_FIELDS = ("historical_failure_modes", "design_notes")
STATE_KEYS = {"after_turn", "hard", "soft", "replaced", "released", "note"}


class AcceptanceError(ValueError):
    pass


def load(path: Path = DATASET) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def earlier_texts() -> frozenset:
    return frozenset(s.strip() for name in EARLIER_DATASETS for s in _strings(load(PROJECT / "evaluation" / name)))


def primary_category(scenario: dict) -> str:
    return scenario["category"][0]


def validate(data: dict) -> None:
    """Structure, unique ids, required fields, and the coverage the Phase 4F.1 brief asks for."""
    meta, cases, scenarios = data["_meta"], data["cases"], data["scenarios"]
    levels, classes = meta["severity_levels"], meta["failure_classes"]
    for cid, c in classes.items():
        if c.get("default_severity") not in levels or not c.get("name") or not c.get("group"):
            raise AcceptanceError(f"failure class {cid}: needs group, name and a known default_severity")
    for check in meta["global_checks"]:
        if set(check["classes"]) - set(classes):
            raise AcceptanceError(f"{check['id']}: unknown failure class")

    try:
        validate_cases(cases)                       # closed keys, known tools: the shape agent_live reads
    except Exception as e:
        raise AcceptanceError(f"cases: {e}") from None
    ids = [c["id"] for c in cases]
    if [s.get("scenario_id") for s in scenarios] != ids:
        raise AcceptanceError("scenarios and cases must list the same ids in the same order")
    if not SCENARIO_COUNT[0] <= len(ids) <= SCENARIO_COUNT[1]:
        raise AcceptanceError(f"{len(ids)} scenarios, expected {SCENARIO_COUNT[0]}-{SCENARIO_COUNT[1]}")

    users = [t["user"].strip() for c in cases for t in c["turns"]]
    if len(set(users)) != len(users):
        raise AcceptanceError("duplicate user turn")
    copied = sorted(set(users) & earlier_texts())
    if copied:
        raise AcceptanceError(f"user turn copied from an earlier evaluation set: {copied}")

    used_modes = set()
    for case, s in zip(cases, scenarios):
        sid, n = s["scenario_id"], len(case["turns"])
        if not re.fullmatch(r"PA-\d{2}", sid):
            raise AcceptanceError(f"{sid}: id must look like PA-01")
        unknown = set(s) - set(REQUIRED_FIELDS) - set(OPTIONAL_FIELDS)
        missing = [f for f in REQUIRED_FIELDS if not s.get(f)]
        if unknown or missing:
            raise AcceptanceError(f"{sid}: unknown fields {sorted(unknown)}, missing or empty {missing}")
        if not TURNS_PER_SCENARIO[0] <= n <= TURNS_PER_SCENARIO[1]:
            raise AcceptanceError(f"{sid}: {n} turns, expected {TURNS_PER_SCENARIO[0]}-{TURNS_PER_SCENARIO[1]}")
        if set(s["category"]) - set(meta["categories"]) or len(set(s["category"])) != len(s["category"]):
            raise AcceptanceError(f"{sid}: bad category {s['category']}")
        if set(s["dimensions"]) - set(meta["dimensions"]) or len(set(s["dimensions"])) != len(s["dimensions"]):
            raise AcceptanceError(f"{sid}: bad dimensions {s['dimensions']}")
        modes = s.get("historical_failure_modes", [])
        if set(modes) - set(meta["historical_failure_modes"]):
            raise AcceptanceError(f"{sid}: unknown historical failure mode")
        used_modes |= set(modes)
        states = s["constraint_state"]
        if [st.get("after_turn") for st in states] != list(range(1, n + 1)):
            raise AcceptanceError(f"{sid}: constraint_state needs one entry per turn, in order")
        for st in states:
            if set(st) - STATE_KEYS or not isinstance(st.get("hard"), list) or not isinstance(st.get("soft"), list):
                raise AcceptanceError(f"{sid}: bad constraint_state entry after turn {st.get('after_turn')}")
        for field in ("expected_behavior", "grounding_expectation", "pass_criteria"):
            if not all(isinstance(x, str) and x.strip() for x in s[field]):
                raise AcceptanceError(f"{sid}: {field} must be a list of non-empty strings")
        for f in s["forbidden_behavior"]:
            if set(f) != {"behavior", "class", "severity"} or f["class"] not in classes or f["severity"] not in levels:
                raise AcceptanceError(f"{sid}: bad forbidden_behavior entry {f}")
        sev = s["severity_if_failed"]
        worst = min((f["severity"] for f in s["forbidden_behavior"]), key=levels.index)
        if set(sev) != {"worst_case", "default"} or sev["worst_case"] != worst or sev["default"] not in levels:
            raise AcceptanceError(f"{sid}: severity_if_failed must give default and worst_case = {worst}")

    unused = sorted(set(meta["historical_failure_modes"]) - used_modes)
    if unused:
        raise AcceptanceError(f"historical failure modes without a scenario: {unused}")
    cov = coverage(data)
    for cat, (lo, hi) in CATEGORY_COUNTS.items():
        if not lo <= len(cov["categories_primary"][cat]) <= hi:
            raise AcceptanceError(f"category {cat}: {len(cov['categories_primary'][cat])} primary scenarios, expected {lo}-{hi}")
    thin = [d for d, sids in cov["dimensions"].items() if len(sids) < MIN_SCENARIOS_PER_DIMENSION]
    if thin:
        raise AcceptanceError(f"dimensions with fewer than {MIN_SCENARIOS_PER_DIMENSION} scenarios: {thin}")
    if cov["scenarios_with_3_to_5_turns"] < MIN_LONG_SCENARIOS:
        raise AcceptanceError("too few scenarios with 3-5 turns")


def coverage(data: dict) -> dict:
    meta, turns = data["_meta"], {c["id"]: len(c["turns"]) for c in data["cases"]}
    scenarios = data["scenarios"]
    return {
        "scenarios": len(scenarios), "turns": sum(turns.values()),
        "multi_turn_scenarios": sum(1 for n in turns.values() if n >= 2),
        "scenarios_with_3_to_5_turns": sum(1 for n in turns.values() if 3 <= n <= 5),
        "categories_primary": {c: [s["scenario_id"] for s in scenarios if primary_category(s) == c]
                               for c in meta["categories"]},
        "categories_any": {c: [s["scenario_id"] for s in scenarios if c in s["category"]] for c in meta["categories"]},
        "dimensions": {d: [s["scenario_id"] for s in scenarios if d in s["dimensions"]] for d in meta["dimensions"]},
        "historical_failure_modes": {h: [s["scenario_id"] for s in scenarios if h in s.get("historical_failure_modes", [])]
                                     for h in meta["historical_failure_modes"]},
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def manifest() -> dict:
    return {"dataset": {"evaluation/acceptance_scenarios.json": sha256(DATASET)},
            "frozen_files": {f: sha256(PROJECT / f) for f in FROZEN_FILES}}


# ---- document ------------------------------------------------------------------------------------

def _cell(text: str) -> str:
    return str(text).replace("|", "\\|")


def _tool(turn: dict) -> str:
    names = [turn["tool"], *turn.get("acceptable_tools", [])]
    return ", ".join("no tool" if t is None else f"`{t}`" for t in names)


def render_classes(data: dict) -> list:
    out = ["| Class | Group | Failure | Default severity | Escalation / note |", "|---|---|---|---|---|"]
    for cid, c in data["_meta"]["failure_classes"].items():
        out.append(f"| {cid} | {c['group']} | {_cell(c['name'])} | {c['default_severity']} | {_cell(c.get('note', '—'))} |")
    return out


def render_checks(data: dict) -> list:
    out = ["| Check | Every turn of every scenario | Failure classes |", "|---|---|---|"]
    for g in data["_meta"]["global_checks"]:
        out.append(f"| {g['id']} | {_cell(g['text'])} | {', '.join(g['classes'])} |")
    return out


def render_history(data: dict) -> list:
    cov = coverage(data)["historical_failure_modes"]
    out = ["| Id | Known failure mode | Where it was seen | Scenarios |", "|---|---|---|---|"]
    for hid, h in data["_meta"]["historical_failure_modes"].items():
        out.append(f"| {hid} | {_cell(h['summary'])} | {_cell(h['source'])} | {', '.join(cov[hid])} |")
    return out


def render_scenarios(data: dict) -> list:
    """Dataset identity, summary, coverage, and one card per scenario."""
    meta, cov = data["_meta"], coverage(data)
    cases = {c["id"]: c for c in data["cases"]}
    out = [f"Dataset `{meta['version']}`, sha256 `{sha256(DATASET)}`: {cov['scenarios']} scenarios, {cov['turns']} user "
           f"turns, {cov['multi_turn_scenarios']} multi-turn, {cov['scenarios_with_3_to_5_turns']} with 3–5 turns.", "",
           "### 6.1 Scenario summary", "",
           "| Id | Scenario | Category | Turns | Dimensions | Listed failures: blocker / major / minor |",
           "|---|---|---|---|---|---|"]
    for s in data["scenarios"]:
        listed = [sum(1 for f in s["forbidden_behavior"] if f["severity"] == level) for level in meta["severity_levels"]]
        out.append(f"| {s['scenario_id']} | {_cell(s['title'])} | {', '.join(s['category'])} | "
                   f"{len(cases[s['scenario_id']]['turns'])} | {' '.join(s['dimensions'])} | "
                   f"{' / '.join(map(str, listed))} |")
    out += ["", "### 6.2 Category coverage", "",
            "The first category of a scenario is its primary one; the required counts apply to primary categories.", "",
            "| Category | Required | Primary | Also covered by |", "|---|---|---|---|"]
    for c, name in meta["categories"].items():
        lo, hi = CATEGORY_COUNTS[c]
        required = f"at least {lo}" if hi >= SCENARIO_COUNT[1] else (str(lo) if lo == hi else f"{lo}–{hi}")
        primary = cov["categories_primary"][c]
        also = [x for x in cov["categories_any"][c] if x not in primary]
        out.append(f"| {c}. {name} | {required} | {len(primary)}: {', '.join(primary)} | {', '.join(also) or '—'} |")
    out += ["", "### 6.3 Dimension coverage", "", "| Dimension | Scenarios | Count |", "|---|---|---|"]
    for d, name in meta["dimensions"].items():
        out.append(f"| {d}. {name} | {', '.join(cov['dimensions'][d])} | {len(cov['dimensions'][d])} |")
    out += ["", "### 6.4 Scenarios", ""]
    for s in data["scenarios"]:
        sid, case = s["scenario_id"], cases[s["scenario_id"]]
        sev = s["severity_if_failed"]
        out += [f"#### {sid} — {s['title']}", "",
                f"- **scenario_id:** {sid}",
                f"- **category:** " + ", ".join(f"{c} ({meta['categories'][c]})" for c in s["category"]),
                f"- **dimensions:** " + ", ".join(f"{d} {meta['dimensions'][d]}" for d in s["dimensions"]),
                f"- **historical failure modes:** {', '.join(s.get('historical_failure_modes', [])) or '—'}",
                f"- **severity if failed:** worst case {sev['worst_case']}; a failed pass criterion that matches no "
                f"listed forbidden behaviour is {sev['default']} unless the taxonomy (§3) says otherwise", "",
                f"**Purpose.** {s['purpose']}", "",
                "**Conversation turns**", "",
                "| Turn | User | Expected catalog capability (first is preferred) |", "|---|---|---|"]
        for i, t in enumerate(case["turns"], start=1):
            out.append(f"| {i} | {_cell(t['user'])} | {_tool(t)} |")
        out += ["", "**Relevant constraints** (state after each turn)", "",
                "| After turn | Hard: every product presented as matching must satisfy | Soft / intent | Replaced or released | Note |",
                "|---|---|---|---|---|"]
        for st in s["constraint_state"]:
            gone = [f"replaced: {x}" for x in st.get("replaced", [])] + [f"released: {x}" for x in st.get("released", [])]
            out.append(f"| {st['after_turn']} | {_cell('; '.join(st['hard']) or 'none')} | "
                       f"{_cell('; '.join(st['soft']) or '—')} | {_cell('; '.join(gone) or '—')} | {_cell(st.get('note', '—'))} |")
        out += ["", "**Expected behaviour**", ""] + [f"- {x}" for x in s["expected_behavior"]]
        out += ["", "**Forbidden behaviour**", "", "| Behaviour | Class | Severity |", "|---|---|---|"]
        out += [f"| {_cell(f['behavior'])} | {f['class']} | {f['severity']} |" for f in s["forbidden_behavior"]]
        out += ["", "**Evidence / grounding expectation**", ""] + [f"- {x}" for x in s["grounding_expectation"]]
        out += ["", "**Pass criteria** (in addition to the global checks of §5)", ""] + [f"- {x}" for x in s["pass_criteria"]]
        if s.get("design_notes"):
            out += ["", "**Design-time catalog notes** (not the oracle; see §1.3)", ""] + [f"- {x}" for x in s["design_notes"]]
        out.append("")
    return out[:-1]


# The parts of the document that restate the dataset. Everything else in it is written by hand.
BLOCKS = {"failure classes": render_classes, "global checks": render_checks,
          "historical failure modes": render_history, "acceptance scenarios": render_scenarios}


def render(data: dict, name: str) -> str:
    begin, end = (m.format(name) for m in MARKERS)
    note = "Generated from `evaluation/acceptance_scenarios.json` by `python -m evaluation.acceptance render`; not edited by hand."
    return "\n".join([begin, "", f"*{note}*", "", *BLOCKS[name](data), "", end])


def document_block(text: str, name: str) -> str:
    begin, end = (m.format(name) for m in MARKERS)
    if text.count(begin) != 1 or text.count(end) != 1:
        raise AcceptanceError(f"the document needs exactly one generated block '{name}'")
    return text[text.index(begin):text.index(end) + len(end)]


def stale_blocks(data: dict, path: Path = DOCUMENT) -> list:
    text = Path(path).read_text(encoding="utf-8")
    return [name for name in BLOCKS if document_block(text, name) != render(data, name)]


def write_document(data: dict, path: Path = DOCUMENT) -> None:
    text = Path(path).read_text(encoding="utf-8")
    for name in BLOCKS:
        text = text.replace(document_block(text, name), render(data, name))
    Path(path).write_text(text, encoding="utf-8")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else "validate"
    dataset = load()
    validate(dataset)
    if command == "validate":
        print(json.dumps(coverage(dataset), ensure_ascii=False, indent=1))
    elif command == "render":
        write_document(dataset)
        print(f"wrote {len(BLOCKS)} generated blocks of {DOCUMENT.relative_to(PROJECT)}")
    elif command == "--check":
        stale = stale_blocks(dataset)
        if stale:
            raise SystemExit(f"{DOCUMENT.relative_to(PROJECT)} is stale ({stale}): run python -m evaluation.acceptance render")
        print("acceptance document is current")
    elif command == "manifest":
        print(json.dumps(manifest(), ensure_ascii=False, indent=1))
    else:
        raise SystemExit(__doc__)
