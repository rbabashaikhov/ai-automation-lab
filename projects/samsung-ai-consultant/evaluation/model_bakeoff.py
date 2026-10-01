"""Model bake-off gate: run the frozen Samsung Consultant runtime with only the chat model changed.

Nothing here is a second evaluation framework: cases are driven, extracted and scored by ``agent_live`` /
``agent_eval``. This module only fixes the experiment (models, case sets, order) and loops over it.

    python -m evaluation.model_bakeoff manifest                       # frozen configuration + sha256 (run first)
    python -m evaluation.model_bakeoff driver-create                  # one temporary inactive driver -> its id
    python -m evaluation.model_bakeoff run <model> <stage> <out> <driver_id> [run_tag]
    python -m evaluation.model_bakeoff analyze <out>/<model>/<stage>[-<run_tag>] <catalog_codes.txt>
    python -m evaluation.model_bakeoff driver-delete <driver_id>

Isolation: the model id is replaced in the driver's *inline copy* of ``workflows/ai-consultant.json``
(``agent_live.with_model``). The deployed Consultant workflow, its credentials and the Consultant container are
never changed, so production never switches model. Every turn's trace records the model, temperature and API mode
n8n actually sent; a turn answered by another model fails the run.

Stages (fixed before the first run; identical for every model):

* ``stage1`` -- ``STAGE1_FROZEN`` (19 of the 42 frozen cases) plus every case of ``bakeoff_cases.json``;
* ``full``   -- all 42 cases of ``agent_cases.json`` (survivors of stage 1 only);
* ``smoke``  -- ``no-tool-greeting`` only: tooling / model-access check, never scored.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from consultant.agent_tools import DEFAULT_MAX_TOOL_CALLS_PER_TURN, TOOL_SCHEMAS
from consultant.n8n_workflow import AGENT_MAX_ITERATIONS, MEMORY_WINDOW

from . import agent_live
from .agent_live import DRIVER_NAME, MODEL_PRICES, PROJECT, REPO, _api, analyze, build_driver, extract, load_catalog_codes

MODELS = ("gpt-4.1-mini", "gpt-4o-mini", "gpt-4.1")           # baseline first; the order of the runs
BASELINE = MODELS[0]
FROZEN_CASES = PROJECT / "evaluation/agent_cases.json"
SUPPLEMENT_CASES = PROJECT / "evaluation/bakeoff_cases.json"
# Chosen by what each case exercises, before any run: device intent (PS5), price-sensitive recommendation, no-match,
# thin_wall, weak-evidence recommendation, vague request, comparison of two models (codes and families), product-code
# questions (overview, not-listed feature, unknown code), price basis, catalog list, catalog aggregates, invention
# traps, a general-knowledge turn, and the one multi-turn case of the frozen set.
STAGE1_FROZEN = ("rec-gaming", "rec-budget-gaming", "rec-impossible", "rec-thin-wall", "rec-movies", "rec-vague",
                 "compare-exact", "compare-family-ambiguous", "get-tv-exact", "get-tv-not-listed-feature",
                 "get-tv-unknown-model", "search-list-price", "search-under-150k", "stats-count-oled",
                 "stats-largest-oled-tie", "adv-invent-price", "adv-brightness", "no-tool-oled-explainer",
                 "followup-oled65-spike")
# Registered before the first run. Stage 1 only removes a clearly weaker model; the decision is made on stage 2.
ELIMINATION_RULE = ("A model is eliminated after stage 1 if, on the same stage-1 turns and by manual verdict, it "
                    "(a) passes at least 3 turns fewer than the best model, or (b) states a fabricated model, price or "
                    "specification in any answer, or (c) has at least 2 more turns than the best model where an explicit "
                    "user constraint is lost or an invented hard constraint reaches the Core.")
# Registered before the first run. Provider-side failures say nothing about a model's answers.
EXECUTION_FAILURE_RULE = ("An execution that fails stops the run. If the cause is infrastructure (provider quota, billing or "
                          "rate limit, transport, n8n API), the case is executed again from its first turn in a new session "
                          "and the event is recorded; it is not a model failure. If the cause is the model's own output "
                          "(for example an Agent error after its tool calls), the turn is scored as failed and not repeated.")
N8N_CONTAINER, SSH_HOST = "n8n-compose-n8n-1", "n8n-vps"


class ExecutionFailed(RuntimeError):
    pass
N8N_TOOL = REPO / "tools/n8n-tool"
FROZEN_FILES = ("evaluation/agent_cases.json", "evaluation/bakeoff_cases.json", "consultant/prompts/agent_system_v3.md",
                "workflows/ai-consultant.json", "evaluation/agent_eval.py", "evaluation/agent_live.py",
                "evaluation/model_bakeoff.py", "consultant/semantic_guard.py", "consultant/agent_tools.py")


def _ids(path: Path) -> list:
    return [c["id"] for c in json.loads(path.read_text())["cases"]]


def plan(stage: str) -> list:
    """``[(set name, dataset path, case id)]`` in run order."""
    if stage == "smoke":
        return [("frozen", FROZEN_CASES, "no-tool-greeting")]
    if stage == "stage1":
        missing = set(STAGE1_FROZEN) - set(_ids(FROZEN_CASES))
        assert not missing, missing
        return [("frozen", FROZEN_CASES, c) for c in STAGE1_FROZEN] + [("supplement", SUPPLEMENT_CASES, c)
                                                                      for c in _ids(SUPPLEMENT_CASES)]
    if stage == "full":
        return [("frozen", FROZEN_CASES, c) for c in _ids(FROZEN_CASES)]
    raise SystemExit(f"unknown stage {stage}")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest() -> dict:
    wf = json.loads((PROJECT / "workflows/ai-consultant.json").read_text())
    node = next(n for n in wf["nodes"] if n["name"] == agent_live.MODEL_NODE)
    turns = lambda stage: sum(len(agent_live.cases(p)[c]["turns"]) for _, p, c in plan(stage))   # noqa: E731
    return {
        "models": list(MODELS), "baseline": BASELINE, "provider": "OpenAI (n8n credential 'OpenAI account', unchanged)",
        "only_variable": "parameters.model.value / cachedResultName of the 'OpenAI Chat Model' node in the driver's inline copy",
        "committed_model_node": {"type": node["type"], "typeVersion": node["typeVersion"], "parameters": node["parameters"]},
        "constants": {"prompt": "consultant/prompts/agent_system_v3.md", "temperature": node["parameters"]["options"]["temperature"],
                      "max_tokens": "not set (provider default) for every model", "memory_window": MEMORY_WINDOW,
                      "agent_max_iterations": AGENT_MAX_ITERATIONS, "tool_call_cap_per_turn": DEFAULT_MAX_TOOL_CALLS_PER_TURN,
                      "tool_schemas_sha256_16": hashlib.sha256(json.dumps(TOOL_SCHEMAS, sort_keys=True, ensure_ascii=False)
                                                               .encode()).hexdigest()[:16]},
        "frozen_files_sha256": {f: _sha(PROJECT / f) for f in FROZEN_FILES},
        "stages": {s: {"cases": len(plan(s)), "turns": turns(s),
                       "multi_turn_cases": sum(len(agent_live.cases(p)[c]["turns"]) > 1 for _, p, c in plan(s)),
                       "case_ids": [c for _, _, c in plan(s)]} for s in ("stage1", "full")},
        "stage1_case_ids_sha256": hashlib.sha256("\n".join(c for _, _, c in plan("stage1")).encode()).hexdigest(),
        "elimination_rule": ELIMINATION_RULE, "execution_failure_rule": EXECUTION_FAILURE_RULE,
        "prices_usd_per_1m_tokens": {m: [round(a * 1e6, 4), round(b * 1e6, 4)] for m, (a, b) in MODEL_PRICES.items()},
    }


# ---- temporary driver ---------------------------------------------------------------------------------

def _n8n_tool(*args: str) -> str:
    r = subprocess.run([sys.executable, "-m", "n8n_tool", "workflows", *args], cwd=N8N_TOOL, capture_output=True,
                       text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError(f"n8n-tool {args[0]} failed: {(r.stdout + r.stderr)[-400:]}")
    return r.stdout


def _request(method: str, path: str):
    base, key = _api()
    req = urllib.request.Request(f"{base}/api/v1{path}", method=method, headers={"X-N8N-API-KEY": key})
    return json.load(urllib.request.urlopen(req, timeout=60))


def driver_create(tmp: Path) -> str:
    f = tmp / "driver.json"
    f.write_text(json.dumps(build_driver("no-tool-greeting", "bake-create"), ensure_ascii=False, indent=1))
    _n8n_tool("create", str(f), "--yes")
    found = [w for w in _request("GET", "/workflows?limit=250")["data"] if w["name"] == DRIVER_NAME]
    assert len(found) == 1 and not found[0]["active"], [w["id"] for w in found]
    return found[0]["id"]


def driver_delete(driver_id: str) -> None:
    """Refuses anything that is not the temporary, inactive evaluation driver."""
    wf = _request("GET", f"/workflows/{driver_id}")
    if wf["name"] != DRIVER_NAME or wf.get("active"):
        raise SystemExit(f"refusing to delete {driver_id}: {wf['name']!r} active={wf.get('active')}")
    _request("DELETE", f"/workflows/{driver_id}")


# ---- runs ---------------------------------------------------------------------------------------------

def _retry(what: str, fn, log: list, attempts: int = 4):
    """Infrastructure retries only (API / SSH transport); every retry is recorded."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:                                       # noqa: BLE001
            log.append({"step": what, "attempt": attempt, "error": f"{type(e).__name__}: {str(e)[:200]}"})
            if attempt == attempts:
                raise
            time.sleep(5 * attempt)


