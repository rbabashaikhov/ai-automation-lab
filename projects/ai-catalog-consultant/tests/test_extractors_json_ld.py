from ingestion.extractors.json_ld import extract_json_ld


def test_extracts_product_and_breadcrumb(product_miniled_html):
    result = extract_json_ld(product_miniled_html)

    assert result.sku == "UE50M70HAUXPY"
    assert result.brand == "Samsung"
    assert result.currency == "RUB"
    assert result.price == 45490
    assert result.is_available is True
    assert result.url == "https://galaxystore.ru/product/UE50M70HAUXPY/"
    assert result.description
    assert len(result.breadcrumb) >= 1


def test_sku_matches_digital_data_mpn_code(product_miniled_html):
    """Cross-source consistency check confirmed during discovery: JSON-LD
    `sku` equals digitalData `mpnCode` on every fetched product."""
    from ingestion.extractors.digital_data import extract_digital_data_product

    ld = extract_json_ld(product_miniled_html)
    dd = extract_digital_data_product(product_miniled_html)
    assert ld.sku == dd.mpn_code


def test_missing_ld_json_returns_empty_result():
    result = extract_json_ld("<html><body>nothing here</body></html>")
    assert result.sku is None
    assert result.price is None
    assert result.breadcrumb == []
