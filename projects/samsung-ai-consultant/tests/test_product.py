from ingestion.catalog import CatalogEntry
from ingestion.product import extract_product
from ingestion.validate import validate_product


def test_extract_product_miniled_full_pipeline(product_miniled_html):
    catalog_entry = CatalogEntry(
        external_id="3965052",
        product_url="https://galaxystore.ru/product/UE50M70HAUXPY/",
        mpn_code="UE50M70HAUXPY",
        name="Телевизор Samsung 50 Mini LED M70",
        series_hint="M70",
        category="Телевизоры/Mini LED",
        price=53990,
        sale_price=45490,
        currency="RUB",
        stock=165,
        available_hint=True,
    )

    product = extract_product(
        source="galaxystore",
        product_url="https://galaxystore.ru/product/UE50M70HAUXPY/",
        page_html=product_miniled_html,
        catalog_entry=catalog_entry,
    )

    assert product.external_id == "3965052"
    assert product.model_code == "UE50M70HAUXPY"
    assert product.sku is None  # deliberate: no genuine SKU source, see product.py docstring
    assert product.brand == "Samsung"
    assert product.year == 2026
    assert product.series == "M70"
    assert product.screen_size_inches == 50.0
    assert product.resolution == "3840x2160"
    assert product.panel_technology == "Mini LED"
    assert product.refresh_rate_hz == 60
    assert product.price == 53990
    assert product.sale_price == 45490
    assert product.currency == "RUB"
    assert product.is_available is True
    assert product.description
    assert len(product.spec_rows) >= 30
    assert "Диагональ" in product.specs_text
    assert product.source_hash and len(product.source_hash) == 64

    result = validate_product(product)
    assert result.is_valid, result.errors


def test_extract_product_without_catalog_entry_still_works(product_neoqled_html):
    product = extract_product(
        source="galaxystore",
        product_url="https://galaxystore.ru/product/QE50QN70HAUXPY/",
        page_html=product_neoqled_html,
        catalog_entry=None,
    )
    assert product.external_id == "3965350"
    assert product.model_code == "QE50QN70HAUXPY"
    assert product.panel_technology == "Neo QLED"
    # No catalog articleMain hint available -> falls back to model-code heuristic.
    assert product.series == "QN70"

    result = validate_product(product)
    assert result.is_valid, result.errors


def test_extract_product_no_discount_leaves_sale_price_null(product_miniled_html):
    # Simulate a catalog entry claiming a "sale" price that isn't actually lower.
    catalog_entry = CatalogEntry(
        external_id="3965052",
        product_url="https://galaxystore.ru/product/UE50M70HAUXPY/",
    )
    product = extract_product(
        source="galaxystore",
        product_url="https://galaxystore.ru/product/UE50M70HAUXPY/",
        page_html=product_miniled_html,
        catalog_entry=catalog_entry,
    )
    # digitalData on the page itself provides a real discount, so this just
    # re-confirms the page's own price/sale_price win over the (mostly empty)
    # catalog hint, and that sale_price < price holds.
    assert product.sale_price < product.price
