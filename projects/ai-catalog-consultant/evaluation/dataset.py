"""Load, validate and select cases from ``retrieval_cases.json``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

DEFAULT_DATASET_PATH = Path(__file__).parent / "retrieval_cases.json"
EXPECTED_CASE_COUNT = 21

INTENTS = (
    "exact_lookup",
    "structured_selection",
    "semantic_feature_intent",
    "hybrid",
    "difficult_ambiguous",
)
MECHANISMS = ("sql_sufficient", "vector_primary", "sql_plus_vector", "aggregate_not_retrieval")
FAMILIES = (
    "sql_sufficient",
    "vector_primary",
    "hybrid",
    "difficult_ambiguous",
    "aggregate_not_retrieval",
)
SECTIONS = (
    "overview",
    "display",
    "gaming",
    "audio",
    "smart_features",
    "connectivity",
    "physical_design",
)

_REQUIRED = ("id", "query", "intent", "expected_mechanism", "filters", "relevant_product_ids", "notes")
_OPTIONAL = ("expected_sections",)


class DatasetError(ValueError):
    """The dataset file or one of its cases is structurally invalid."""


def derive_family(intent: str, mechanism: str) -> str:
    """Map a case's stored (intent, expected_mechanism) onto the five families.

    The file stores two orthogonal fields, not a family. Precedence:
    an aggregate mechanism always wins (a superlative must never be solved by
    vector search, even when it is also ambiguous); then ``difficult_ambiguous``
    intent; then the mechanism (``sql_plus_vector`` -> ``hybrid``).
    """
    if mechanism == "aggregate_not_retrieval":
        return "aggregate_not_retrieval"
    if intent == "difficult_ambiguous":
        return "difficult_ambiguous"
    if mechanism == "sql_plus_vector":
        return "hybrid"
    return mechanism


@dataclass(frozen=True)
class EvalCase:
    """One evaluation case.

    ``relevant_product_ids`` are ``model_code`` values (as stored in the
    dataset), not the canonical ``(source, external_id)`` identity; see
    ``evaluation/AUDIT.md``. Any single listed product is an acceptable answer
    for rank metrics; the whole list is the expected set for SQL/aggregate cases.
    """

    id: str
    query: str
    intent: str
    expected_mechanism: str
    filters: dict
    relevant_product_ids: tuple
    notes: str
    expected_sections: tuple = ()

    @property
    def family(self) -> str:
        return derive_family(self.intent, self.expected_mechanism)


def _parse_case(raw: Any, index: int) -> EvalCase:
    where = f"cases[{index}]"
    if not isinstance(raw, dict):
        raise DatasetError(f"{where}: case must be an object")
    cid = raw.get("id")
    if isinstance(cid, str) and cid:
        where = f"case {cid!r}"
    missing = [k for k in _REQUIRED if k not in raw]
    if missing:
        raise DatasetError(f"{where}: missing required field(s): {', '.join(missing)}")
    unknown = sorted(set(raw) - set(_REQUIRED) - set(_OPTIONAL))
    if unknown:
        raise DatasetError(f"{where}: unknown field(s): {', '.join(unknown)}")
    for key in ("id", "query", "notes"):
        if not isinstance(raw[key], str) or not raw[key].strip():
            raise DatasetError(f"{where}: {key!r} must be a non-empty string")
    if raw["intent"] not in INTENTS:
        raise DatasetError(f"{where}: invalid intent {raw['intent']!r}; expected one of {INTENTS}")
    if raw["expected_mechanism"] not in MECHANISMS:
        raise DatasetError(
            f"{where}: invalid expected_mechanism {raw['expected_mechanism']!r}; "
            f"expected one of {MECHANISMS}"
        )
    if not isinstance(raw["filters"], dict):
        raise DatasetError(f"{where}: 'filters' must be an object")
    rel = raw["relevant_product_ids"]
    if (
        not isinstance(rel, list)
        or not rel
        or not all(isinstance(p, str) and p.strip() for p in rel)
    ):
        raise DatasetError(f"{where}: 'relevant_product_ids' must be a non-empty list of strings")
    if len(set(rel)) != len(rel):
        raise DatasetError(f"{where}: duplicate entries in 'relevant_product_ids'")
    sections = raw.get("expected_sections", [])
    if not isinstance(sections, list) or any(s not in SECTIONS for s in sections):
        raise DatasetError(f"{where}: 'expected_sections' must be a list drawn from {SECTIONS}")
    return EvalCase(
        id=raw["id"],
        query=raw["query"],
        intent=raw["intent"],
        expected_mechanism=raw["expected_mechanism"],
        filters=dict(raw["filters"]),
        relevant_product_ids=tuple(rel),
        notes=raw["notes"],
        expected_sections=tuple(sections),
    )


def parse_dataset(data: Any, expected_count: Optional[int] = EXPECTED_CASE_COUNT) -> list:
    """Validate an already-parsed dataset document and return its cases in file order."""
    if not isinstance(data, dict) or not isinstance(data.get("cases"), list):
        raise DatasetError("dataset must be an object with a 'cases' list")
    cases = [_parse_case(raw, i) for i, raw in enumerate(data["cases"])]
    seen = set()
    for c in cases:
        if c.id in seen:
            raise DatasetError(f"duplicate case id {c.id!r}")
        seen.add(c.id)
    if expected_count is not None and len(cases) != expected_count:
        raise DatasetError(f"expected exactly {expected_count} cases, found {len(cases)}")
    return cases


def load_dataset(
    path: Path = DEFAULT_DATASET_PATH, expected_count: Optional[int] = EXPECTED_CASE_COUNT
) -> list:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DatasetError(f"{path}: invalid JSON: {exc}") from exc
    return parse_dataset(data, expected_count)


def select_cases(
    cases: Iterable[EvalCase],
    families: Optional[Iterable[str]] = None,
    case_ids: Optional[Iterable[str]] = None,
) -> list:
    """Filter by family and/or id (both given = intersection). Unknown values raise."""
    cases = list(cases)
    if families is not None:
        families = list(families)
        bad = [f for f in families if f not in FAMILIES]
        if bad:
            raise DatasetError(f"unknown family/families: {bad}; expected one of {FAMILIES}")
        cases = [c for c in cases if c.family in families]
    if case_ids is not None:
        case_ids = list(case_ids)
        known = {c.id for c in cases}
        bad = [i for i in case_ids if i not in known]
        if bad:
            raise DatasetError(f"case id(s) not found in selection: {bad}")
        cases = [c for c in cases if c.id in case_ids]
    return cases
