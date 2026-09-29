"""Score supplied retrieval results against dataset cases and summarize.

Nothing here runs retrieval: callers pass a :class:`CaseRun` per case.
Per-family semantics (see evaluation/README.md):

* ``sql_sufficient``: pass iff the returned set equals the expected set.
* ``aggregate_not_retrieval``: pass iff the set matches AND no vector/hybrid
  retrieval was used to produce it.
* ``vector_primary``: rank metrics; pass iff an expected product is in the top ``PASS_K``.
* ``hybrid``: pass iff the filter check holds (when the admitted set was
  supplied) AND an expected product is in the top ``PASS_K``.
* ``difficult_ambiguous``: metrics are reported but never gate (status ``informational``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from . import metrics
from .dataset import FAMILIES, EvalCase

KS = (1, 3, 5)
PASS_K = 5
VECTOR_MECHANISMS = ("vector", "hybrid")


@dataclass(frozen=True)
class ChunkHit:
    model_code: str
    section: Optional[str] = None
    similarity: Optional[float] = None


@dataclass(frozen=True)
class CaseRun:
    """What a retrieval run produced for one case.

    ``mechanism_used``: "sql" | "vector" | "hybrid" | "none".
    ``products``: final ranked model_codes (SQL result set, or reranked list).
    When omitted, it is derived from ``chunks`` (first occurrence order).
    ``filter_admitted``: the SQL-filtered candidate set, for hybrid runs.
    """

    case_id: str
    mechanism_used: str = "none"
    products: Optional[Sequence[str]] = None
    chunks: Sequence[ChunkHit] = ()
    filter_admitted: Optional[Sequence[str]] = None

    def ranked_products(self) -> list:
        if self.products is not None:
            return list(self.products)
        return [c.model_code for c in self.chunks]


@dataclass
class CaseResult:
    case_id: str
    family: str
    status: str  # pass | fail | informational | not_run
    metrics: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)  # (name, ok: bool|None, detail)

    def render(self) -> str:
        lines = [f"[{self.status.upper():13}] {self.case_id}  ({self.family})"]
        for name, ok, detail in self.checks:
            mark = {True: "ok", False: "FAIL", None: "n/a"}[ok]
            lines.append(f"    {name:<26} {mark:<4} {detail}".rstrip())
        for k, v in self.metrics.items():
            lines.append(f"    {k:<26} {v}")
        return "\n".join(lines)


def _rank_metrics(ranked: Sequence[str], relevant: Sequence[str]) -> dict:
    m = {f"hit@{k}": metrics.hit_at_k(ranked, relevant, k) for k in KS}
    m["mrr"] = metrics.reciprocal_rank(ranked, relevant)
    m["first_relevant_rank"] = metrics.first_relevant_rank(ranked, relevant)
    return m


def score_case(case: EvalCase, run: Optional[CaseRun]) -> CaseResult:
    fam = case.family
    if run is None:
        return CaseResult(case.id, fam, "not_run")
    if run.case_id != case.id:
        raise ValueError(f"run for {run.case_id!r} supplied to case {case.id!r}")
    ranked = run.ranked_products()
    rel = list(case.relevant_product_ids)
    res = CaseResult(case.id, fam, "fail")
    res.metrics["mechanism_used"] = run.mechanism_used
    res.metrics["expected_mechanism"] = case.expected_mechanism

    if fam in ("sql_sufficient", "aggregate_not_retrieval"):
        sm = metrics.set_match(ranked, rel)
        res.metrics.update({k: sm[k] for k in ("precision", "recall")})
        res.checks.append(("set_match", sm["exact"],
                           f"missing={sm['missing']} unexpected={sm['unexpected']}"))
        gates = [sm["exact"]]
        if fam == "aggregate_not_retrieval":
            ok = run.mechanism_used not in VECTOR_MECHANISMS
            res.checks.append(("no_vector_retrieval", ok, f"mechanism_used={run.mechanism_used}"))
            gates.append(ok)
        res.status = "pass" if all(gates) else "fail"
        return res

    res.metrics.update(_rank_metrics(ranked, rel))
    if run.chunks:
        res.metrics.update(metrics.chunk_metrics(run.chunks, rel, case.expected_sections))
    top = res.metrics["hit@%d" % PASS_K]
    res.checks.append((f"expected_in_top_{PASS_K}", top, f"first_rank={res.metrics['first_relevant_rank']}"))

    if fam == "difficult_ambiguous":
        res.status = "informational"
        return res
    gates = [top]
    if fam == "hybrid":
        if run.filter_admitted is None:
            res.checks.append(("filter_correctness", None, "admitted set not supplied"))
        else:
            fc = metrics.filter_correctness(ranked, run.filter_admitted, rel)
            res.checks.append(("filter_correctness", fc["ok"],
                               f"outside={fc['returned_outside_filter']} "
                               f"excluded={fc['expected_excluded_by_filter']}"))
            gates.append(fc["ok"])
    res.status = "pass" if all(gates) else "fail"
    return res


def score_run(cases: Sequence[EvalCase], runs: Sequence[CaseRun]) -> list:
    """Score every case (file order). Runs for unknown/duplicate case ids raise."""
    by_id = {}
    known = {c.id for c in cases}
    for r in runs:
        if r.case_id not in known:
            raise ValueError(f"run for unknown case {r.case_id!r}")
        if r.case_id in by_id:
            raise ValueError(f"duplicate run for case {r.case_id!r}")
        by_id[r.case_id] = r
    return [score_case(c, by_id.get(c.id)) for c in cases]


def _mean(xs: Sequence[float]) -> Optional[float]:
    return round(sum(xs) / len(xs), 4) if xs else None


def summarize(results: Sequence[CaseResult]) -> dict:
    """Aggregate per family and overall. Rank metrics are macro-averaged over
    the cases that have them (vector_primary, hybrid, difficult_ambiguous);
    ``informational`` cases are excluded from pass rates."""

    def block(rs):
        run = [r for r in rs if r.status != "not_run"]
        gated = [r for r in run if r.status in ("pass", "fail")]
        ranked = [r for r in run if "mrr" in r.metrics]
        out = {
            "cases": len(rs),
            "run": len(run),
            "pass": sum(r.status == "pass" for r in rs),
            "fail": sum(r.status == "fail" for r in rs),
            "informational": sum(r.status == "informational" for r in rs),
            "not_run": sum(r.status == "not_run" for r in rs),
            "pass_rate": _mean([r.status == "pass" for r in gated]),
            "rank_cases": len(ranked),
            "mrr": _mean([r.metrics["mrr"] for r in ranked]),
        }
        for k in KS:
            out[f"hit@{k}"] = _mean([r.metrics[f"hit@{k}"] for r in ranked])
        return out

    return {
        "overall": block(list(results)),
        "by_family": {f: block([r for r in results if r.family == f]) for f in FAMILIES},
    }


def render_summary(summary: dict) -> str:
    cols = ("cases", "run", "pass", "fail", "informational", "not_run",
            "pass_rate", "hit@1", "hit@3", "hit@5", "mrr")
    rows = [("overall", summary["overall"])] + list(summary["by_family"].items())
    head = f"{'':<26}" + "".join(f"{c:>14}" for c in cols)
    body = ["".join([f"{name:<26}"] + [f"{'-' if b[c] is None else b[c]:>14}" for c in cols])
            for name, b in rows]
    return "\n".join([head] + body)
