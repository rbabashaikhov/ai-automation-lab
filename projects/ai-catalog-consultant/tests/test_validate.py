from ingestion.normalize import SpecRow
from ingestion.product import ExtractedProduct
from ingestion.validate import validate_product


def _valid_product(**overrides) -> ExtractedProduct:
    base = dict(
        source="galaxystore",
        external_id="123",
        product_url="https://galaxystore.ru/product/ABC/",
        name="Samsung TV 55",
        brand="Samsung",
        spec_rows=[
            SpecRow("g", "a", "a", "1", "1", None, 0),
            SpecRow("g", "b", "b", "2", "2", None, 1),
            SpecRow("g", "c", "c", "3", "3", None, 2),
        ],
    )
    base.update(overrides)
    return ExtractedProduct(**base)


def test_valid_product_passes():
    result = validate_product(_valid_product())
    assert result.is_valid
    assert result.errors == []


def test_missing_external_id_fails():
    result = validate_product(_valid_product(external_id=None))
    assert not result.is_valid
    assert any(e.code == "missing_external_id" for e in result.errors)


def test_missing_name_fails():
    result = validate_product(_valid_product(name=""))
    assert not result.is_valid
    assert any(e.code == "missing_name" for e in result.errors)


def test_missing_product_url_fails():
    result = validate_product(_valid_product(product_url=""))
    assert not result.is_valid
    assert any(e.code == "missing_product_url" for e in result.errors)


def test_unexpected_brand_fails():
    result = validate_product(_valid_product(brand="LG"))
    assert not result.is_valid
    assert any(e.code == "unexpected_brand" for e in result.errors)


def test_insufficient_specifications_fails():
    result = validate_product(_valid_product(spec_rows=[]))
    assert not result.is_valid
    assert any(e.code == "insufficient_specifications" for e in result.errors)


def test_substantial_specs_text_compensates_for_few_spec_rows():
    long_text = "Samsung TV with a long, meaningful specifications summary paragraph."
    result = validate_product(_valid_product(spec_rows=[], specs_text=long_text))
    assert result.is_valid
