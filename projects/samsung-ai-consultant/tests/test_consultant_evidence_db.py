"""Phase 4C against a disposable PostgreSQL+pgvector container (tests/run_db_tests.sh).

Real chunk text from the indexing builder; synthetic deterministic vectors; no OpenAI.
ResolvedPlan -> 4B structured retrieval -> 4C evidence -> deterministic renderer.
"""

import psycopg2
import pytest

from consultant.catalog_repository import CatalogRepository, open_readonly_connection
from consultant.evidence import build_evidence, serialize_for_llm
from consultant.render import render_debug
from consultant.retrieval import run
from consultant.schemas import QueryPlanDelta
from consultant.semantic import CachedQueryEmbeddings

from .consultant_fixtures import seed, seed_chunks, section_vector
from .db_fixtures import TEST_DATABASE_URL, db_conn, requires_db  # noqa: F401

pytestmark = requires_db


@pytest.fixture
def env(db_conn):  # noqa: F811
    ids = seed(db_conn)
    chunk_ids = seed_chunks(db_conn, ids)
    return CatalogRepository(db_conn), ids, chunk_ids, db_conn


class SpyEmbedder(CachedQueryEmbeddings):
    """Cached vectors only; counts every lookup (there is no network embedder in the package)."""


def build(repo, raw, emb=None, **kw):
    plan, rp, res = run(QueryPlanDelta.from_dict(raw), repo)
    return build_evidence(plan, rp, res, repo, emb, **kw), plan, res


# ---- repository vector / section access ----------------------------------------------------------

def test_vector_in_products_is_restricted_and_ordered(env):
    repo, ids, _, _ = env
    allowed = [ids["QE65S95HAUXPY"], ids["QE65S90HAEXPY"]]
    hits = repo.vector_in_products(allowed, section_vector("audio"))
    assert hits and {h.product_id for h in hits} <= set(allowed)
    sims = [h.similarity for h in hits]
    assert sims == sorted(sims, reverse=True)
    assert hits[0].section == "audio"


def test_section_restriction_and_allowlist(env):
    repo, ids, _, _ = env
    hits = repo.vector_in_products([ids["QE65S95HAUXPY"]], section_vector("audio"), ["gaming", "display"])
    assert {h.section for h in hits} <= {"gaming", "display"}
    with pytest.raises(KeyError):
        repo.vector_in_products([ids["QE65S95HAUXPY"]], section_vector("audio"), ["price"])
    with pytest.raises(ValueError):
        repo.vector_in_products([ids["QE65S95HAUXPY"]], [0.1] * 10)
    assert repo.vector_in_products([], section_vector("audio")) == []


def test_null_embeddings_are_excluded(env):
    repo, ids, chunk_ids, _ = env
    hits = repo.vector_in_products([ids["UE32H5000FUXRU"]], section_vector("physical_design"))
    assert chunk_ids[("UE32H5000FUXRU", "physical_design")] not in {h.chunk_id for h in hits}
    assert repo.chunks_by_section([ids["UE32H5000FUXRU"]], ["physical_design"])     # section lookup still sees it


def test_deterministic_section_lookup(env):
    repo, ids, chunk_ids, _ = env
    rows = repo.chunks_by_section([ids["QE65S95HAUXPY"], ids["QE65S90HAEXPY"]], ["audio", "gaming"])
    assert {(r.product_id, r.section) for r in rows} == {
        (ids["QE65S95HAUXPY"], "audio"), (ids["QE65S95HAUXPY"], "gaming"),
        (ids["QE65S90HAEXPY"], "audio"), (ids["QE65S90HAEXPY"], "gaming")}
    assert all(r.similarity is None for r in rows)


def test_global_fallback_uses_unchanged_function_with_availability_only(env):
    repo, ids, _, _ = env
    hits = repo.vector_global(section_vector("gaming"), available_only=True)
    assert hits and hits[0].section == "gaming"
    assert ids["QE55QN1EHAUXPY"] not in {h.product_id for h in hits}        # unavailable excluded


