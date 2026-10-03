"""Deterministic chunking of one product's document along semantic section
boundaries (see `metadata.py` for the section taxonomy), not arbitrary
character windows.

## Stable chunk identity

`chunks` enforces `UNIQUE (document_id, chunk_index)` -- there is no
natural-key column for "the gaming chunk of this product" at the schema
level (see db/migrations/004_rag.sql, read directly rather than assumed).
Rather than invent one, this module follows the same strategy
db/README.md already documents for re-indexing in general ("delete its
existing chunks and re-insert, since chunk boundaries can shift"): every
rebuild deletes *all* of a document's existing chunks and re-inserts a
fresh, deterministically-ordered set (see `repository.py`). `chunk_index`
is assigned by always walking `SECTION_ORDER` in the same fixed order and
skipping empty sections, so in practice the same section reliably lands
at the same `chunk_index` across rebuilds *as long as which sections are
present doesn't change* -- but nothing depends on that holding, because
old rows are gone before new ones are inserted. `metadata.section` (a
stable string key, e.g. `"gaming"`) is the actual durable identity for
"this logical chunk," carried in every chunk row precisely so a
downstream consumer (or a future smarter re-indexer) can match chunks by
section rather than by `chunk_index` position. This is the simplest
strategy that is safe under the current schema -- see the Phase 3A ADR
for the alternatives considered (e.g. a synthetic `section` uniqueness
constraint) and why they weren't worth the added complexity yet.

## Context in every chunk

Every chunk restates a short identity preamble (name, model, category,
section label) so it stays understandable in isolation -- never the full
document.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .builder import HEADER_DUPLICATE_SPEC_NAMES, build_header_lines
from .metadata import (
    OVERVIEW,
    SECTION_LABELS,
    SECTION_ORDER,
    build_structured_metadata,
    group_specs_by_section,
)
from .models import Product, Spec


@dataclass
class ChunkDraft:
    chunk_index: int
    section: str
    content: str
    content_hash: str
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _identity_preamble(product: Product, section: str) -> list[str]:
    lines = [f"Samsung {product.name}"]
    if product.model_code:
        lines.append(f"Модель: {product.model_code}")
    if product.category:
        lines.append(f"Категория: {product.category}")
    lines.append(f"Раздел: {SECTION_LABELS[section]}")
    return lines


def _spec_lines(specs: list[Spec]) -> list[str]:
    return [
        f"{spec.spec_name}: {spec.spec_value}"
        for spec in specs
        if spec.spec_name not in HEADER_DUPLICATE_SPEC_NAMES
    ]


def build_chunks(product: Product, specs: list[Spec]) -> list[ChunkDraft]:
    by_section = group_specs_by_section(specs)
    base_metadata = build_structured_metadata(product)

    chunks: list[ChunkDraft] = []
    chunk_index = 0

    # Overview chunk: always present (it's the product's own identity +
    # typed attributes), even if "Основные характеристики" specs are thin.
    overview_lines = build_header_lines(product)
    overview_specs = _spec_lines(by_section[OVERVIEW])
    if overview_specs:
        overview_lines.append("")
        overview_lines.extend(overview_specs)
    content = "\n".join(overview_lines).strip()
    chunks.append(
        ChunkDraft(
            chunk_index=chunk_index,
            section=OVERVIEW,
            content=content,
            content_hash=compute_content_hash(content),
            metadata={**base_metadata, "section": OVERVIEW},
        )
    )
    chunk_index += 1

    for section in SECTION_ORDER:
        if section == OVERVIEW:
            continue
        spec_lines = _spec_lines(by_section[section])
        if not spec_lines:
            continue  # no empty chunks
        lines = _identity_preamble(product, section)
        lines.append("")
        lines.extend(spec_lines)
        content = "\n".join(lines).strip()
        chunks.append(
            ChunkDraft(
                chunk_index=chunk_index,
                section=section,
                content=content,
                content_hash=compute_content_hash(content),
                metadata={**base_metadata, "section": section},
            )
        )
        chunk_index += 1

    return chunks
