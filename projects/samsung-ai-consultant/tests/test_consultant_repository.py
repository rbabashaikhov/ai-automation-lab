"""CatalogRepository against a disposable PostgreSQL+pgvector container with the real
migrations (tests/run_db_tests.sh), seeded with the curated production subset."""

import psycopg2
import pytest

from consultant.catalog_repository import (
    MAX_CANDIDATES, MAX_LIST_LIMIT, CatalogRepository, compile_filters, open_readonly_connection,
)
from consultant.schemas import (
    Filters, GroupKey, ModelRef, ProductKind, Range, RefKind, ResolutionClass, SortDir, SortKey,
)

from .consultant_fixtures import load_raw, seed
from .db_fixtures import TEST_DATABASE_URL, db_conn, requires_db  # noqa: F401

pytestmark = requires_db


@pytest.fixture
def repo(db_conn):  # noqa: F811
    seed(db_conn)
    return CatalogRepository(db_conn)


def codes(rows):
    return sorted(p.model_code for p in rows)


def test_compile_filters_is_parameterized_and_typed():
    where, params = compile_filters(Filters(panel_technology=("OLED",), effective_price=Range(None, 1.0)))
    assert "%s" in where and "OLED" not in where and params == [["OLED"], 1.0]
    with pytest.raises(TypeError):
        compile_filters({"panel_technology": "OLED"})


def test_effective_price_is_canonical(repo):
    raw = {p["model_code"]: p for p in load_raw()}
    for row in repo.search(Filters(), limit=MAX_LIST_LIMIT).rows:
        assert row.effective_price == raw[row.model_code]["effective_price"]
    qn80 = repo.get_products_by_codes(["QE75QN80HAUXPY"])[0]
    assert (qn80.price, qn80.sale_price, qn80.effective_price) == (229990.0, 189990.0, 189990.0)


def test_effective_vs_list_price_filters(repo):
    eff = codes(repo.search(Filters(panel_technology=("Neo QLED",), effective_price=Range(None, 200000))).rows)
    lst = codes(repo.search(Filters(panel_technology=("Neo QLED",), list_price=Range(None, 200000))).rows)
    assert "QE75QN80HAUXPY" in eff and "QE75QN80HAUXPY" not in lst


def test_typed_filters_availability_resolution_and_kind(repo):
    assert "QE55QN1EHAUXPY" not in codes(repo.search(Filters(is_available=True), limit=50).rows)
    assert codes(repo.search(Filters(is_available=False)).rows) == ["QE55QN1EHAUXPY"]
    fhd = codes(repo.search(Filters(resolution_class=(ResolutionClass.FHD,))).rows)
    assert "QE32LS03CBUXRU" in fhd and "QE65S95HAUXPY" not in fhd
    no_displays = codes(repo.search(Filters(exclude_product_kinds=(ProductKind.DISPLAY,)), limit=50).rows)
    assert "MNA114MS1CCXRU" not in no_displays and "UE27LSM7FAXXPY" not in no_displays
    assert len(no_displays) == len(load_raw()) - 2


def test_screen_size_range_and_exact(repo):
    assert codes(repo.search(Filters(panel_technology=("OLED",), screen_size_inches=Range(65, 65))).rows) == [
        "QE65S85HAEXPY", "QE65S90HAEXPY", "QE65S95HAUXPY"]
    big = repo.search(Filters(screen_size_inches=Range(100, None))).rows
    assert all(p.screen_size_inches >= 100 for p in big) and big


def test_sorting_and_stable_order(repo):
    rows = repo.search(Filters(panel_technology=("OLED",)), (SortKey.EFFECTIVE_PRICE, SortDir.DESC), 50).rows
    prices = [p.effective_price for p in rows]
    assert prices == sorted(prices, reverse=True)
    by_size = repo.search(Filters(), (SortKey.SCREEN_SIZE, SortDir.ASC), 3).rows
    assert by_size[0].model_code == "UE27LSM7FAXXPY"


def test_hard_limits(repo):
    r = repo.search(Filters(), limit=10_000)
    assert r.limit == MAX_LIST_LIMIT and r.total_count == len(load_raw())
    small = repo.search(Filters(), limit=2)
    assert len(small.rows) == 2 and small.truncated and small.total_count == len(load_raw())
    assert repo.candidates(Filters()).limit == MAX_CANDIDATES


