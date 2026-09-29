import json

import pytest

from evaluation.dataset import load_dataset
from evaluation.run_baseline import (
    EMBEDDING_DIMS, compose_hybrid, extract_query_embeddings, expected_ranks, load_cache, queries_needing_embedding,
    sql_only_discriminates,
)
from evaluation.scoring import ChunkHit


def test_compose_hybrid_filters_and_keeps_similarity_order():
    chunks = [ChunkHit("X", "gaming", .9), ChunkHit("A", "gaming", .8), ChunkHit("B", "audio", .7),
              ChunkHit("A", "audio", .6)]
    out = compose_hybrid(chunks, ["A", "B"])
    assert [(c.model_code, c.similarity) for c in out] == [("A", .8), ("B", .7), ("A", .6)]
    assert compose_hybrid(chunks, []) == []


def test_sql_only_discriminates():
    assert not sql_only_discriminates(["A", "B", "C"], ["A", "B", "C"])  # filter alone suffices
    assert sql_only_discriminates(["A", "B", "C"], ["A"])


def test_hybrid_oled_65_ps5_filter_alone_satisfies_ground_truth():
    # documents the accepted 3D.1 ground truth: this case cannot discriminate SQL-only from hybrid
    c = {x.id: x for x in load_dataset()}
    same = set(c["structured-oled-65"].relevant_product_ids)
    assert not sql_only_discriminates(same, c["hybrid-oled-65-ps5"].relevant_product_ids)


def test_expected_ranks():
    assert expected_ranks(["X", "X", "A"], ["A", "B"]) == {"A": 2, "B": None}


def test_queries_needing_embedding_excludes_sql_and_aggregate_and_dedupes():
    qs = queries_needing_embedding(load_dataset())
    assert len(qs) == len(set(qs)) == 12
    assert "самый дешёвый телевизор" not in qs and "QE65S95HAUXPY" not in qs


def test_load_cache(tmp_path):
    assert load_cache(tmp_path / "missing.json") == {}
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"q": [0.0] * EMBEDDING_DIMS}))
    assert list(load_cache(p)) == ["q"]
    p.write_text(json.dumps({"q": [0.0] * 3}))
    with pytest.raises(ValueError):
        load_cache(p)


def _exec(queries, dims=EMBEDDING_DIMS, model="text-embedding-3-small", status="success", n_out=None):
    emit = [{"json": {"query_text": q}} for q in queries]
    outs = [{"json": {"model": model, "data": [{"embedding": [0.0] * dims}]}, "pairedItem": {"item": i}}
            for i in range(len(queries) if n_out is None else n_out)]
    return {"status": status, "data": {"resultData": {"runData": {
        "Emit evaluation queries": [{"data": {"main": [emit]}}],
        "OpenAI: Embed query": [{"data": {"main": [outs]}}]}}}}


def test_extract_query_embeddings_verifies_execution():
    assert sorted(extract_query_embeddings(_exec(["a", "b"]), ["b", "a"])) == ["a", "b"]
    with pytest.raises(ValueError, match="differ"):
        extract_query_embeddings(_exec(["a"]), ["a", "b"])
    with pytest.raises(ValueError, match="differ"):
        extract_query_embeddings(_exec(["a", "a"]), ["a", "b"])
    with pytest.raises(ValueError, match="embedding outputs"):
        extract_query_embeddings(_exec(["a", "b"], n_out=1), ["a", "b"])
    with pytest.raises(ValueError, match="dimensions"):
        extract_query_embeddings(_exec(["a"], dims=3), ["a"])
    with pytest.raises(ValueError, match="unexpected embedding"):
        extract_query_embeddings(_exec(["a"], model="other"), ["a"])
    with pytest.raises(ValueError, match="status"):
        extract_query_embeddings(_exec(["a"], status="error"), ["a"])
