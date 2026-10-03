from ingestion.extractors.sm_params import extract_sm_params_catalog, extract_sm_params_product


def test_catalog_items_include_series_hint_and_prices(catalog_page1_html):
    items = extract_sm_params_catalog(catalog_page1_html)
    assert len(items) > 0

    by_id = {item.external_id: item for item in items}
    first = by_id["3965052"]
    assert first.series_hint == "M70"
    assert first.detail_url == "/product/UE50M70HAUXPY/"
    assert first.price_special == 45490
    assert first.price_recommended == 53990
    assert first.can_buy is True


def test_product_specs_grouped_extraction(product_miniled_html):
    product = extract_sm_params_product(product_miniled_html)

    assert product.serial_code == "UE50M70HAUXPY"
    assert len(product.specs) >= 30  # 15 groups, ~48 items observed during discovery

    by_name = {s.spec_name: s for s in product.specs}
    assert by_name["Диагональ, дюйм"].values == ["50"]
    assert by_name["Частота обновления, Гц"].values == ["60"]
    assert by_name["Год выпуска"].values == ["2026"]
    assert by_name["Технология экрана"].values == ["Mini LED"]
    assert "4K" in by_name["Разрешение"].values[0]


def test_neo_qled_product_has_different_panel_technology(product_neoqled_html):
    product = extract_sm_params_product(product_neoqled_html)
    by_name = {s.spec_name: s for s in product.specs}
    assert by_name["Технология экрана"].values == ["Neo QLED"]


def test_missing_sm_params_returns_empty():
    assert extract_sm_params_catalog("<html></html>") == []
    product = extract_sm_params_product("<html></html>")
    assert product.serial_code is None
    assert product.specs == []
