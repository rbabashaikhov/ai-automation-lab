from evaluation.fullcatalog_context import (
    NOT_LISTED, SPEC_FIELDS, build_message, malformed_dimensions, render_product, represented_once,
)

DEPTH = "Размер без подставки (ШxВxГ), см"


def _p(code="A", **kw):
    base = {"model_code": code, "name": f"TV {code}", "category": "OLED", "panel_technology": "OLED",
            "screen_size_inches": 65, "refresh_rate_hz": 120, "price": 100, "sale_price": None,
            "effective_price": 100, "is_available": True, "specs": {}}
    base.update(kw)
    return base


def test_absent_fields_are_explicit_not_listed_never_blank_or_guessed():
    txt = render_product(_p(refresh_rate_hz=None))
    assert "refresh_hz=not listed" in txt
    assert txt.count(NOT_LISTED) == 1 + len(SPEC_FIELDS)  # refresh + every whitelisted spec
    assert "anti_glare=not listed" in txt and "sound_power_W=not listed" in txt


def test_present_values_preserved_raw_and_sale_price_shown():
    p = _p(sale_price=90, effective_price=90, price=100,
           specs={DEPTH: "2229.8 x 1273.4 x 57.4", "Мощность звука, Вт": "70", "Антибликовое покрытие": "Да"})
    txt = render_product(p)
    assert "size_without_stand_WxHxD_cm=2229.8 x 1273.4 x 57.4" in txt  # raw, not corrected
    assert "sound_power_W=70" in txt and "anti_glare=Да" in txt
    assert "effective_price_rub=90 (list 100)" in txt


def test_represented_exactly_once_detects_missing_duplicate_extra():
    msg = "\n".join(render_product(_p(c)) for c in ("A", "B"))
    assert represented_once(msg, ["A", "B"])["ok"]
    assert represented_once(msg, ["A", "B", "C"])["missing"] == ["C"]
    assert represented_once(msg + "\n" + render_product(_p("A")), ["A", "B"])["duplicated"] == ["A"]
    assert represented_once(msg, ["A"])["extra"] == ["B"]


def test_malformed_dimensions_reported_not_fixed():
    ps = [_p("A", specs={DEPTH: "150.95 x 89.49 x 2.64"}), _p("B", specs={DEPTH: "70.8.8"}),
          _p("C", specs={DEPTH: "96.5\tx 56.3 x 7.5"}), _p("D", specs={DEPTH: "257.4 х 147.8 х 3.57"}), _p("E")]
    assert [m for m, _ in malformed_dimensions(ps)] == ["B", "C", "D"]
    assert dict(malformed_dimensions(ps))["C"] == "96.5\tx 56.3 x 7.5"


def test_chunks_are_supporting_only_and_do_not_add_products():
    prods = [_p("A"), _p("B")]
    cands = [{"model_code": "Z", "chunks": [{"section": "audio", "similarity": 0.4, "content": "chunk text"}]}]
    msg = build_message("q", prods, cands)
    assert represented_once(msg.split("Supporting retrieved chunks")[0], ["A", "B"])["ok"]
    assert "chunk text" in msg and "2 available products" in msg