def _gaps() -> tuple:
    """``(seconds between cases, seconds between turns)`` from BAKEOFF_CASE_GAP_S / BAKEOFF_TURN_GAP_S (default 0, 0).
    Pacing for a provider tokens-per-minute limit: it changes when a turn starts, never what the model receives."""
    return int(os.environ.get("BAKEOFF_CASE_GAP_S", "0")), int(os.environ.get("BAKEOFF_TURN_GAP_S", "0"))


def run_case(model: str, set_name: str, cases_path: Path, case_id: str, session: str, driver_id: str, out: Path,
             log: list) -> dict:
    d = out / set_name
    d.mkdir(parents=True, exist_ok=True)
    driver_file, parent_out, trace = d / f"{case_id}.driver.json", d / f"{case_id}.parent.out", d / f"{case_id}.trace.json"
    driver_file.write_text(json.dumps(build_driver(case_id, session, model, cases_path, _gaps()[1]), ensure_ascii=False,
                                      indent=1))
    _retry(f"{case_id}: driver update", lambda: _n8n_tool("update", driver_id, str(driver_file), "--yes"), log)
    # The case is executed exactly once: a failed execution is recorded, not repeated.
    started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    r = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=15", "-o",
                        "ServerAliveCountMax=4", SSH_HOST, "docker", "exec", "-e", "N8N_RUNNERS_BROKER_PORT=5699",
                        N8N_CONTAINER, "n8n", "execute", "--id", driver_id, "--rawOutput"],
                       capture_output=True, text=True, timeout=600)
    parent_out.write_text(r.stdout)
    if "{\n" not in r.stdout:
        log.append({"step": f"{case_id}: execute", "returncode": r.returncode, "stderr": r.stderr[-300:]})
        raise RuntimeError(f"{case_id}: no execution output (rc={r.returncode})")
    rec = _retry(f"{case_id}: extract", lambda: extract(case_id, parent_out, trace, cases_path), log)
    rec.update(model=model, session=session, started=started)
    trace.write_text(json.dumps(rec, ensure_ascii=False, indent=1))
    failed = [t for t in rec["turns"] if t.get("missing") or t.get("status") != "success"]
    if failed:
        # Not scored here and never retried automatically: the run stops and the cause is classified by hand
        # (EXECUTION_FAILURE_RULE). The record is kept; resume re-executes the case only after it is set aside.
        kept = d / f"{case_id}.failed-{int(time.time())}.json"
        trace.rename(kept)
        why = [t.get("parent_error") or t.get("error") or t.get("status") for t in failed]
        log.append({"step": f"{case_id}: execution failed", "record": kept.name, "errors": why})
        raise ExecutionFailed(f"{case_id}: {why}")
    driver_file.unlink()
    parent_out.unlink()                                             # the trace keeps everything scoring needs
    return rec


