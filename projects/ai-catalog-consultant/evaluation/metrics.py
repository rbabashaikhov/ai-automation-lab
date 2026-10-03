"""Pure, deterministic retrieval metrics. No I/O, no randomness.

Products are identified by the dataset's ``relevant_product_ids`` values
(``model_code``). Ranks are 1-based. A ranked list may contain a product more
than once (chunk-level results); only its first occurrence counts.
"""

from __future__ import annotations

from typing import Optional, Sequence


def dedupe_ranked(ranked: Sequence[str]) -> list:
    seen, out = set(), []
    for p in ranked:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def first_relevant_rank(ranked: Sequence[str], relevant: Sequence[str]) -> Optional[int]:
    """Rank of the first acceptable product in the de-duplicated list, else None."""
    rel = set(relevant)
    for i, p in enumerate(dedupe_ranked(ranked), start=1):
        if p in rel:
            return i
    return None


def hit_at_k(ranked: Sequence[str], relevant: Sequence[str], k: int) -> bool:
    if k < 1:
        raise ValueError("k must be >= 1")
    rank = first_relevant_rank(ranked, relevant)
    return rank is not None and rank <= k


def reciprocal_rank(ranked: Sequence[str], relevant: Sequence[str]) -> float:
    rank = first_relevant_rank(ranked, relevant)
    return 0.0 if rank is None else 1.0 / rank


def set_match(returned: Sequence[str], expected: Sequence[str]) -> dict:
    """Exact set comparison for SQL/aggregate cases (order-insensitive)."""
    got, want = set(returned), set(expected)
    hit = got & want
    return {
        "exact": got == want,
        "precision": len(hit) / len(got) if got else 0.0,
        "recall": len(hit) / len(want) if want else 0.0,
        "missing": sorted(want - got),
        "unexpected": sorted(got - want),
    }


def filter_correctness(
    returned: Sequence[str], admitted: Sequence[str], relevant: Sequence[str]
) -> dict:
    """Did the structured filter behave? ``admitted`` is the SQL-filtered candidate set.

    ``ok`` requires (a) every returned product to lie inside the admitted set
    and (b) every expected product to be admitted (the filter must not exclude
    a right answer).
    """
    adm = set(admitted)
    outside = sorted(set(returned) - adm)
    excluded = sorted(set(relevant) - adm)
    return {"ok": not outside and not excluded, "returned_outside_filter": outside,
            "expected_excluded_by_filter": excluded}


def chunk_metrics(chunks: Sequence, relevant: Sequence[str], sections: Sequence[str] = ()) -> dict:
    """Chunk-level diagnostics over an ordered list of objects with
    ``.model_code``, ``.section`` and ``.similarity``.

    * ``product_chunk_rank``: rank (among chunks) of the first chunk of an expected product.
    * ``section_chunk_rank``: same, but the chunk must also be in an expected section
      (None when no sections are expected).
    * ``best_relevant_similarity``: highest similarity among expected-product chunks.
    """
    rel, secs = set(relevant), set(sections)
    product_rank = section_rank = None
    best = None
    for i, c in enumerate(chunks, start=1):
        if c.model_code not in rel:
            continue
        if product_rank is None:
            product_rank = i
        if secs and section_rank is None and c.section in secs:
            section_rank = i
        if c.similarity is not None and (best is None or c.similarity > best):
            best = c.similarity
    return {
        "product_chunk_rank": product_rank,
        "section_chunk_rank": section_rank if secs else None,
        "best_relevant_similarity": best,
    }
