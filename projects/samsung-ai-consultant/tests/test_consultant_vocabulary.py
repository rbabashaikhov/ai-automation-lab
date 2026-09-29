"""Catalog vocabulary, model-code normalization and family resolution (pure)."""

import pytest

from consultant.planning import compare_families
from consultant.schemas import ModelRef, ModelResolution, ProductKind, ProductRow, RefKind
from consultant.vocabulary import build_vocabulary, code_stem, nearest_codes, normalize_code

# (model_code, series, panel, category, resolution, size, year) -- series values as in production
ROWS = [
    ("QE55S95HAUXPY", "S95H", "OLED", "OLED", "3840x2160", 55, 2026),
    ("QE65S95HAUXPY", "S95H", "OLED", "OLED", "3840x2160", 65, 2026),
    ("QE77S95HAEXPY", "S95H", "OLED", "OLED", "3840x2160", 77, 2026),
    ("QE83S95HAEXPY", "S95H", "OLED", "OLED", "3840x2160", 83, 2026),
    ("QE42S90HAEXPY", "S90H", "OLED", "OLED", "3840x2160", 42, 2026),
    ("QE55S90HAUXPY", "S90H", "OLED", "OLED", "3840x2160", 55, 2026),
    ("QE65S90HAEXPY", "S90H", "OLED", "OLED", "3840x2160", 65, 2026),
    ("QE55LS03HAUXPY", "LS03HAUXPY", "QLED", "The Frame", "3840x2160", 55, 2026),
    ("QE43LS03HEUXPY", "LS03HEUXPY", "QLED", "The Frame", "3840x2160", 43, 2026),
    ("QE75LS03HWUXPY", "LS03HWUXPY", "Neo QLED", "The Frame", "3840x2160", 75, 2026),
    ("QE115QN90FUXRU", "QN90FAUXRU", "Neo QLED", "Neo QLED", "3840x2160", 115, 2026),
    ("MNA114MS1CCXRU", "MS1С", "Micro LED", "Micro LED", "4968x2808", 114, 2026),   # Cyrillic 'С'
]


@pytest.fixture(scope="module")
def vocab():
    return build_vocabulary(ROWS)


def test_normalization():
    assert normalize_code(" qe65s95hauxpy ") == "QE65S95HAUXPY"
    assert normalize_code("MS1С") == "MS1C"                  # Cyrillic lookalike -> Latin
    assert code_stem("QE65S95HAUXPY") == "S95HAUXPY"
    assert code_stem("MRE115MR95FXRU") == "MR95FXRU"


def test_exact_code(vocab):
    assert vocab.code_for("qe65s95hauxpy") == "QE65S95HAUXPY"
    assert vocab.code_for("QE55S90HAEXPY") is None


def test_family_by_series(vocab):
    assert vocab.family_codes("s95h") == (("QE55S95HAUXPY", "QE65S95HAUXPY", "QE77S95HAEXPY", "QE83S95HAEXPY"),
                                          "series")


def test_family_series_with_cyrillic_lookalike(vocab):
    assert vocab.family_codes("MS1C") == (("MNA114MS1CCXRU",), "series")


def test_family_model_code_stem_fallback(vocab):
    # series holds suffixes (LS03HAUXPY / LS03HEUXPY / LS03HWUXPY); the stem unifies the family
    codes, basis = vocab.family_codes("LS03H")
    assert basis == "model_code_stem" and codes == ("QE43LS03HEUXPY", "QE55LS03HAUXPY", "QE75LS03HWUXPY")
    assert vocab.family_codes("QN90F") == (("QE115QN90FUXRU",), "model_code_stem")


def test_family_token_guard(vocab):
    assert vocab.family_codes("S9") == ((), "invalid_token")    # would match S90H and S95H
    assert vocab.family_codes("PS5") == ((), "model_code_stem")


def test_nearest_codes(vocab):
    assert nearest_codes("QE55S90HAEXPY", vocab.model_codes)[:2] == ("QE55S90HAUXPY", "QE65S90HAEXPY")
    assert nearest_codes("ZZZ", vocab.model_codes) == ()


def test_vocabulary_is_closed_and_has_no_live_facts(vocab):
    assert vocab.canonical("panel_technology", "oled") == "OLED"
    assert vocab.canonical("panel_technology", "QD-OLED") is None
    assert not hasattr(vocab, "prices") and not hasattr(vocab, "availability")


def _row(i, code, size):
    return ProductRow(i, "galaxystore", str(i), code, code, "OLED", None, ProductKind.TV, 2026, size,
                      "3840x2160", "OLED", 120, 100.0, None, 100.0, "RUB", True, None)


def _family(token, sizes, start):
    rows = tuple(_row(start + n, f"{token}-{s}", float(s)) for n, s in enumerate(sizes))
    return ModelResolution(ModelRef(token, RefKind.FAMILY), "family", rows, "series",
                           tuple(sorted(float(s) for s in sizes)))


def test_family_comparison_ambiguous_multiple_shared_sizes():
    fc = compare_families([_family("S95H", [55, 65, 77, 83], 1), _family("S90H", [42, 48, 55, 65, 77, 83], 10)], None)
    assert fc.status == "ambiguous_multiple_shared_sizes"
    assert fc.shared_sizes == (55.0, 65.0, 77.0, 83.0) and fc.selected_size is None and fc.products == ()


def test_family_comparison_user_size():
    fc = compare_families([_family("S95H", [55, 65], 1), _family("S90H", [55, 65], 10)], 65)
    assert fc.status == "resolved_user_size" and fc.selected_size == 65
    assert [p.model_code for p in fc.products] == ["S95H-65", "S90H-65"]


def test_family_comparison_single_shared_size():
    fc = compare_families([_family("A1", [55, 65], 1), _family("B1", [65, 75], 10)], None)
    assert fc.status == "resolved_single_shared_size" and fc.selected_size == 65


def test_family_comparison_size_not_shared_and_none_shared():
    assert compare_families([_family("A1", [55], 1), _family("B1", [65], 10)], 55).status == "size_not_shared"
    assert compare_families([_family("A1", [55], 1), _family("B1", [65], 10)], None).status == "no_shared_size"


def test_family_common_facts_only_when_uniform():
    fc = compare_families([_family("A1", [55, 65], 1), _family("B1", [55, 65], 10)], None)
    assert fc.common_facts["A1"]["panel_technology"] == "OLED"
