"""Phase 4F.2 live acceptance run: a loop of the unchanged live tooling over ``acceptance_scenarios.json``.

    python -m evaluation.acceptance_run driver-create <tmp_dir>                  # one temporary inactive driver -> id
    python -m evaluation.acceptance_run run <out_dir> <driver_id> <run_id> [PA-.. ...]
    python -m evaluation.acceptance_run driver-delete <driver_id>

Evaluation-only. The conversations are driven by ``model_bakeoff.run_case`` (temporary inactive driver with the
committed Consultant workflow inline, ``n8n execute``, trace extraction through the n8n API): nothing in
``consultant/``, the deployed workflow, the Consultant container or the database is touched.

* One session per scenario (``pa4f2-<run_id>-<scenario>``); all turns of a scenario run in one ``n8n execute``
  process, so the Agent's window memory carries that conversation and no other.
* The model is the committed one: ``run_case`` passes ``gpt-4.1-mini``, which leaves the inline workflow identical
  to the committed file (asserted by ``tests/test_agent_live_unit.py``). Every turn's trace records what n8n sent.
* Each scenario is executed exactly once. A failed execution stops the run and is classified by hand
  (``model_bakeoff.EXECUTION_FAILURE_RULE``); an infrastructure re-execution gets a new session (``-xN``).
* Session isolation is recorded, not assumed: for every turn the output of the workflow's own "Prior turns" node
  (the Agent's memory read before the turn) is stored as ``prior_user_turns``.

A confirmation run (design document §4.2) is the same command with another ``run_id`` and the failed scenario ids.
"""
import json
import sys
import time
from pathlib import Path

from . import acceptance
from . import model_bakeoff as mb
from .agent_live import fetch

MODEL = "gpt-4.1-mini"                       # the committed production model; never overridden
SET_NAME = "acceptance"
PRIOR_TURNS_NODE = "Prior turns"


def prior_user_turns(sub_execution: dict):
    """Earlier user turns the Agent's memory held when the turn started (what the workflow sends as ``h``)."""
    runs = sub_execution["data"]["resultData"]["runData"].get(PRIOR_TURNS_NODE) or []
    items = ((runs[0].get("data") or {}).get("main") or [[]])[0] if runs else []
    messages = (items[0].get("json") or {}).get("messages") if items else None
    return sum(1 for g in messages if g.get("human") is not None) if isinstance(messages, list) else None


def run(out: Path, driver_id: str, run_id: str, only=()) -> dict:
    data = acceptance.load()
    acceptance.validate(data)
    ids = [c["id"] for c in data["cases"] if not only or c["id"] in only]
    assert ids and set(only) <= {c["id"] for c in data["cases"]}, only
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    log, done, t0 = [], [], time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for sid in ids:
        trace_file = out / SET_NAME / f"{sid}.trace.json"
        if trace_file.exists():                                      # resume: a scenario is never executed twice
            done.append(sid)
            continue
        again = len(list((out / SET_NAME).glob(f"{sid}.failed-*.json")))
        session = f"pa4f2-{run_id}-{sid}" + (f"-x{again}" if again else "")
        try:
            rec = mb.run_case(MODEL, SET_NAME, acceptance.DATASET, sid, session, driver_id, out, log)
        except mb.ExecutionFailed as e:
            (out / f"stopped-{int(time.time())}.json").write_text(json.dumps(
                {"run_id": run_id, "started": t0, "completed": done, "stopped_at": sid, "infrastructure_events": log},
                ensure_ascii=False, indent=1))
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
    record = {"run_id": run_id, "started": t0, "finished": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "model": MODEL, "scenarios": done, "infrastructure_events": log}
    (out / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))
    return record


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "driver-create":
        print(mb.driver_create(Path(sys.argv[2])))
    elif cmd == "driver-delete":
        mb.driver_delete(sys.argv[2])
    elif cmd == "run":
        r = run(Path(sys.argv[2]), sys.argv[3], sys.argv[4], tuple(sys.argv[5:]))
        print("DONE", r["run_id"], len(r["scenarios"]), "scenarios; infrastructure events:", len(r["infrastructure_events"]))
    else:
        raise SystemExit(__doc__)