def test_vector_queries_run_in_a_readonly_session(env):
    repo, ids, _, _ = env
    conn = open_readonly_connection(TEST_DATABASE_URL)
    try:
        ro = CatalogRepository(conn)
        assert ro.vector_in_products([ids["QE65S95HAUXPY"]], section_vector("audio"))
        assert ro.chunks_by_section([ids["QE65S95HAUXPY"]], ["overview"])
        with conn.cursor() as cur, pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
            cur.execute("UPDATE chunks SET content = 'x'")
    finally:
        conn.rollback()
        conn.close()


# ---- section lookup needs no embeddings ------------------------------------------------------------

@pytest.mark.parametrize("raw,sections", [
    ({"intent": "recommend", "use_cases": ["gaming"]}, {"gaming", "display"}),
    ({"intent": "recommend", "use_cases": ["sound"]}, {"audio"}),
    ({"intent": "recommend", "use_cases": ["movies"]}, {"display", "audio"}),
    ({"intent": "recommend", "use_cases": ["thin_wall"]}, {"physical_design"}),
    ({"intent": "spec_question", "model_refs": [{"text": "QE65S95HAUXPY", "kind": "full"}],
      "attributes_asked": ["earc"]}, {"connectivity"}),
])
def test_mapped_needs_use_section_lookup_with_zero_embeddings(env, raw, sections):
    repo = env[0]
    emb = SpyEmbedder({})
    b, _, _ = build(repo, raw, emb)
    passages = [x for p in b.products for x in p.passages]
    assert passages and {x.section for x in passages} <= sections
    assert {x.retrieval for x in passages} == {"section_lookup"}
    assert (emb.hits, emb.misses) == (0, 0) and b.semantic["embedding_lookups"] == 0


# ---- candidate-scoped semantic --------------------------------------------------------------------------

def test_candidate_scoped_never_escapes_and_only_breaks_fit_ties(env):
    repo = env[0]
    need = "что-то с хорошим звуком"
    emb = SpyEmbedder({need: section_vector("audio")})
    raw = {"intent": "recommend", "constraints": {"panel_technology": ["OLED"]}, "use_cases": ["gaming"],
           "free_text_need": need}
    b, plan, res = build(repo, raw, emb)
    candidates = {p.id for p in res.candidates}
    assert b.semantic["path"] == "candidate_scoped" and b.semantic["escaped_products"] == 0
    assert {p.product_id for p in b.products} <= candidates
    assert all(x.product_id in candidates for p in b.products for x in p.passages)
    assert {x.retrieval for p in b.products for x in p.passages} == {"vector"}
    # structured fit is never overridden: preferred counts are non-increasing in the final order
    fits = [p.ranking_debug["preferred_matched"] for p in b.products]
    assert fits == sorted(fits, reverse=True)
    assert emb.hits == 1
    # structural confidence: tied top fit band wider than the shortlist -> weak; else vector-only need -> partial
    if b.totals["top_fit_band"] > 8:
        assert b.retrieval_confidence == "weak" and "top_fit_band_exceeds_shortlist" in b.confidence_reasons
    else:
        assert b.retrieval_confidence == "partial"
        assert "free_text_need_backed_by_vector_passages_only" in b.confidence_reasons


def test_semantic_tiebreak_is_off_by_default_and_opt_in(env):
    repo = env[0]
    need = "что-то с хорошим звуком"
    raw = {"intent": "recommend", "constraints": {"panel_technology": ["OLED"]}, "use_cases": ["gaming"],
           "free_text_need": need}
    emb = SpyEmbedder({need: section_vector("audio")})
    default, _, res = build(repo, raw, emb)
    assert default.semantic["tiebreak"] is None
    assert [p.model_code for p in default.products] == [c.product.model_code for c in res.shortlist.items]
    plan, rp, res = run(QueryPlanDelta.from_dict(raw), repo)
    opted = build_evidence(plan, rp, res, repo, emb, apply_semantic_tiebreak=True)
    tb = opted.semantic["tiebreak"]
    assert tb and tb["structured_order"] == res.ranking.ranked_codes()
    assert sorted(tb["final_order"]) == sorted(tb["structured_order"])        # a permutation, nothing added
    fits = [p.ranking_debug["preferred_matched"] for p in opted.products]
    assert fits == sorted(fits, reverse=True)                                  # never across unequal fit


