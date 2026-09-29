"""Phase 4C unit tests (no DB): evidence contract, fact ids, sanitization, serialization,
budget, confidence, semantic helpers, lexical probe and relaxation logic."""

import pytest

from consultant.evidence import (
    EvidenceBundle, FactItem, Gap, Passage, ProductEvidence, approx_tokens, classify_confidence, column_facts,
    constraint_statuses, enforce_budget, fact_id, feature_statuses, sanitize_passage, section_priority,
    serialize_for_llm, to_llm_payload,
)
from consultant.features import FEATURES, evaluate_features
from consultant.planning import resolve_plan
from consultant.ranking import rank_candidates
from consultant.relaxation import alternative_rows, describe, relax
from consultant.schemas import (
    FeatureResult, FeatureState, Filters, ProductKind, ProductRow, QueryPlanDelta, Range, Route,
)
from consultant.semantic import (
    CachedQueryEmbeddings, EmbeddingUnavailable, best_similarity_by_product, catalog_coverage, lexical_probe,
    mentions, probe_terms, semantic_tiebreak,
)
from consultant.vocabulary import build_vocabulary

from .consultant_fixtures import load_raw, product_rows

YES, NL = FeatureState.YES, FeatureState.NOT_LISTED
OVERVIEW = ('Samsung Телевизор Samsung 65" OLED S95H\nМодель: QE65S95HAUXPY\nКатегория: OLED\n'
            'Цена: 329990 RUB (без скидки 349990 RUB)\nНаличие: в наличии\n\nПроцессор: NQ4 AI Gen3 Processor')


# ---- sanitization / facts --------------------------------------------------------------------------

def test_stale_price_and_availability_lines_are_stripped():
    text, removed = sanitize_passage(OVERVIEW)
    assert removed == 2 and "Цена:" not in text and "Наличие:" not in text
    assert "Процессор: NQ4 AI Gen3 Processor" in text and "Модель: QE65S95HAUXPY" in text


def test_non_price_lines_mentioning_price_words_survive():
    text, removed = sanitize_passage("Режим: Цена не указана в этой строке\nЦена: 1")
    assert removed == 1 and text == "Режим: Цена не указана в этой строке"


def test_fact_ids_are_deterministic_and_safe():
    assert fact_id("P1", "col", "effective_price") == "P1.col.effective_price"
    assert fact_id("P2", "spec", "versia_hdmi") == "P2.spec.versia_hdmi"
    for kind, key in (("col", "price; drop"), ("text", "x"), ("spec", "Версия HDMI")):
        with pytest.raises(ValueError):
            fact_id("P1", kind, key)


def test_column_facts_use_live_row_and_omit_missing():
    rows, _, by_code = product_rows()
    qn80 = by_code["QE75QN80HAUXPY"]
    facts = {f.fact_id: f for f in column_facts("P1", qn80)}
    assert facts["P1.col.effective_price"].value == "189990"          # live sale price, not chunk text
    assert facts["P1.col.price"].value == "229990" and facts["P1.col.is_available"].value == "yes"
    no_sale = by_code["QE65S90HAEXPY"]
    assert "P1.col.sale_price" not in {f.fact_id for f in column_facts("P1", no_sale)}


def test_feature_status_links_to_fact_ids():
    rows, specs, by_code = product_rows()
    p = by_code["QE65S95HAUXPY"]
    res = evaluate_features([p], specs, ["hz_120", "vrr", "allm"])[p.id]
    keys = {s.spec_name: s.spec_key for s in specs[p.id]}
    st = {f.feature_id: f for f in feature_statuses("P1", res, keys)}
    assert st["hz_120"].fact_ids == ("P1.col.refresh_rate_hz",)
    assert st["vrr"].fact_ids[0].startswith("P1.spec.") and st["vrr"].state == "yes"
    assert st["allm"].state == "not_listed" and st["allm"].fact_ids == ()       # not_listed stays distinct


def _vocab():
    return build_vocabulary([(p["model_code"], p["series"], p["panel_technology"], p["category"],
                              p["resolution"], p["screen_size_inches"], p["year"]) for p in load_raw()])


class _PlanRepo:
    def __init__(self):
        self.v = _vocab()

    def vocabulary(self):
        return self.v

    def resolve_model_refs(self, refs, vocab=None):
        return []


def _plan(raw):
    return resolve_plan(QueryPlanDelta.from_dict(raw), _PlanRepo())


def test_constraint_status_from_live_values():
    rows, _, by_code = product_rows()
    plan = _plan({"intent": "recommend", "constraints": {"panel_technology": ["Neo QLED"],
                                                         "effective_price": {"max": 200000}}})
    ok = {c.key: c for c in constraint_statuses(plan, by_code["QE75QN80HAUXPY"])}
    assert ok["effective_price"].satisfied and ok["panel_technology"].satisfied
    bad = {c.key: c for c in constraint_statuses(plan, by_code["QE65S95HAUXPY"])}
    assert bad["effective_price"].satisfied is False and bad["effective_price"].actual == "329990"


