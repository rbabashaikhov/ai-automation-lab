from ingestion.extractors.digital_data import (
    extract_digital_data_listing,
    extract_digital_data_product,
)


def test_listing_extracts_items_and_pagination(catalog_page1_html):
    listing = extract_digital_data_listing(catalog_page1_html)

    assert listing.result_count == 75
    assert listing.pages_count == 3
    assert listing.current_page == 1
    assert len(listing.items) > 0

    first = listing.items[0]
    assert first.external_id == "3965052"
    assert first.mpn_code == "UE50M70HAUXPY"
    assert first.product_url == "https://galaxystore.ru/product/UE50M70HAUXPY/"
    assert first.price == 53990
    assert first.sale_price == 45490
    assert "&quot;" not in (first.name or "")


def test_listing_internal_sku_code_equals_id_not_a_real_sku(catalog_page1_html):
    """Verified discovery: digitalData skuCode == id, confirming it is not a
    real manufacturer SKU (see extractors/digital_data.py module docstring)."""
    listing = extract_digital_data_listing(catalog_page1_html)
    for item in listing.items:
        assert item.internal_sku_code == item.external_id


def test_last_page_has_no_further_items_beyond_result_count(catalog_page3_html):
    listing = extract_digital_data_listing(catalog_page3_html)
    assert listing.current_page == 3
    assert listing.pages_count == 3
    assert len(listing.items) == 3


def test_product_extracts_core_fields(product_miniled_html):
    product = extract_digital_data_product(product_miniled_html)

    assert product.external_id == "3965052"
    assert product.mpn_code == "UE50M70HAUXPY"
    assert product.brand == "Samsung"
    assert product.currency == "RUB"
    assert product.price == 53990
    assert product.sale_price == 45490
    assert product.stock == 165
    assert product.category == "Телевизоры/Mini LED"


def test_product_missing_digital_data_returns_empty():
    product = extract_digital_data_product("<html><body>no digitalData here</body></html>")
    assert product.external_id is None
    assert product.mpn_code is None
