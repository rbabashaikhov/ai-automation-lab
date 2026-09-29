import pytest

from evaluation import metrics
from evaluation.dataset import EvalCase, load_dataset
from evaluation.scoring import CaseRun, ChunkHit, render_summary, score_case, score_run, summarize


def mk(family_intent="semantic_feature_intent", mech="vector_primary", rel=("A", "B"), sections=(), cid="c"):
    return EvalCase(cid, "q", family_intent, mech, {}, tuple(rel), "n", tuple(sections))


def test_hit_at_k_and_mrr():
    ranked = ["X", "Y", "A"]
    assert not metrics.hit_at_k(ranked, ["A"], 2)
    assert metrics.hit_at_k(ranked, ["A"], 3)
    assert metrics.reciprocal_rank(ranked, ["A"]) == pytest.approx(1 / 3)
    with pytest.raises(ValueError):
        metrics.hit_at_k(ranked, ["A"], 0)


def test_multiple_acceptable_products_use_best_rank():
    assert metrics.first_relevant_rank(["X", "B", "A"], ["A", "B"]) == 2
    assert metrics.reciprocal_rank(["X", "B", "A"], ["A", "B"]) == 0.5


def test_duplicate_products_from_chunks_count_once():
    assert metrics.first_relevant_rank(["X", "X", "X", "A"], ["A"]) == 2


def test_no_match_and_empty():
    assert metrics.first_relevant_rank(["X"], ["A"]) is None
    assert metrics.reciprocal_rank(["X"], ["A"]) == 0.0
    assert not metrics.hit_at_k([], ["A"], 5)


def test_set_match():
    m = metrics.set_match(["A", "C"], ["A", "B"])
    assert not m["exact"] and m["missing"] == ["B"] and m["unexpected"] == ["C"]
    assert m["precision"] == m["recall"] == 0.5
    assert metrics.set_match(["B", "A"], ["A", "B"])["exact"]
    assert metrics.set_match([], ["A"])["precision"] == 0.0


def test_chunk_metrics_section_and_similarity():
    chunks = [ChunkHit("X", "gaming", .9), ChunkHit("A", "display", .8), ChunkHit("A", "gaming", .7)]
    m = metrics.chunk_metrics(chunks, ["A"], ["gaming"])
    assert m == {"product_chunk_rank": 2, "section_chunk_rank": 3, "best_relevant_similarity": .8}
    assert metrics.chunk_metrics(chunks, ["A"])["section_chunk_rank"] is None
    assert metrics.chunk_metrics(chunks, ["Z"])["product_chunk_rank"] is None


def test_filter_correctness():
    assert metrics.filter_correctness(["A"], ["A", "B"], ["A"])["ok"]
    bad = metrics.filter_correctness(["A", "Z"], ["A"], ["A", "B"])
    assert not bad["ok"] and bad["returned_outside_filter"] == ["Z"] and bad["expected_excluded_by_filter"] == ["B"]


def test_vector_case_pass_fail_and_not_run():
    case = mk()
    assert score_case(case, CaseRun("c", "vector", ["X", "A"])).status == "pass"
    r = score_case(case, CaseRun("c", "vector", [f"X{i}" for i in range(5)] + ["A"]))
    assert r.status == "fail" and r.metrics["hit@5"] is False
    assert score_case(case, None).status == "not_run"
    with pytest.raises(ValueError):
        score_case(case, CaseRun("other"))


def test_vector_case_derives_products_from_chunks():
    r = score_case(mk(sections=["gaming"]), CaseRun("c", "vector", chunks=[ChunkHit("X"), ChunkHit("B", "gaming", .5)]))
    assert r.metrics["hit@3"] and r.metrics["section_chunk_rank"] == 2 and r.metrics["mrr"] == 0.5


def test_sql_case_requires_exact_set():
    case = mk("structured_selection", "sql_sufficient", rel=("A", "B"))
    assert score_case(case, CaseRun("c", "sql", ["B", "A"])).status == "pass"
    assert score_case(case, CaseRun("c", "sql", ["A"])).status == "fail"
    assert "mrr" not in score_case(case, CaseRun("c", "sql", ["A"])).metrics


def test_aggregate_case_fails_if_vector_used():
    case = mk("structured_selection", "aggregate_not_retrieval", rel=("A",))
    assert score_case(case, CaseRun("c", "sql", ["A"])).status == "pass"
    r = score_case(case, CaseRun("c", "vector", ["A"]))
    assert r.status == "fail" and ("no_vector_retrieval", False, "mechanism_used=vector") in r.checks


def test_hybrid_needs_filter_and_hit():
    case = mk("hybrid", "sql_plus_vector", rel=("A",))
    ok = CaseRun("c", "hybrid", ["A", "B"], filter_admitted=["A", "B"])
    assert score_case(case, ok).status == "pass"
    leak = CaseRun("c", "hybrid", ["A", "Z"], filter_admitted=["A", "B"])
    assert score_case(case, leak).status == "fail"
    unknown = score_case(case, CaseRun("c", "hybrid", ["A"]))
    assert unknown.status == "pass" and ("filter_correctness", None, "admitted set not supplied") in unknown.checks


def test_difficult_is_informational_never_fails():
    case = mk("difficult_ambiguous", "vector_primary", rel=("A",))
    r = score_case(case, CaseRun("c", "vector", ["X"]))
    assert r.status == "informational" and r.metrics["hit@5"] is False


def test_render_is_human_readable():
    text = score_case(mk(), CaseRun("c", "vector", ["A"])).render()
    assert text.startswith("[PASS") and "hit@1" in text


def test_score_run_validates_runs_and_aggregate_is_deterministic():
    cases = load_dataset()
    ids = [c.id for c in cases]
    runs = [CaseRun(c.id, "sql", list(c.relevant_product_ids)) for c in cases]
    results = score_run(cases, runs)
    s1, s2 = summarize(results), summarize(score_run(cases, list(reversed(runs))))
    assert s1 == s2 and [r.case_id for r in results] == ids
    assert s1["overall"]["cases"] == 21 and s1["overall"]["not_run"] == 0
    # gold answers fed back as SQL results: sql/aggregate pass; vector-only mechanisms flagged where required
    assert s1["by_family"]["sql_sufficient"]["pass_rate"] == 1.0
    assert s1["by_family"]["aggregate_not_retrieval"]["pass_rate"] == 1.0
    assert "overall" in render_summary(s1)
    with pytest.raises(ValueError):
        score_run(cases, [CaseRun("nope")])
    with pytest.raises(ValueError):
        score_run(cases, [runs[0], runs[0]])


def test_summary_averages_rank_metrics_only_over_rank_cases():
    cases = [mk(cid="a", rel=("A",)), mk(cid="b", rel=("B",)), mk("structured_selection", "sql_sufficient", ("C",), cid="s")]
    runs = [CaseRun("a", "vector", ["A"]), CaseRun("b", "vector", ["X", "B"]), CaseRun("s", "sql", ["C"])]
    s = summarize(score_run(cases, runs))["overall"]
    assert s["rank_cases"] == 2 and s["mrr"] == 0.75 and s["hit@1"] == 0.5 and s["hit@3"] == 1.0
    assert s["pass_rate"] == 1.0