def run(model: str, stage: str, out_root: Path, driver_id: str, run_tag: str = "") -> dict:
    assert model in MODELS, model
    name = stage + (f"-{run_tag}" if run_tag else "")
    out = Path(out_root) / model / name
    out.mkdir(parents=True, exist_ok=True)
    log, done = [], []
    t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for set_name, cases_path, case_id in plan(stage):
        if (out / set_name / f"{case_id}.trace.json").exists():      # resume after an interruption: never re-run a case
            done.append(case_id)
            continue
        if _gaps()[0] and done:
            time.sleep(_gaps()[0])
        again = len(list((out / set_name).glob(f"{case_id}.failed-*.json")))     # infrastructure re-execution: new session
        session = f"bake-{model.replace('.', '')}-{name}-{case_id}" + (f"-x{again}" if again else "")
        try:
            rec = run_case(model, set_name, cases_path, case_id, session, driver_id, out, log)
        except ExecutionFailed as e:
            (out / f"stopped-{int(time.time())}.json").write_text(json.dumps(
                {"model": model, "stage": name, "started": t0, "completed_cases": done, "stopped_at": case_id,
                 "infrastructure_events": log}, ensure_ascii=False, indent=1))
            raise SystemExit(f"STOPPED {model} {name}: {e}")
        wrong = [t.get("models") for t in rec["turns"] if t.get("models") not in ([model], [], None)]
        if wrong:
            raise SystemExit(f"{case_id}: answered by {wrong}, expected {model}")
        print(case_id, [(t.get("status"), t.get("latency_s"), t.get("model_rounds"), t.get("models")) for t in rec["turns"]],
              flush=True)
        done.append(case_id)
    record = {"model": model, "stage": name, "started": t0, "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "cases": done, "infrastructure_events": log, "case_gap_s": _gaps()[0], "turn_gap_s": _gaps()[1]}
    (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
    return record


def analyze_run(out: Path, catalog_codes: frozenset) -> dict:
    """``agent_live.analyze`` per case set of a run directory (``frozen`` / ``supplement``)."""
    result = {}
    for set_name, path in (("frozen", FROZEN_CASES), ("supplement", SUPPLEMENT_CASES)):
        if (Path(out) / set_name).is_dir():
            result[set_name] = analyze(Path(out) / set_name, catalog_codes, path)["summary"]
    return result


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "manifest":
        print(json.dumps(manifest(), ensure_ascii=False, indent=1))
    elif cmd == "driver-create":
        print(driver_create(Path(sys.argv[2])))
    elif cmd == "driver-delete":
        driver_delete(sys.argv[2])
    elif cmd == "run":
        rec = run(sys.argv[2], sys.argv[3], Path(sys.argv[4]), sys.argv[5], sys.argv[6] if len(sys.argv) > 6 else "")
        print("DONE", rec["model"], rec["stage"], len(rec["cases"]), "infrastructure events:", len(rec["infrastructure_events"]))
    elif cmd == "analyze":
        print(json.dumps(analyze_run(Path(sys.argv[2]), load_catalog_codes(Path(sys.argv[3]))), ensure_ascii=False, indent=1))
    else:
        raise SystemExit(__doc__)
