"""Deterministic extraction (no LLM). Needs like 'для PS5' are intentionally out of scope."""

import pytest

from consultant.extract import extract
from consultant.schemas import ResolutionClass, Sort, SortDir, SortKey
from consultant.vocabulary import build_vocabulary

from .consultant_fixtures import load_raw


@pytest.fixture(scope="module")
def vocab():
    return build_vocabulary([(p["model_code"], p["series"], p["panel_technology"], p["category"],
                              p["resolution"], p["screen_size_inches"], p["year"]) for p in load_raw()])


def test_full_model_code(vocab):
    e = extract("Есть ли у QE65S95HAUXPY HDMI 2.1?", vocab)
    assert e.model_codes == ("QE65S95HAUXPY",) and e.unknown_model_codes == ()


def test_lowercase_code_and_brand(vocab):
    assert extract("samsung qe65s95hauxpy", vocab).model_codes == ("QE65S95HAUXPY",)


def test_unknown_catalog_shaped_code_is_reported_not_dropped(vocab):
    # QE55S90HAEXPY is the non-existent code the Phase 3D.4 LLM answer invented.
    e = extract("сравни QE65S95HAUXPY и QE55S90HAEXPY", vocab)
    assert e.model_codes == ("QE65S95HAUXPY",) and e.unknown_model_codes == ("QE55S90HAEXPY",)


def test_family_tokens(vocab):
    assert extract("Сравни S95H и S90H", vocab).family_tokens == ("S95H", "S90H")
    assert extract("для PS5", vocab).family_tokens == ()


@pytest.mark.parametrize("text", ['65"', "65 дюймов", "65 дюймовый", "65-дюймовый", "65 inch"])
def test_screen_sizes(text, vocab):
    assert extract(text, vocab).screen_sizes == (65.0,)


@pytest.mark.parametrize("text,value,bound", [
    ("200000", 200000, "unspecified"),
    ("200 000 рублей", 200000, "unspecified"),
    ("200 тысяч", 200000, "unspecified"),
    ("200к", 200000, "unspecified"),
    ("2 млн", 2000000, "unspecified"),
    ("1,5 млн", 1500000, "unspecified"),
    ("до 200 тысяч", 200000, "max"),
    ("не дороже 150000 ₽", 150000, "max"),
    ("от 100 тыс", 100000, "min"),
])
def test_prices(text, value, bound, vocab):
    (p,) = extract(text, vocab).prices
    assert (p.value, p.bound) == (value, bound)


def test_size_and_price_do_not_collide(vocab):
    e = extract("Нужен телевизор 65 дюймов до 200 тысяч.", vocab)
    assert e.screen_sizes == (65.0,) and [(p.value, p.bound) for p in e.prices] == [(200000.0, "max")]


def test_small_bare_numbers_are_not_prices(vocab):
    assert extract("сравни первые 2", vocab).prices == ()


@pytest.mark.parametrize("text,hz", [("120 Гц", 120), ("144 Hz", 144), ("100 герц", 100)])
def test_refresh_rate(text, hz, vocab):
    assert extract(text, vocab).refresh_rates == (hz,)


def test_keywords(vocab):
    e = extract("Neo QLED или OLED, The Frame, 4K", vocab)
    assert e.panel_technologies == ("Neo QLED", "OLED")     # 'neo qled' does not also yield 'QLED'
    assert e.categories == ("The Frame",)
    assert e.resolution_classes == (ResolutionClass.UHD_4K,)
    assert e.prices == ()                                    # '4K' is a resolution, not 4 000 RUB


@pytest.mark.parametrize("text,sort", [
    ("самый дешевый OLED", Sort(SortKey.EFFECTIVE_PRICE, SortDir.ASC)),
    ("самый дешёвый телевизор", Sort(SortKey.EFFECTIVE_PRICE, SortDir.ASC)),
    ("самый дорогой", Sort(SortKey.EFFECTIVE_PRICE, SortDir.DESC)),
    ("самый большой OLED телевизор", Sort(SortKey.SCREEN_SIZE, SortDir.DESC)),
    ("самый маленький", Sort(SortKey.SCREEN_SIZE, SortDir.ASC)),
    ("самая высокая частота", Sort(SortKey.REFRESH_RATE, SortDir.DESC)),
    ("самый дешевый без скидки", Sort(SortKey.LIST_PRICE, SortDir.ASC)),
])
def test_superlatives(text, sort, vocab):
    assert extract(text, vocab).superlative == sort


def test_list_price_and_availability_wording(vocab):
    assert extract("по полной цене", vocab).price_basis == "list"
    assert extract("какие модели сейчас нет в наличии", vocab).is_available is False
    assert extract("что есть в наличии", vocab).is_available is True


def test_needs_are_not_interpreted(vocab):
    e = extract("хороший для PS5, для фильмов, для светлой комнаты", vocab)
    assert e.superlative is None and e.panel_technologies == () and e.prices == ()
