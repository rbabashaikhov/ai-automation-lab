from ingestion import normalize
from ingestion.extractors.sm_params import SpecItem, extract_sm_params_product


def _specs(product_html: str) -> list[SpecItem]:
    return extract_sm_params_product(product_html).specs


def test_normalize_from_real_miniled_product(product_miniled_html):
    specs = _specs(product_miniled_html)
    assert normalize.normalize_screen_size_inches(specs) == 50.0
    assert normalize.normalize_resolution(specs) == "3840x2160"
    assert normalize.normalize_panel_technology(specs) == "Mini LED"
    assert normalize.normalize_refresh_rate_hz(specs) == 60
    assert normalize.normalize_year(specs) == 2026


def test_normalize_from_real_neoqled_product(product_neoqled_html):
    specs = _specs(product_neoqled_html)
    assert normalize.normalize_panel_technology(specs) == "Neo QLED"
    assert normalize.normalize_screen_size_inches(specs) == 50.0


def test_normalize_screen_size_handles_comma_decimal():
    specs = [SpecItem(spec_group="g", spec_name="Диагональ, дюйм", values=["55,5"])]
    assert normalize.normalize_screen_size_inches(specs) == 55.5


def test_normalize_resolution_explicit_dimensions():
    specs = [SpecItem(spec_group="g", spec_name="Разрешение", values=["8K (7680×4320)"])]
    assert normalize.normalize_resolution(specs) == "7680x4320"


def test_normalize_resolution_falls_back_to_known_label():
    specs = [SpecItem(spec_group="g", spec_name="Разрешение", values=["Full HD"])]
    assert normalize.normalize_resolution(specs) == "1920x1080"


def test_normalize_resolution_missing_spec_returns_none():
    assert normalize.normalize_resolution([]) is None


def test_normalize_series_prefers_catalog_hint():
    assert normalize.normalize_series("M70", "UE50M70HAUXPY") == "M70"


def test_normalize_series_falls_back_to_model_code_pattern():
    assert normalize.normalize_series(None, "QE50QN70HAUXPY") == "QN70"


def test_normalize_series_returns_none_when_unavailable():
    assert normalize.normalize_series(None, None) is None


def test_normalize_model_code_precedence():
    assert normalize.normalize_model_code(None, "ue50m70hauxpy", "other") == "UE50M70HAUXPY"
    assert normalize.normalize_model_code(None, None, None) is None


def test_normalize_price_rejects_negative():
    assert normalize.normalize_price(-10) is None
    assert normalize.normalize_price(None) is None
    assert normalize.normalize_price(45490.456) == 45490.46


def test_normalize_availability_prefers_json_ld_signal():
    assert (
        normalize.normalize_availability(
            json_ld_available=False, sm_can_buy=True, sm_disabled=False, stock=100
        )
        is False
    )


def test_normalize_availability_falls_back_to_sm_params():
    assert (
        normalize.normalize_availability(
            json_ld_available=None, sm_can_buy=True, sm_disabled=True, stock=100
        )
        is False
    )


def test_normalize_availability_falls_back_to_stock():
    assert (
        normalize.normalize_availability(
            json_ld_available=None, sm_can_buy=None, sm_disabled=None, stock=0
        )
        is False
    )
    assert (
        normalize.normalize_availability(
            json_ld_available=None, sm_can_buy=None, sm_disabled=None, stock=5
        )
        is True
    )


def test_normalize_availability_defaults_true_when_no_signal():
    assert (
        normalize.normalize_availability(
            json_ld_available=None, sm_can_buy=None, sm_disabled=None, stock=None
        )
        is True
    )


def test_build_spec_rows_deduplicates_keys_deterministically():
    specs = [
        SpecItem(spec_group="A", spec_name="Вес, кг", values=["1"], sort_order=0),
        SpecItem(spec_group="B", spec_name="Вес, кг", values=["2"], sort_order=1),
    ]
    rows = normalize.build_spec_rows(specs)
    keys = [r.spec_key for r in rows]
    assert len(keys) == len(set(keys))
    assert rows[0].unit == "kg"


def test_compute_source_hash_is_deterministic_and_sensitive_to_content():
    kwargs = dict(
        name="TV A",
        brand="Samsung",
        category="OLED",
        year=2026,
        series="S90",
        screen_size_inches=55.0,
        resolution="3840x2160",
        panel_technology="OLED",
        refresh_rate_hz=120,
        price=100000.0,
        sale_price=None,
        currency="RUB",
        is_available=True,
        description="desc",
        specs_text="text",
    )
    h1 = normalize.compute_source_hash(**kwargs)
    h2 = normalize.compute_source_hash(**kwargs)
    assert h1 == h2

    kwargs["price"] = 99999.0
    h3 = normalize.compute_source_hash(**kwargs)
    assert h3 != h1