def test_section_priority_follows_mapped_features():
    assert section_priority(_plan({"intent": "recommend", "use_cases": ["gaming"]})) == ["gaming", "display"]
    assert section_priority(_plan({"intent": "recommend", "use_cases": ["sound"]})) == ["audio"]
    assert section_priority(_plan({"intent": "recommend", "use_cases": ["thin_wall"]})) == ["physical_design"]


# ---- serialization / budget / confidence -------------------------------------------------------------

def _bundle(n_products=3, passages=2, gaps=(Gap("not_in_catalog_domain", "no brightness data"),)):
    prods = []
    for i in range(1, n_products + 1):
        ps = tuple(Passage(100 * i + j, i, "galaxystore", f"ext{i}", "display", "Текст " * 200, "vector", 0.91)
                   for j in range(passages))
        prods.append(ProductEvidence(f"P{i}", i, "galaxystore", f"ext{i}", f"CODE{i}", f"Name {i}", "https://x/",
                                     (FactItem(f"P{i}.col.effective_price", "Цена", "100", "RUB", "products"),),
                                     (), (), ps, ("hard_constraints_met",), {"structured_rank": i, "fit": 3}))
    return EvidenceBundle("evidence-v1", "recommend", "CONSTRAINT_FIRST", "R6", {"intent": "recommend"},
                          tuple(prods), (), tuple(gaps), {"shown": n_products}, "strong", (), {}, {})


def test_llm_serialization_excludes_diagnostics_and_internal_ids():
    b = _bundle()
    s = serialize_for_llm(b)
    for forbidden in ("similarity", "0.91", "ext1", "https://x/", "structured_rank", "fit", "product_id",
                      "raw_payload", "description"):
        assert forbidden not in s
    payload = to_llm_payload(b)
    assert payload["products"][0]["handle"] == "P1" and payload["gaps"][0]["kind"] == "not_in_catalog_domain"


