import copy
import json
from collections import Counter

import pytest

from evaluation.dataset import (
    DEFAULT_DATASET_PATH, DatasetError, EXPECTED_CASE_COUNT, FAMILIES, derive_family, load_dataset, parse_dataset,
    select_cases,
)
from evaluation.catalog_check import compile_filters


@pytest.fixture(scope="module")
def cases():
    return load_dataset()


def _doc(case_overrides=None):
    base = {"id": "c1", "query": "q", "intent": "exact_lookup", "expected_mechanism": "sql_sufficient",
            "filters": {}, "relevant_product_ids": ["A"], "notes": "n"}
    base.update(case_overrides or {})
    return {"cases": [base]}


def test_loads_exactly_21_unique_cases(cases):
    assert len(cases) == EXPECTED_CASE_COUNT == 21
    assert len({c.id for c in cases}) == 21


def test_family_counts_are_stable(cases):
    counts = Counter(c.family for c in cases)
    assert set(counts) <= set(FAMILIES)
    assert counts == {"sql_sufficient": 7, "vector_primary": 6, "hybrid": 3,
                      "difficult_ambiguous": 3, "aggregate_not_retrieval": 2}


def test_derive_family_precedence():
    # aggregate mechanism wins over a difficult_ambiguous intent
    assert derive_family("difficult_ambiguous", "aggregate_not_retrieval") == "aggregate_not_retrieval"
    assert derive_family("difficult_ambiguous", "vector_primary") == "difficult_ambiguous"
    assert derive_family("hybrid", "sql_plus_vector") == "hybrid"


def test_wrong_count_rejected(cases):
    data = json.loads(DEFAULT_DATASET_PATH.read_text(encoding="utf-8"))
    data["cases"].pop()
    with pytest.raises(DatasetError, match="exactly 21"):
        parse_dataset(data)


@pytest.mark.parametrize("override,match", [
    ({"intent": "nope"}, "invalid intent"),
    ({"expected_mechanism": "magic"}, "invalid expected_mechanism"),
    ({"relevant_product_ids": []}, "non-empty list"),
    ({"relevant_product_ids": ["A", "A"]}, "duplicate"),
    ({"relevant_product_ids": "A"}, "non-empty list"),
    ({"filters": []}, "'filters'"),
    ({"query": "  "}, "'query'"),
    ({"expected_sections": ["bogus"]}, "expected_sections"),
    ({"surprise": 1}, "unknown field"),
])
def test_malformed_case_rejected(override, match):
    with pytest.raises(DatasetError, match=match):
        parse_dataset(_doc(override), expected_count=None)


def test_missing_field_and_non_object_rejected():
    doc = _doc()
    del doc["cases"][0]["notes"]
    with pytest.raises(DatasetError, match="missing required"):
        parse_dataset(doc, expected_count=None)
    with pytest.raises(DatasetError, match="must be an object"):
        parse_dataset({"cases": ["x"]}, expected_count=None)
    with pytest.raises(DatasetError, match="'cases' list"):
        parse_dataset({}, expected_count=None)


def test_duplicate_ids_rejected():
    doc = _doc()
    doc["cases"].append(copy.deepcopy(doc["cases"][0]))
    with pytest.raises(DatasetError, match="duplicate case id"):
        parse_dataset(doc, expected_count=None)


def test_invalid_json_rejected(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{nope", encoding="utf-8")
    with pytest.raises(DatasetError, match="invalid JSON"):
        load_dataset(p)


def test_select_by_family_and_id(cases):
    assert len(select_cases(cases, families=["hybrid"])) == 3
    assert [c.id for c in select_cases(cases, case_ids=["exact-model-code"])] == ["exact-model-code"]
    assert select_cases(cases, families=["hybrid"], case_ids=["hybrid-oled-65-ps5"])[0].id == "hybrid-oled-65-ps5"
    with pytest.raises(DatasetError):
        select_cases(cases, families=["bogus"])
    with pytest.raises(DatasetError):
        select_cases(cases, case_ids=["missing"])


def test_every_dataset_filter_is_compilable(cases):
    # a new filter key added to the dataset must be supported by catalog_check
    for c in cases:
        compile_filters(c.filters)


def test_compile_filters_rejects_unknown_key():
    with pytest.raises(KeyError):
        compile_filters({"colour": "red"})
    cf = compile_filters({"panel_technology": "OLED", "max_effective_price": 5})
    assert cf["where"] == "p.panel_technology = %s AND COALESCE(p.sale_price, p.price) <= %s"
    assert cf["params"] == ["OLED", 5]


def _by_id(cases):
    return {c.id: c for c in cases}


def test_corrected_ground_truth(cases):
    by = _by_id(cases)
    assert set(by["structured-budget-under-30k"].relevant_product_ids) == {
        "QE32Q5FAAUXPY", "UE32F6000FUXRU", "UE32H5000FUXRU", "UE43F6000FUXRU"}
    # all three 65" OLEDs carry VRR/FreeSync evidence; no S95H-only distinction
    assert set(by["hybrid-oled-65-ps5"].relevant_product_ids) == set(
        by["structured-oled-65"].relevant_product_ids)
    # effective-price rule admits the 75" QN80H (189990 effective, 229990 list)
    assert "QE75QN80HAUXPY" in by["hybrid-neo-qled-budget-gaming"].relevant_product_ids
    assert "QE75LS03HWUXPY" in by["structured-neo-qled-75-120hz"].relevant_product_ids
    assert by["difficult-cheapest-superlative"].family == "aggregate_not_retrieval"


def test_budget_filters_use_effective_price(cases):
    for c in cases:
        if "max_effective_price" in c.filters:
            assert "COALESCE(p.sale_price, p.price)" in compile_filters(c.filters)["where"]


def test_expected_sections_not_yet_populated(cases):
    assert all(not c.expected_sections for c in cases)