def test_unknown_identifiers_rejected(repo):
    with pytest.raises(TypeError):
        repo.search(Filters(), ("name", "asc"))
    with pytest.raises(TypeError):
        repo.extreme(Filters(), "price", SortDir.ASC)
    with pytest.raises(TypeError):
        repo.count(Filters(), "model_code")


def test_hostile_values_are_only_data(repo):
    assert repo.search(Filters(panel_technology=("OLED'; DROP TABLE products; --",))).rows == []
    assert repo.count(Filters())[0][1] == len(load_raw())


def test_tie_aware_extremes(repo):
    largest_oled = repo.extreme(Filters(panel_technology=("OLED",)), SortKey.SCREEN_SIZE, SortDir.DESC)
    assert codes(largest_oled) == ["QE83S85HAEXPY", "QE83S90HAEXPY", "QE83S95HAEXPY"]   # three-way tie
    cheapest = repo.extreme(Filters(), SortKey.EFFECTIVE_PRICE, SortDir.ASC)
    assert codes(cheapest) == ["UE32H5000FUXRU"]
    most_expensive = repo.extreme(Filters(), SortKey.EFFECTIVE_PRICE, SortDir.DESC)
    assert codes(most_expensive) == ["MNA114MS1CCXRU"]         # factual extremes see the whole catalog
    fastest = repo.extreme(Filters(), SortKey.REFRESH_RATE, SortDir.DESC)
    assert len(fastest) > 1 and {p.refresh_rate_hz for p in fastest} == {120}
    assert repo.extreme(Filters(panel_technology=("OLED",), effective_price=Range(None, 1000)),
                        SortKey.EFFECTIVE_PRICE, SortDir.ASC) == []


def test_count_and_grouping(repo):
    assert repo.count(Filters())[0] == (None, len(load_raw()))
    kinds = dict(repo.count(Filters(), GroupKey.PRODUCT_KIND))
    assert kinds == {"display": 2, "tv": len(load_raw()) - 2}
    sizes = dict(repo.count(Filters(panel_technology=("OLED",)), GroupKey.SCREEN_SIZE))
    assert sizes[83.0] == 3 and sizes[65.0] == 3


def test_exact_lookup_and_not_found(repo):
    exact, missing = repo.resolve_model_refs([ModelRef("qe65s95hauxpy", RefKind.FULL),
                                              ModelRef("QE55S90HAEXPY", RefKind.FULL)])
    assert exact.status == "exact" and exact.products[0].identity == ("galaxystore", exact.products[0].external_id)
    assert missing.status == "not_found" and missing.products == () and missing.suggestions


def test_family_lookup(repo):
    (fam,) = repo.resolve_model_refs([ModelRef("S95H", RefKind.FAMILY)])
    assert fam.status == "family" and fam.basis == "series"
    assert codes(fam.products) == ["QE55S95HAUXPY", "QE65S95HAUXPY", "QE77S95HAEXPY", "QE83S95HAEXPY"]
    assert fam.sizes == (55.0, 65.0, 77.0, 83.0)


def test_spec_lookup(repo):
    pid = repo.get_products_by_codes(["MNA114MS1CCXRU"])[0].id
    specs = repo.get_specs([pid], ["Версия HDMI"])[pid]
    assert [(s.spec_name, s.spec_value) for s in specs] == [("Версия HDMI", "2.1")]
    assert repo.get_specs([]) == {}


def test_vocabulary_and_live_price_range(repo):
    v = repo.vocabulary()
    assert "QE65S95HAUXPY" in v.model_codes and "OLED" in v.panel_technologies and "The Frame" in v.categories
    lo, hi = repo.price_range(Filters(panel_technology=("OLED",)))
    assert lo <= hi


def test_readonly_connection_refuses_writes(repo):
    conn = open_readonly_connection(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()[0] == "on"
            with pytest.raises(psycopg2.errors.ReadOnlySqlTransaction):
                cur.execute("UPDATE products SET price = 1")
    finally:
        conn.rollback()
        conn.close()
    assert repo.get_products_by_codes(["QE65S95HAUXPY"])[0].price != 1