def test_missing_cached_vector_is_a_gap_not_a_call(env):
    repo = env[0]
    emb = SpyEmbedder({})
    b, _, _ = build(repo, {"intent": "recommend", "constraints": {"panel_technology": ["OLED"]},
                           "free_text_need": "без кэша"}, emb)
    assert "semantic_unavailable" in {g.kind for g in b.gaps} and emb.misses == 1
    assert all(x.retrieval == "section_lookup" for p in b.products for x in p.passages)


# ---- product-scoped semantic + absence rule --------------------------------------------------------------

def _spec_q(repo, code, need, emb):
    return build(repo, {"intent": "spec_question", "model_refs": [{"text": code, "kind": "full"}],
                        "free_text_need": need}, emb)[0]


def test_product_scoped_known_long_tail_spec_present(env):
    repo = env[0]
    emb = SpyEmbedder({"Dolby Atmos": section_vector("audio")})
    b = _spec_q(repo, "QE65S95HAUXPY", "Dolby Atmos", emb)
    p = b.products[0]
    assert b.route == "PRODUCT_SCOPED_SEMANTIC" and b.semantic["path"] == "product_scoped"
    assert any(f.label == "Поддержка форматов звука" and "Atmos" in f.value for f in p.facts)
    assert len(p.passages) <= 3 and p.passages[0].section == "audio"
    assert not {"attribute_not_listed_for_product", "attribute_absence_unverified"} & {g.kind for g in b.gaps}


def test_product_scoped_verified_absence(env):
    repo = env[0]
    b = _spec_q(repo, "UE32H5000FUXRU", "FreeSync", SpyEmbedder({}))
    kinds = {g.kind for g in b.gaps}
    assert "attribute_not_listed_for_product" in kinds and "semantic_unavailable" in kinds


def test_term_unknown_to_catalog_is_unverified_not_absent(env):
    repo = env[0]
    b = _spec_q(repo, "QE65S95HAUXPY", "Bixby", SpyEmbedder({"Bixby": section_vector("smart_features")}))
    kinds = {g.kind for g in b.gaps}
    assert "attribute_absence_unverified" in kinds and "attribute_not_listed_for_product" not in kinds


def test_semantic_miss_is_not_absence(env):
    """The vector for the need points at the wrong section (retrieval 'fails'), but the product's
    specs do list it: the probe finds it, and no absence gap is produced."""
    repo = env[0]
    b = _spec_q(repo, "QE65S95HAUXPY", "FreeSync", SpyEmbedder({"FreeSync": section_vector("physical_design")}))
    p = b.products[0]
    assert p.passages[0].section == "physical_design"                         # semantic 'miss'
    assert any("FreeSync" in f.value for f in p.facts)                        # verified present
    assert not {"attribute_not_listed_for_product", "attribute_absence_unverified"} & {g.kind for g in b.gaps}


# ---- evidence safety -----------------------------------------------------------------------------------------

def test_live_price_wins_over_stale_chunk_text(env):
    repo, ids, _, conn = env
    with conn.cursor() as cur:        # disposable DB only: price changes after indexing
        cur.execute("UPDATE products SET sale_price = 299990 WHERE id = %s", (ids["QE65S95HAUXPY"],))
    conn.commit()
    b, _, _ = build(repo, {"intent": "lookup", "model_refs": [{"text": "QE65S95HAUXPY", "kind": "full"}]})
    p = b.products[0]
    facts = {f.fact_id: f.value for f in p.facts}
    assert facts["P1.col.effective_price"] == "299990"
    ov = p.passages[0]
    assert ov.section == "overview" and ov.stripped_lines == 2
    assert "Цена:" not in ov.text and "Наличие:" not in ov.text and "329990" not in ov.text
    s = serialize_for_llm(b)
    assert "329990" not in s and "299990" in s


def test_llm_serialization_is_clean(env):
    repo, ids, _, _ = env
    need = "звук"
    b, _, _ = build(repo, {"intent": "recommend", "use_cases": ["sound"], "free_text_need": need},
                    SpyEmbedder({need: section_vector("audio")}))
    s = serialize_for_llm(b)
    for forbidden in ("similarity", "raw_payload", "description", '"product_id"', "external_id", "galaxystore.ru",
                      "structured_rank", str(ids["QE65S95HAUXPY"]) + ","):
        assert forbidden not in s
    assert "similarity=" not in s and any(x.similarity is not None for p in b.products for x in p.passages)