def test_budget_trims_passages_never_products_or_gaps():
    b = _bundle(n_products=4, passages=2)
    full = approx_tokens(b)
    small = enforce_budget(b, limit=full // 2)
    assert small.budget["trimmed_passages"] > 0 and len(small.products) == 4
    assert small.gaps == b.gaps and all(p.facts for p in small.products)
    kept = [len(p.passages) for p in small.products]
    assert kept == sorted(kept, reverse=True)            # lowest-ranked products lose passages first
    tiny = enforce_budget(b, limit=1)
    assert tiny.budget["over_budget"] and sum(len(p.passages) for p in tiny.products) == 0 and tiny.gaps


def test_confidence_is_structural():
    none = ()
    assert classify_confidence(Route.CONSTRAINT_FIRST, True, none, False, False)[0] == "strong"
    assert classify_confidence(Route.CONSTRAINT_FIRST, True, (Gap("required_feature_not_listed", "x"),),
                               False, False)[0] == "partial"
    assert classify_confidence(Route.CONSTRAINT_FIRST, True, none, False, True)[0] == "partial"
    assert classify_confidence(Route.CONSTRAINT_FIRST, True, none, True, False)[0] == "weak"
    assert classify_confidence(Route.SEMANTIC_FALLBACK, True, none, False, False)[0] == "weak"
    assert classify_confidence(Route.SQL_FILTER, False, none, False, False)[0] == "weak"
    assert classify_confidence(Route.CLARIFY, False, none, False, False)[0] == "not_applicable"
    # excluded-unavailable is information, not uncertainty
    assert classify_confidence(Route.SQL_FILTER, True, (Gap("excluded_unavailable", "3"),), False, False)[0] == "strong"


# ---- semantic helpers -------------------------------------------------------------------------------

def test_cached_embeddings_never_generate():
    emb = CachedQueryEmbeddings({"q": [0.0] * 1536})
    assert len(emb.embed("q")) == 1536
    with pytest.raises(EmbeddingUnavailable):
        emb.embed("not cached")
    assert (emb.hits, emb.misses) == (1, 1)
    with pytest.raises(ValueError):
        CachedQueryEmbeddings({"q": [0.0] * 10})


def _row(i, price):
    return ProductRow(i, "galaxystore", f"{i:03d}", f"M{i:03d}", f"Телевизор {i}", "OLED", None, ProductKind.TV,
                      2026, 55.0, "3840x2160", "OLED", 120, price, None, price, "RUB", True, None)


def test_semantic_tiebreak_only_reorders_inside_equal_fit():
    ps = [_row(1, 100), _row(2, 200), _row(3, 300), _row(4, 50)]
    fr = {1: {"a": FeatureResult("a", YES)}, 2: {"a": FeatureResult("a", YES)}, 3: {"a": FeatureResult("a", YES)},
          4: {"a": FeatureResult("a", NL)}}
    r = rank_candidates(ps, fr, preferred=["a"])
    assert r.ranked_codes() == ["M001", "M002", "M003", "M004"]
    # product 4 has the best similarity but lower fit: it must not move above the tie group
    new, rep, _ = semantic_tiebreak(r, {1: 0.1, 2: 0.2, 3: 0.3, 4: 0.99})
    assert new.ranked_codes() == ["M003", "M002", "M001", "M004"]
    assert [c.rank for c in new.qualified] == [1, 2, 3, 4]
    assert {x.model_code: (x.structured_rank, x.final_rank) for x in rep}["M003"] == (3, 1)
    assert {x.model_code: x.tie_group_size for x in rep}["M004"] == 1
    assert r.ranked_codes() == ["M001", "M002", "M003", "M004"]       # input ranking untouched


def test_unscored_products_keep_structured_order_after_scored():
    ps = [_row(1, 100), _row(2, 200), _row(3, 300)]
    fr = {p.id: {} for p in ps}
    new, _, _ = semantic_tiebreak(rank_candidates(ps, fr), {3: 0.5})
    assert new.ranked_codes() == ["M003", "M001", "M002"]


def test_best_similarity_by_product():
    class C:
        def __init__(self, pid, sim):
            self.product_id, self.similarity = pid, sim
    assert best_similarity_by_product([C(1, 0.2), C(1, 0.5), C(2, 0.1)]) == {1: 0.5, 2: 0.1}


def test_probe_terms_and_boundaries():
    assert probe_terms("Есть ли у модели Bixby?") == ("bixby",)
    assert probe_terms("Wi-Fi 6E") == ("wi-fi", "6e")
    assert probe_terms("голосовой помощник") == ("голосо", "помощн")
    assert not mentions("2.1", "Вес, кг: 22.1") and mentions("2.1", "Версия HDMI: 2.1")
    assert not mentions("6e", "x: 16e") and mentions("6e", "Стандарты Wi-Fi: Wi-Fi 6E")
    assert not mentions("голосо", "Способы управления: Пульт ДУ; Смартфон; Голос")


def _spec(pid, name, value):
    from consultant.schemas import SpecRow
    return SpecRow(pid, "g", name, "k", value)


def test_lexical_probe_statuses():
    specs = [_spec(1, "Поддержка приложения AirPlay", "Да"), _spec(1, "HDMI", "4 шт")]
    assert lexical_probe(("airplay",), specs, {"airplay": 2}).status == "mentioned_in_specs"
    partial = lexical_probe(("hdmi", "2.1"), specs, {"hdmi": 75, "2.1": 1})
    assert partial.status == "mentioned_in_specs" and not partial.all_terms_matched
    other = [_spec(2, "HDMI", "3 шт")]
    assert lexical_probe(("airplay",), other, {"airplay": 2}).status == "verified_not_listed"
    assert lexical_probe(("bixby",), other, {"bixby": 0}).status == "absence_unverified"
    # a partially known need never becomes a verified absence
    assert lexical_probe(("airplay", "bixby"), other, {"airplay": 2, "bixby": 0}).status == "absence_unverified"


def test_stem_mismatch_is_unverified_not_absent():
    """'голосовой помощник' vs catalog wording 'Голос': the probe cannot match, and since the
    catalog never uses 'голосо…', the result is uncertainty -- not a false 'not listed'."""
    specs = [_spec(1, "Способы управления", "Пульт ДУ; Смартфон; Голос")]
    cov = {t: catalog_coverage(t, specs) for t in probe_terms("голосовой помощник")}
    assert lexical_probe(probe_terms("голосовой помощник"), specs, cov).status == "absence_unverified"


# ---- relaxation (fake repository) -----------------------------------------------------------------------

class _RelaxRepo:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def count(self, f):
        return [(None, len(self.rows))]

    def search(self, f, sort=None, limit=3, cap=50):
        self.calls.append(("search", sort))
        return type("R", (), {"rows": self.rows[:limit]})()

    def nearest(self, f, key, target, limit=3):
        self.calls.append(("nearest", key, target))
        return self.rows[:limit]


def test_relaxation_drops_one_constraint_at_a_time():
    rows, _, by_code = product_rows()
    plan = _plan({"intent": "recommend", "constraints": {"panel_technology": ["OLED"], "screen_size_inches": 20,
                                                         "effective_price": {"max": 30000}}})
    repo = _RelaxRepo([by_code["QE42S90HAEXPY"], by_code["QE48S85HAEXPY"]])
    probes = relax(plan, repo)
    assert [p.dropped for p in probes] == ["panel_technology", "screen_size_inches", "effective_price"]
    assert probes[1].requested == "screen_size_inches = 20" and probes[1].actual_values == (42.0, 48.0)
    kinds = [c[0] for c in repo.calls]
    assert kinds == ["search", "nearest", "search"]                 # exact size -> nearest by distance
    assert repo.calls[2][1][1].value == "asc"                       # dropped upper bound -> ascending
    alts = alternative_rows(probes)
    assert alts[0][1] == ["panel_technology", "screen_size_inches", "effective_price"]
    assert describe("effective_price", Filters(effective_price=Range(None, 30000))) == "effective_price <= 30000"

