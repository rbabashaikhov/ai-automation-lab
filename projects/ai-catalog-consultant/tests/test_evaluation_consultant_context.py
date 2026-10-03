from evaluation.consultant_context import (
    QUERIES, build_user_message, parse_depth_cm, render_context, select_candidates,
)
from evaluation.dataset import load_dataset


def _chunks(spec):
    return [{"model_code": m, "section": s, "similarity": 1 - i / 100, "content": f"{m}/{s}"}
            for i, (m, s) in enumerate(spec)]


def test_select_candidates_first_k_distinct_products_in_rank_order():
    ch = _chunks([("A", "x"), ("A", "y"), ("B", "x"), ("C", "x"), ("B", "y")])
    out = select_candidates(ch, k_products=2, window=40, max_chunks=3)
    assert [c["model_code"] for c in out] == ["A", "B"]
    assert [c["product_rank"] for c in out] == [1, 2]
    assert [x["chunk_rank"] for x in out[0]["chunks"]] == [1, 2]
    assert [x["chunk_rank"] for x in out[1]["chunks"]] == [3, 5]


def test_select_candidates_window_cap_and_best_chunk_always_kept():
    ch = _chunks([("A", "1"), ("B", "1"), ("C", "1"), ("A", "2"), ("A", "3"), ("A", "4"), ("B", "2")])
    out = select_candidates(ch, k_products=3, window=4, max_chunks=2)
    assert [x["chunk_rank"] for x in out[0]["chunks"]] == [1, 4]      # cap of 2, rank<=window
    assert [x["chunk_rank"] for x in out[1]["chunks"]] == [2]         # rank 7 is beyond the window
    far = select_candidates(_chunks([("A", "1")] * 1 + [("Z", "1")] * 50 + [("B", "1")]), 3, window=5, max_chunks=3)
    assert far[2]["model_code"] == "B" and len(far[2]["chunks"]) == 1  # best chunk kept beyond window


def test_select_candidates_is_deterministic_and_handles_few_products():
    ch = _chunks([("A", "x"), ("B", "x")])
    assert select_candidates(ch, 8) == select_candidates(ch, 8)
    assert len(select_candidates(ch, 8)) == 2
    assert select_candidates([], 8) == []


def test_parse_depth_cm():
    assert parse_depth_cm("150.95 x 89.49 x 2.64") == 2.64
    assert parse_depth_cm("96.5\tx 56.3 x 7.5") == 7.5
    assert parse_depth_cm(None) is None and parse_depth_cm("n/a") is None


def test_render_separates_structured_facts_from_chunks_and_omits_nothing_invented():
    cand = [{"product_rank": 1, "model_code": "A", "chunks": [{"section": "audio", "similarity": 0.4, "content": "text"}],
             "facts": {"model_code": "A", "name": "TV A", "fields": {"refresh_rate_hz": 120}, "specs": {"Мощность звука, Вт": "70"}}}]
    txt = build_user_message("q", cand)
    assert txt.index("STRUCTURED FACTS") < txt.index("RETRIEVED CHUNKS")
    assert "refresh_rate_hz: 120" in txt and "Мощность звука, Вт: 70" in txt and "similarity is not a quality score" in txt
    assert "sale_price" not in txt  # absent values are never fabricated
    assert render_context(cand) in txt


def test_spike_queries_are_the_five_phase_3d2_weak_cases():
    ds = {c.query for c in load_dataset()}
    assert len(QUERIES) == 5 and set(QUERIES) <= ds
