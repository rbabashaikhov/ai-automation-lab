"""Deterministic relaxation probes for zero-result structured requests (Phase 4C).

For each hard constraint the user set, drop exactly that one constraint (everything else --
including the availability default and recommendation scope -- stays), count what would match,
and fetch up to ``per_probe`` nearest alternatives by SQL only:

* exact range (min == max, e.g. 65")      -> nearest by |value - target|;
* upper bound dropped (e.g. price <= 30k) -> ascending on that key (closest from above);
* lower bound dropped                     -> descending on that key (closest from below);
* set / flag constraints                  -> default order (effective price ascending).

Every alternative records the dropped constraint and its actual value for it. Vector search is
never used to invent "close enough" alternatives. Bounded: at most one probe per user constraint.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Optional

from .catalog_repository import CatalogRepository
from .schemas import Filters, Range, ResolvedPlan, SortDir, SortKey

PER_PROBE = 3
_RANGE_KEYS = {"screen_size_inches": SortKey.SCREEN_SIZE, "effective_price": SortKey.EFFECTIVE_PRICE,
               "list_price": SortKey.LIST_PRICE, "refresh_rate_hz": SortKey.REFRESH_RATE}
_ROW_VALUE = {"screen_size_inches": "screen_size_inches", "effective_price": "effective_price",
              "list_price": "price", "refresh_rate_hz": "refresh_rate_hz", "panel_technology": "panel_technology",
              "category": "category", "resolution_class": "resolution", "is_available": "is_available"}


@dataclass(frozen=True)
class RelaxationProbe:
    dropped: str                 # the one hard constraint removed
    requested: str               # human-readable original constraint
    matches: int                 # products matching every other constraint
    alternatives: tuple          # ProductRow (<= PER_PROBE)
    actual_values: tuple         # value of the dropped attribute for each alternative


def describe(key: str, filters: Filters) -> str:
    v = getattr(filters, key)
    if isinstance(v, Range):
        if v.min is not None and v.min == v.max:
            return f"{key} = {v.min:g}"
        parts = ([f">= {v.min:g}"] if v.min is not None else []) + ([f"<= {v.max:g}"] if v.max is not None else [])
        return f"{key} {' and '.join(parts)}"
    if isinstance(v, tuple):
        return f"{key} in {[getattr(x, 'value', x) for x in v]}"
    return f"{key} = {v}"


def _alternatives(repo: CatalogRepository, key: str, original: Range, relaxed: Filters, n: int) -> list:
    sk = _RANGE_KEYS[key]
    if original.min is not None and original.min == original.max:
        return repo.nearest(relaxed, sk, original.min, n)
    direction = SortDir.ASC if original.max is not None else SortDir.DESC
    return repo.search(relaxed, (sk, direction), n).rows


def relax(plan: ResolvedPlan, repo: CatalogRepository, per_probe: int = PER_PROBE,
          keys: Optional[tuple] = None) -> tuple:
    empty = Filters()
    probes = []
    for key in keys if keys is not None else plan.user_constraint_keys:
        relaxed = replace(plan.filters, **{key: getattr(empty, key)})
        matches = repo.count(relaxed)[0][1]
        if not matches:
            probes.append(RelaxationProbe(key, describe(key, plan.filters), 0, (), ()))
            continue
        original = getattr(plan.filters, key)
        rows = (_alternatives(repo, key, original, relaxed, per_probe) if key in _RANGE_KEYS
                else repo.search(relaxed, None, per_probe).rows)
        attr = _ROW_VALUE[key]
        probes.append(RelaxationProbe(key, describe(key, plan.filters), matches, tuple(rows),
                                      tuple(getattr(r, attr) for r in rows)))
    return tuple(probes)


def alternative_rows(probes: tuple) -> list:
    """De-duplicated ``(ProductRow, [dropped keys])`` across probes, first-seen order."""
    seen: dict = {}
    order: list = []
    for p in probes:
        for row in p.alternatives:
            if row.id not in seen:
                seen[row.id] = (row, [])
                order.append(row.id)
            seen[row.id][1].append(p.dropped)
    return [seen[i] for i in order]

