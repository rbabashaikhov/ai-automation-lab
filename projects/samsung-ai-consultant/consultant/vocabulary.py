"""Closed catalog vocabularies and deterministic model / family resolution (pure; no DB).

Safe to cache: model codes, enum values, sizes, series/family index. Deliberately absent: prices
and availability (live facts, read per request).

Family resolution (verified by the Phase 4B inventory): ``products.series`` is 75/75 populated
but only partly a family code -- clean for S95H/S90H/S85H/QN80H/QN70H/M70/M80H/R85H/R95H/M1E/...,
while other rows hold a model-code suffix from the ingestion heuristic (``LS03HAUXPY``,
``U8000HUXPY``, ``QN90FAUXRU``) and ``MS1С`` contains a Cyrillic 'С'. Order:

1. ``series`` equals the token (after upper-casing and Cyrillic-lookalike normalization);
2. otherwise the model-code *stem* (the part after the letter prefix and size digits,
   ``QE65S95HAUXPY`` -> ``S95HAUXPY``) starts with the token. The token must be >= 3 chars and
   contain a letter and a digit, so ``S9`` never matches both S90H and S95H.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

# Cyrillic letters that look like Latin ones in model codes / series (observed: 'MS1С').
_LOOKALIKES = str.maketrans("АВСЕНКМОРТХУ", "ABCEHKMOPTXY")
_STEM = re.compile(r"^[A-Z]+\d+(.+)$")
FAMILY_TOKEN = re.compile(r"^(?=.*[A-Z])(?=.*\d)[A-Z0-9]{3,}$")


def normalize_code(text: str) -> str:
    return re.sub(r"\s+", "", str(text)).upper().translate(_LOOKALIKES)


def code_stem(model_code: str) -> Optional[str]:
    m = _STEM.match(normalize_code(model_code))
    return m.group(1) if m else None


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def nearest_codes(code: str, catalog_codes: Iterable[str], max_distance: int = 2, limit: int = 3) -> tuple:
    scored = sorted((levenshtein(code, c), c) for c in catalog_codes)
    return tuple(c for d, c in scored if d <= max_distance)[:limit]


@dataclass(frozen=True)
class CatalogVocabulary:
    model_codes: frozenset
    panel_technologies: frozenset
    categories: frozenset
    resolutions: frozenset
    screen_sizes: tuple
    years: frozenset
    series_by_code: dict          # model_code -> normalized series
    stem_by_code: dict            # model_code -> model-code stem

    def code_for(self, text: str) -> Optional[str]:
        code = normalize_code(text)
        return code if code in self.model_codes else None

    def family_codes(self, token: str) -> tuple:
        """``(sorted model codes, basis)``; basis is 'series', 'model_code_stem' or 'invalid_token'."""
        t = normalize_code(token)
        by_series = sorted(c for c, s in self.series_by_code.items() if s == t)
        if by_series:
            return tuple(by_series), "series"
        if not FAMILY_TOKEN.match(t):
            return (), "invalid_token"
        by_stem = sorted(c for c, s in self.stem_by_code.items() if s and s.startswith(t))
        return tuple(by_stem), "model_code_stem"

    def is_family(self, token: str) -> bool:
        return bool(self.family_codes(token)[0])

    def canonical(self, kind: str, value: str) -> Optional[str]:
        """Case-insensitive match of a panel technology / category to its catalog spelling."""
        pool = {"panel_technology": self.panel_technologies, "category": self.categories}[kind]
        want = value.strip().lower()
        for v in pool:
            if v.lower() == want:
                return v
        return None


def build_vocabulary(rows: Sequence[tuple]) -> CatalogVocabulary:
    """``rows``: (model_code, series, panel_technology, category, resolution, screen_size, year)."""
    codes, panels, cats, res, sizes, years = set(), set(), set(), set(), set(), set()
    series_by_code, stem_by_code = {}, {}
    for code, series, panel, cat, resolution, size, year in rows:
        if code:
            c = normalize_code(code)
            codes.add(c)
            series_by_code[c] = normalize_code(series) if series else None
            stem_by_code[c] = code_stem(c)
        for bucket, v in ((panels, panel), (cats, cat), (res, resolution), (years, year)):
            if v is not None:
                bucket.add(v)
        if size is not None:
            sizes.add(float(size))
    return CatalogVocabulary(frozenset(codes), frozenset(panels), frozenset(cats), frozenset(res),
                             tuple(sorted(sizes)), frozenset(years), series_by_code, stem_by_code)
