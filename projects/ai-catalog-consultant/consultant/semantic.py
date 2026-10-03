"""Phase 4C semantic helpers: cached query embeddings, fit-tie-only semantic tie-break,
and the deterministic lexical spec probe used for absence decisions.

There is deliberately **no network embedder** in this package: the only implementation reads
vectors cached by Phase 3D (``evaluation/results/query_embeddings.json``) or supplied by tests.
A missing vector raises :class:`EmbeddingUnavailable`; callers turn that into an explicit gap.

Grounding rule (Phase 4A amendment): a vector search that does not surface an attribute is
never evidence that the attribute is absent. Absence is decided only by :func:`lexical_probe`
over the product's actual ``product_specs`` rows.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping, Optional, Sequence

from .ranking import _fit
from .schemas import RankedCandidate, RankingResult, SpecRow

EMBEDDING_DIMS = 1536


class EmbeddingUnavailable(LookupError):
    """No cached/supplied vector for this text. Never triggers a network call."""


class CachedQueryEmbeddings:
    """Exact-text lookup of pre-computed query vectors. Counts lookups; performs no I/O after load."""

    def __init__(self, vectors: Mapping[str, Sequence[float]], source: str = "memory"):
        for text, v in vectors.items():
            if len(v) != EMBEDDING_DIMS:
                raise ValueError(f"vector for {text!r} has {len(v)} dims, expected {EMBEDDING_DIMS}")
        self._vectors = dict(vectors)
        self.source = source
        self.hits = 0
        self.misses = 0

    @classmethod
    def from_file(cls, path: Path) -> "CachedQueryEmbeddings":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")), source=str(path))

    def embed(self, text: str) -> list:
        v = self._vectors.get(text)
        if v is None:
            self.misses += 1
            raise EmbeddingUnavailable(text)
        self.hits += 1
        return list(v)


# ---- semantic tie-break (only inside equal structured fit) -----------------------------------

@dataclass(frozen=True)
class TiebreakReport:
    model_code: str
    structured_rank: int
    final_rank: int
    best_similarity: Optional[float]
    tie_group_size: int


def _reorder(items: Sequence[RankedCandidate], best: Mapping[int, float]) -> tuple:
    out, reports, i = [], [], 0
    while i < len(items):
        j = i
        while j < len(items) and _fit(items[j]) == _fit(items[i]):
            j += 1
        group = list(items[i:j])
        if len(group) > 1:
            # Scored products first by similarity (desc); unscored keep their structured order after.
            group.sort(key=lambda c: (best.get(c.product.id) is None, -(best.get(c.product.id) or 0.0), c.rank))
        for c in group:
            out.append(c)
            reports.append((c, j - i))
        i = j
    renumbered = tuple(replace(c, rank=n) for n, c in enumerate(out, start=1))
    rep = tuple(TiebreakReport(c.product.model_code, c.rank, n, best.get(c.product.id), size)
                for n, (c, size) in enumerate(reports, start=1))
    return renumbered, rep


def semantic_tiebreak(ranking: RankingResult, best_similarity: Mapping[int, float]) -> tuple:
    """Reorder candidates **only within groups of identical structured fit** (preferred count and
    numeric signal values -- the same key shortlist composition uses). Candidates of different fit
    never swap. Returns ``(new RankingResult, reports for qualified, reports for unknown)``."""
    qualified, rq = _reorder(ranking.qualified, best_similarity)
    unknown, ru = _reorder(ranking.unknown, best_similarity)
    return (replace(ranking, policy=f"{ranking.policy}+semantic-tiebreak-v1", qualified=qualified, unknown=unknown),
            rq, ru)


def best_similarity_by_product(chunks: Sequence) -> dict:
    best: dict = {}
    for c in chunks:
        if c.similarity is not None and (c.product_id not in best or c.similarity > best[c.product_id]):
            best[c.product_id] = c.similarity
    return best


# ---- deterministic lexical spec probe ----------------------------------------------------------

_TOKEN = re.compile(r"[0-9A-Za-zА-Яа-яЁё][0-9A-Za-zА-Яа-яЁё+.\-]+")
_STOP = frozenset({
    "есть", "ли", "у", "для", "это", "как", "что", "или", "при", "без", "под", "над", "поддержка",
    "поддержкой", "поддерживает", "функция", "функции", "функцией", "наличие", "телевизор", "телевизора",
    "модель", "модели", "samsung", "самсунг", "the", "and", "with", "support", "есть ли",
})
CYRILLIC_STEM = 6    # crude stem length for Cyrillic tokens (морфология): 'голосовой' -> 'голосо'


def probe_terms(text: str) -> tuple:
    """Lower-cased probe terms from a free-text need: Latin/digit tokens kept whole, Cyrillic tokens
    cut to a short stem. Deterministic; no NLU."""
    terms = []
    for tok in _TOKEN.findall(text.lower().replace("ё", "е")):
        tok = tok.strip(".-")
        min_len = 2 if re.search(r"\d", tok) else 3        # keep '6e', '2.1'; drop 'у', 'ли'
        if len(tok) < min_len or tok in _STOP:
            continue
        if re.search(r"[а-я]", tok) and len(tok) > CYRILLIC_STEM:
            tok = tok[:CYRILLIC_STEM]
        if tok not in terms:
            terms.append(tok)
    return tuple(terms)


@dataclass(frozen=True)
class ProbeResult:
    terms: tuple
    matched_specs: tuple         # SpecRow of this product mentioning any term
    matched_terms: tuple
    status: str                  # "mentioned_in_specs" | "verified_not_listed" | "absence_unverified" | "no_terms"
    catalog_coverage: dict       # term -> number of catalog products mentioning it
    all_terms_matched: bool = False   # a mention of 'hdmi' alone does not confirm 'hdmi 2.1'


def _spec_text(s: SpecRow) -> str:
    return f"{s.spec_name}: {s.spec_value or ''}".lower().replace("ё", "е")


def mentions(term: str, text: str) -> bool:
    """Boundary-aware match used for *both* per-product probing and catalog coverage, so the two
    can never disagree. The term must start at a word boundary ('2.1' does not match '22.1');
    Latin words must also end at one; digit terms may be followed by letters ('2.1' matches
    '2.1a'); Cyrillic stems may be followed by anything (morphology)."""
    t = term.lower()
    cyr_stem = bool(re.search(r"[а-я]", t))
    end = "" if cyr_stem else (r"(?![0-9])" if re.search(r"\d", t) else r"(?![0-9a-zа-я])")
    return re.search(r"(?<![0-9a-zа-я])" + re.escape(t) + end, text.lower().replace("ё", "е")) is not None


def catalog_coverage(term: str, candidate_rows: Sequence[SpecRow]) -> int:
    """Distinct products whose spec rows ``mentions`` the term (rows: ``repo.specs_mentioning``)."""
    return len({s.product_id for s in candidate_rows if mentions(term, _spec_text(s))})


def lexical_probe(terms: Sequence[str], specs: Sequence[SpecRow], catalog_coverage: Mapping[str, int]) -> ProbeResult:
    """Decide presence/absence of a long-tail attribute for one product from its spec rows.

    * a spec row mentions a term -> ``mentioned_in_specs`` (the rows become facts; the attribute
      claim itself is left to the reader of the evidence; ``all_terms_matched`` says whether every
      term was found);
    * no row mentions any term, and *every* term occurs elsewhere in the catalog (so the catalog's
      wording for the whole need is known) -> ``verified_not_listed``;
    * otherwise (some term never occurs in the catalog) -> ``absence_unverified``: the catalog may
      use other wording, so this is uncertainty, never a claim of absence.
    """
    if not terms:
        return ProbeResult((), (), (), "no_terms", {})
    matched, hit_terms = [], []
    for s in specs:
        text = _spec_text(s)
        found = [t for t in terms if mentions(t, text)]
        if found:
            matched.append(s)
            hit_terms.extend(t for t in found if t not in hit_terms)
    cov = {t: int(catalog_coverage.get(t, 0)) for t in terms}
    if matched:
        status = "mentioned_in_specs"
    elif all(cov.values()):
        status = "verified_not_listed"
    else:
        status = "absence_unverified"
    return ProbeResult(tuple(terms), tuple(matched), tuple(hit_terms), status, cov,
                       set(hit_terms) == set(terms))
