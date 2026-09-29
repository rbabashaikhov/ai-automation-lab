"""Deterministic candidate ranking and (separately) shortlist composition.

Ranking v1 decides *fit* only:

1. required features gate: all ``yes`` -> qualified; any ``no`` -> rejected; otherwise (some
   ``not_listed``) -> unknown, kept and ranked separately ("possibly suitable, not in catalog");
2. preferred features with state ``yes`` -- count, descending (no weights);
3. use-case numeric signals in profile order (missing values last);
4. effective price ascending (missing last);
5. canonical identity ``(source, external_id)`` -- stable final tie-break.

Shortlist composition decides *exposure* of an already-ranked list. It never reorders items and
never lets a lower-fit product displace a higher-fit one: price-tier / panel diversity only
chooses among products of the top fit band (identical preferred count *and* numeric signal
values), and only when that band is larger than the shortlist; otherwise the shortlist is the
top of the ranking.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .features import numeric_value
from .schemas import (
    FeatureState, NumericSignal, ProductRow, RankedCandidate, RankingResult, Shortlist, SortDir,
)

RANKING_POLICY = "ranking-v1"
MAX_SHORTLIST = 8

POLICY_TOP_RANKED = "top-ranked-v1"
POLICY_PRICE_TIERS = "price-tiers-v1"
POLICY_PANEL_DIVERSITY = "panel-diversity-v1"
# Diversity only applies when the top fit band has more candidates than the shortlist can show.
DIVERSITY_MIN_BAND = MAX_SHORTLIST + 1


def _numeric_key(value: Optional[float], direction: SortDir) -> tuple:
    if value is None:
        return (1, 0.0)
    return (0, value if direction is SortDir.ASC else -value)


def _price_key(p: ProductRow) -> tuple:
    return (1, 0.0) if p.effective_price is None else (0, p.effective_price)


def rank_candidates(products: Sequence[ProductRow], feature_results: dict, required: Sequence[str] = (),
                    preferred: Sequence[str] = (), numeric: Sequence[NumericSignal] = ()) -> RankingResult:
    """``feature_results``: ``{product_id: {feature_id: FeatureResult}}`` covering every
    required/preferred/numeric feature id (see ``features.evaluate_features``)."""
    qualified, unknown, rejected = [], [], []
    for p in products:
        res = feature_results.get(p.id, {})
        req = {f: res[f].state for f in required}
        pref_yes = sum(1 for f in preferred if res[f].state is FeatureState.YES)
        signals = tuple((s.source, numeric_value(s, p, res)) for s in numeric)
        key = ((-pref_yes,)
               + tuple(_numeric_key(v, s.direction) for (_, v), s in zip(signals, numeric))
               + (_price_key(p), p.identity))
        entry = (key, p, req, pref_yes, signals, res)
        states = set(req.values())
        if FeatureState.NO in states:
            rejected.append(p)
        elif FeatureState.NOT_LISTED in states:
            unknown.append(entry)
        else:
            qualified.append(entry)

    def build(entries):
        entries.sort(key=lambda e: e[0])
        return tuple(RankedCandidate(product=p, rank=i, required=req, preferred_matched=n,
                                     preferred_total=len(preferred), numeric_signals=sig, features=res)
                     for i, (_, p, req, n, sig, res) in enumerate(entries, start=1))

    return RankingResult(RANKING_POLICY, build(qualified), build(unknown),
                         tuple(sorted(rejected, key=lambda p: p.identity)))


def choose_policy(has_budget: bool, has_preferences: bool) -> str:
    """Versioned default: budgeted -> top ranked; no budget with preferences -> price tiers;
    constraint-only (no preferences) -> panel diversity."""
    if has_budget:
        return POLICY_TOP_RANKED
    return POLICY_PRICE_TIERS if has_preferences else POLICY_PANEL_DIVERSITY


def _fit(c: RankedCandidate) -> tuple:
    """Everything ranking treats as fit: preferred count and numeric signal values (not price)."""
    return (c.preferred_matched, tuple(v for _, v in c.numeric_signals))


def _top_band(items: Sequence[RankedCandidate]) -> list:
    """Candidates with exactly the best fit -- the only products diversity may choose among."""
    if not items:
        return []
    best = _fit(items[0])
    return [c for c in items if _fit(c) == best]


def _tercile_labels(band: Sequence[RankedCandidate]) -> dict:
    """Tier by effective-price rank inside the band (low / mid / high thirds). Deterministic."""
    priced = sorted((c for c in band if c.product.effective_price is not None),
                    key=lambda c: (c.product.effective_price, c.product.identity))
    n = len(priced)
    labels = {}
    for i, c in enumerate(priced):
        labels[c.product.id] = ("low", "mid", "high")[min(2, (3 * i) // n)]
    return labels


def _round_robin(band: Sequence[RankedCandidate], group_of, slots: int) -> list:
    """Pick up to ``slots`` from ``band`` cycling over groups; within a group, rank order."""
    groups: dict = {}
    order = []
    for c in band:
        g = group_of(c)
        if g not in groups:
            groups[g] = []
            order.append(g)
        groups[g].append(c)
    picked = []
    while len(picked) < slots and any(groups.values()):
        for g in order:
            if groups[g] and len(picked) < slots:
                picked.append(groups[g].pop(0))
    return picked


def compose_shortlist(ranking: RankingResult, policy: str = POLICY_TOP_RANKED,
                      max_items: int = MAX_SHORTLIST) -> Shortlist:
    items = list(ranking.qualified)
    if policy == POLICY_TOP_RANKED or len(_top_band(items)) < DIVERSITY_MIN_BAND:
        note = () if policy == POLICY_TOP_RANKED else (f"{policy} not applied: top fit band too small",)
        return Shortlist(policy if not note else POLICY_TOP_RANKED, tuple(items[:max_items]), notes=note)

    band = _top_band(items)
    if policy == POLICY_PRICE_TIERS:
        labels = _tercile_labels(band)
        order = {"low": 0, "mid": 1, "high": 2}
        # Visit tiers low -> mid -> high, each in rank order within the tier.
        by_tier = sorted(band, key=lambda c: (order.get(labels.get(c.product.id), 3), c.rank))
        picked = _round_robin(by_tier, lambda c: labels.get(c.product.id, "unpriced"), max_items)
        tiers = {c.product.model_code: labels.get(c.product.id, "unpriced") for c in picked}
    elif policy == POLICY_PANEL_DIVERSITY:
        picked = _round_robin(band, lambda c: c.product.panel_technology, max_items)
        tiers = {}
    else:
        raise ValueError(f"unknown shortlist policy {policy!r}")
    chosen = {c.product.id for c in picked}
    # Expose in ranking order; composition never reorders.
    return Shortlist(policy, tuple(c for c in items if c.product.id in chosen), tiers)