def test_gaps_preserved_bright_room_and_budget(env):
    repo = env[0]
    b, _, _ = build(repo, {"intent": "recommend", "use_cases": ["bright_room"]})
    kinds = {g.kind for g in b.gaps}
    assert {"not_in_catalog_domain", "excluded_unavailable", "excluded_out_of_scope"} <= kinds
    tiny, _, _ = build(repo, {"intent": "recommend", "use_cases": ["bright_room"]}, token_budget=50)
    assert tiny.budget["over_budget"] and {g.kind for g in tiny.gaps} == kinds
    assert len(tiny.products) == len(b.products) and not any(p.passages for p in tiny.products)


def test_data_quality_gap_for_shown_product(env):
    repo = env[0]
    b, _, _ = build(repo, {"intent": "lookup", "model_refs": [{"text": "QE55LS03HAUXPY", "kind": "full"}]})
    assert b.products and not b.gaps                      # lookup does not evaluate depth
    b, _, _ = build(repo, {"intent": "recommend", "constraints": {"category": ["The Frame"],
                                                                  "screen_size_inches": 55},
                           "use_cases": ["thin_wall"]})
    dq = [g for g in b.gaps if g.kind == "data_quality"]
    assert dq and dq[0].code == "malformed_component" and dq[0].handles == ("P1",)


# ---- relaxation --------------------------------------------------------------------------------------------------

def test_relaxation_impossible_budget(env):
    repo = env[0]
    b, _, _ = build(repo, {"intent": "recommend", "constraints": {"panel_technology": ["OLED"],
                                                                  "effective_price": {"max": 30000}}})
    assert not b.products and b.alternatives and "no_product_satisfies" in {g.kind for g in b.gaps}
    probes = {p["dropped"]: p for p in b.totals["relaxation"]}
    assert probes["effective_price"]["matches"] > 0 and probes["panel_technology"]["matches"] > 0
    for a in b.alternatives:
        violated = [c for c in a.constraints if c.satisfied is False]
        assert violated and all(r.startswith("violates:") for r in a.selection_reasons)


def test_relaxation_impossible_size_uses_nearest(env):
    repo = env[0]
    b, _, _ = build(repo, {"intent": "recommend", "constraints": {"panel_technology": ["OLED"],
                                                                  "screen_size_inches": 50}})
    size_alts = [a for a in b.alternatives if "violates:screen_size_inches" in a.selection_reasons]
    sizes = [float({f.fact_id.split('.', 1)[1]: f.value for f in a.facts}["col.screen_size_inches"]) for a in size_alts]
    assert sizes and sizes[0] in (48.0, 55.0)             # nearest OLED sizes to 50", not the cheapest


def test_relaxation_required_feature_not_listed(env):
    repo = env[0]
    b, _, _ = build(repo, {"intent": "recommend", "constraints": {"panel_technology": ["OLED"]},
                           "features": [{"id": "hdmi_2_1", "strength": "required"}]})
    assert not b.products and "required_feature_not_listed" in {g.kind for g in b.gaps}
    assert any(r.startswith("required_not_listed:") for a in b.alternatives for r in a.selection_reasons)
    assert b.retrieval_confidence == "weak"


# ---- global fallback + renderer ---------------------------------------------------------------------------------

def test_global_fallback_respects_scope_and_is_weak(env):
    repo = env[0]
    need = "что-нибудь уютное"
    b, _, _ = build(repo, {"intent": "recommend", "free_text_need": need}, SpyEmbedder({need: section_vector("audio")}))
    assert b.route == "SEMANTIC_FALLBACK" and b.retrieval_confidence == "weak"
    assert b.products and all(p.model_code not in ("MNA114MS1CCXRU", "UE27LSM7FAXXPY") for p in b.products)
    assert all(len(p.passages) <= 2 for p in b.products)


def test_debug_renderer(env):
    repo = env[0]
    b, _, _ = build(repo, {"intent": "recommend", "constraints": {"panel_technology": ["OLED"],
                                                                  "screen_size_inches": 65}, "use_cases": ["gaming"]})
    text = render_debug(b)
    for needle in ("P1", "price:", "available:", "constraint [ok]", "feature hz_120", "fact P1.col.effective_price",
                   "passage #", "confidence:"):
        assert needle in text
