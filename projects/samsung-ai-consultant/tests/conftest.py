from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


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
