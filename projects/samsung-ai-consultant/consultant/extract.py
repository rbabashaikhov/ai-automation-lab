"""Deterministic extraction of structured hints from a user message (no LLM).

Scope (Phase 4B): full model codes, family tokens, screen sizes, prices, refresh rates, catalog
keywords (panel / category / resolution), superlatives, list-price and availability wording.
Needs such as "для PS5" or "для светлой комнаты" are *not* interpreted here (Phase 4D).

Matched spans are masked in order (codes -> sizes -> Hz -> prices -> keywords), so "65 дюймов
до 200 тысяч" yields one size (65) and one price (200000), never a 65-rouble budget.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .schemas import ResolutionClass, Sort, SortDir, SortKey
from .vocabulary import CatalogVocabulary, normalize_code


@dataclass(frozen=True)
class PriceMention:
    value: float
    bound: str          # "max" | "min" | "unspecified"
    text: str


@dataclass(frozen=True)
class Extraction:
    model_codes: tuple = ()           # catalog model codes (normalized)
    unknown_model_codes: tuple = ()   # catalog-shaped codes not in the catalog
    family_tokens: tuple = ()         # tokens that resolve to a catalog family
    screen_sizes: tuple = ()          # inches
    prices: tuple = ()                # PriceMention
    refresh_rates: tuple = ()         # Hz
    panel_technologies: tuple = ()
    categories: tuple = ()
    resolution_classes: tuple = ()    # ResolutionClass
    superlative: Optional[Sort] = None
    price_basis: str = "effective"    # "effective" | "list"
    is_available: Optional[bool] = None


_CODE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,3}\d{2,3}[A-Za-z][A-Za-z0-9]{4,11})(?![A-Za-z0-9])")
_FAMILY = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{1,3}\d{1,4}[A-Za-z]{0,2})(?![A-Za-z0-9])")
_SIZE = re.compile(r"(?<!\d)(\d{2,3}(?:[.,]\d)?)\s*(?:[\"”″]|''|-?\s*дюйм\w*|-?\s*inch\w*|in\b)", re.I)
_HZ = re.compile(r"(?<!\d)(\d{2,3})\s*(?:гц|hz|герц\w*)(?![a-zа-я])", re.I)
_PRICE = re.compile(
    r"(?<![\d.,])(?P<num>\d{1,3}(?:[  ]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(?P<mult>тысяч\w*|тыс\.?|млн\.?|миллион\w*|к(?![а-яa-z])|k(?![a-z]))?\s*"
    r"(?P<cur>руб\w*|р\.|₽|rub\b)?", re.I)
_RESOLUTION_TOKEN = re.compile(r"(?<![\d.,])[48]\s?[kк](?![a-zа-я0-9])", re.I)
_MAX_WORDS = re.compile(r"(?:\bдо|не\s+дороже|дешевле|в\s+пределах|максимум|не\s+более|меньше|under|up\s+to)\s*$")
_MIN_WORDS = re.compile(r"(?:\bот|дороже|не\s+дешевле|больше|минимум|не\s+менее|from|over)\s*$")

_CATEGORY_WORDS = (("the frame", "The Frame"), ("фрейм", "The Frame"), ("movingstyle", "The Movingstyle"),
                   ("мувингстайл", "The Movingstyle"), ("crystal uhd", "Crystal UHD"))
_PANEL_WORDS = (("neo qled", "Neo QLED"), ("нео qled", "Neo QLED"), ("micro rgb", "Micro RGB"),
                ("micro led", "Micro LED"), ("микро led", "Micro LED"), ("mini led", "Mini LED"),
                ("мини led", "Mini LED"), ("oled", "OLED"), ("олед", "OLED"), ("qled", "QLED"), ("led", "LED"))
_RESOLUTION_WORDS = (("ultra hd", ResolutionClass.UHD_4K), ("uhd", ResolutionClass.UHD_4K),
                     ("4k", ResolutionClass.UHD_4K), ("full hd", ResolutionClass.FHD),
                     ("fhd", ResolutionClass.FHD), ("1080p", ResolutionClass.FHD),
                     ("hd ready", ResolutionClass.HD))

_SUPERLATIVES = (
    (r"сам\w*\s+(?:дешев|недорог|бюджетн)|дешевейш|cheapest", SortKey.EFFECTIVE_PRICE, SortDir.ASC),
    (r"сам\w*\s+дорог|дорожайш|most\s+expensive", SortKey.EFFECTIVE_PRICE, SortDir.DESC),
    (r"сам\w*\s+(?:больш|крупн)|крупнейш|наибольш\w*\s+диагонал|largest|biggest", SortKey.SCREEN_SIZE, SortDir.DESC),
    (r"сам\w*\s+(?:маленьк|небольш|компактн)|наименьш\w*\s+диагонал|smallest", SortKey.SCREEN_SIZE, SortDir.ASC),
    (r"сам\w*\s+высок\w*\s+частот|максимальн\w*\s+частот|highest\s+refresh", SortKey.REFRESH_RATE, SortDir.DESC),
)
_LIST_PRICE = re.compile(r"без\s+скидки|полн\w*\s+цен|базов\w*\s+цен|исходн\w*\s+цен|list\s+price|full\s+price")
_UNAVAILABLE = re.compile(r"нет\s+в\s+наличии|не\s+в\s+наличии|отсутству|недоступн|out\s+of\s+stock")
_AVAILABLE = re.compile(r"в\s+наличии|in\s+stock")


def _mask(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _num(s: str) -> float:
    return float(s.replace(" ", "").replace(" ", "").replace(",", "."))


def _keyword(low: str, pairs) -> tuple:
    """Longest phrases first; each hit is masked so 'neo qled' never also yields 'qled'."""
    found = []
    for phrase, value in pairs:
        pattern = re.compile(r"(?<![a-zа-я0-9])" + re.escape(phrase) + r"(?![a-zа-я0-9])")
        m = pattern.search(low)
        while m:
            if value not in found:
                found.append(value)
            low = _mask(low, m.start(), m.end())
            m = pattern.search(low)
    return tuple(found), low


def extract(text: str, vocab: Optional[CatalogVocabulary] = None) -> Extraction:
    work = text
    codes, unknown, families = [], [], []
    for m in _CODE.finditer(text):
        code = normalize_code(m.group(1))
        if vocab is None or code in vocab.model_codes:
            codes.append(code)
        else:
            unknown.append(code)
        work = _mask(work, m.start(), m.end())
    if vocab is not None:
        for m in _FAMILY.finditer(work):
            tok = normalize_code(m.group(1))
            if tok not in families and vocab.is_family(tok):
                families.append(tok)
                work = _mask(work, m.start(), m.end())

    sizes = []
    for m in _SIZE.finditer(work):
        v = _num(m.group(1))
        if 10 <= v <= 130 and v not in sizes:
            sizes.append(v)
        work = _mask(work, m.start(), m.end())
    hz = []
    for m in _HZ.finditer(work):
        v = int(m.group(1))
        if v not in hz:
            hz.append(v)
        work = _mask(work, m.start(), m.end())

    # '4K' / '8K' are resolutions, not 4 000 roubles: protect them before price parsing.
    resolution_tokens = []
    for m in _RESOLUTION_TOKEN.finditer(work):
        resolution_tokens.append(m.group(0))
        work = _mask(work, m.start(), m.end())

    prices = []
    low_all = work.lower().replace("ё", "е")
    for m in _PRICE.finditer(work):
        mult, cur = (m.group("mult") or "").lower(), m.group("cur")
        value = _num(m.group("num"))
        if mult.startswith(("тыс", "к", "k")):
            value *= 1_000
        elif mult.startswith(("млн", "миллион")):
            value *= 1_000_000
        if not (mult or cur or value >= 1000):
            continue
        before = low_all[max(0, m.start() - 20):m.start()]
        bound = "max" if _MAX_WORDS.search(before) else "min" if _MIN_WORDS.search(before) else "unspecified"
        prices.append(PriceMention(value, bound, m.group(0).strip()))
        work = _mask(work, m.start(), m.end())

    low = work.lower().replace("ё", "е")
    categories, low = _keyword(low, _CATEGORY_WORDS)
    panels, low = _keyword(low, _PANEL_WORDS)
    resolutions, low = _keyword(low, _RESOLUTION_WORDS)
    if any(t.replace(" ", "")[0] == "4" for t in resolution_tokens) and ResolutionClass.UHD_4K not in resolutions:
        resolutions = (*resolutions, ResolutionClass.UHD_4K)
    if vocab is not None:
        panels = tuple(p for p in panels if p in vocab.panel_technologies)
        categories = tuple(c for c in categories if c in vocab.categories)

    price_basis = "list" if _LIST_PRICE.search(low) else "effective"
    superlative = None
    for pattern, key, direction in _SUPERLATIVES:
        if re.search(pattern, low):
            if key is SortKey.EFFECTIVE_PRICE and price_basis == "list":
                key = SortKey.LIST_PRICE
            superlative = Sort(key, direction)
            break
    availability = False if _UNAVAILABLE.search(low) else True if _AVAILABLE.search(low) else None

    return Extraction(tuple(codes), tuple(unknown), tuple(families), tuple(sizes), tuple(prices), tuple(hz),
                      panels, categories, resolutions, superlative, price_basis, availability)
