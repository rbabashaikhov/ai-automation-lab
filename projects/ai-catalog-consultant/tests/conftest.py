import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _load_indexing_sample_products() -> dict:
    return json.loads((FIXTURES_DIR / "indexing_sample_products.json").read_text(encoding="utf-8"))


def _to_product(raw: dict):
    from indexing.models import Product

    return Product(
        id=raw["id"],
        source=raw["source"],
        external_id=raw["external_id"],
        model_code=raw["model_code"],
        name=raw["name"],
        brand=raw["brand"],
        category=raw["category"],
        product_url=raw["product_url"],
        year=raw["year"],
        series=raw["series"],
        screen_size_inches=raw["screen_size_inches"],
        resolution=raw["resolution"],
        panel_technology=raw["panel_technology"],
        refresh_rate_hz=raw["refresh_rate_hz"],
        price=raw["price"],
        sale_price=raw["sale_price"],
        currency=raw["currency"],
        is_available=raw["is_available"],
        description=raw["description"],
    )


def _to_specs(raw_specs: list[dict]):
    from indexing.models import Spec

    return [
        Spec(
            spec_group=s["spec_group"],
            spec_name=s["spec_name"],
            spec_key=s["spec_key"],
            spec_value=s["spec_value"],
            sort_order=s["sort_order"],
        )
        for s in raw_specs
    ]


@pytest.fixture
def oled_product_and_specs():
    """QE65S95HAUXPY -- real production OLED, 65"."""
    data = _load_indexing_sample_products()["QE65S95HAUXPY"]
    return _to_product(data["product"]), _to_specs(data["specs"])


@pytest.fixture
def neo_qled_product_and_specs():
    """QE85QN70HAUXPY -- real production Neo QLED, 85"."""
    data = _load_indexing_sample_products()["QE85QN70HAUXPY"]
    return _to_product(data["product"]), _to_specs(data["specs"])


@pytest.fixture
def unusual_display_product_and_specs():
    """UE27LSM7FAXXPY -- real production portable 'Дисплей' (The Movingstyle),
    not a conventional TV -- the "unusual/display product" sample."""
    data = _load_indexing_sample_products()["UE27LSM7FAXXPY"]
    return _to_product(data["product"]), _to_specs(data["specs"])


@pytest.fixture
def catalog_page1_html() -> str:
    return load_fixture("catalog_2026_page1.html")


@pytest.fixture
def catalog_page3_html() -> str:
    return load_fixture("catalog_2026_page3.html")


@pytest.fixture
def product_miniled_html() -> str:
    return load_fixture("product_UE50M70HAUXPY.html")


@pytest.fixture
def product_neoqled_html() -> str:
    return load_fixture("product_QE50QN70HAUXPY.html")
