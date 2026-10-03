from indexing.builder import HEADER_DUPLICATE_SPEC_NAMES
from indexing.chunker import build_chunks
from indexing.metadata import OVERVIEW, SECTION_ORDER


def test_chunker_deterministic_order_and_hashes(oled_product_and_specs):
    product, specs = oled_product_and_specs
    chunks1 = build_chunks(product, specs)
    chunks2 = build_chunks(product, specs)
    assert [c.section for c in chunks1] == [c.section for c in chunks2]
    assert [c.chunk_index for c in chunks1] == list(range(len(chunks1)))
    assert [c.content_hash for c in chunks1] == [c.content_hash for c in chunks2]


def test_chunker_sections_follow_declared_order(oled_product_and_specs):
    product, specs = oled_product_and_specs
    chunks = build_chunks(product, specs)
    sections = [c.section for c in chunks]
    # sections present must appear in the same relative order as SECTION_ORDER
    expected_relative = [s for s in SECTION_ORDER if s in sections]
    assert sections == expected_relative


def test_chunker_first_chunk_is_overview(oled_product_and_specs):
    product, specs = oled_product_and_specs
    chunks = build_chunks(product, specs)
    assert chunks[0].section == OVERVIEW
    assert chunks[0].chunk_index == 0


def test_chunker_every_chunk_includes_identity_context(oled_product_and_specs):
    product, specs = oled_product_and_specs
    chunks = build_chunks(product, specs)
    for chunk in chunks:
        assert product.model_code in chunk.content
        assert "Samsung" in chunk.content


def test_chunker_non_overview_chunks_state_section_label(oled_product_and_specs):
    product, specs = oled_product_and_specs
    chunks = build_chunks(product, specs)
    for chunk in chunks[1:]:
        assert "Раздел:" in chunk.content


def test_chunker_no_empty_chunks(oled_product_and_specs, unusual_display_product_and_specs):
    for product, specs in (oled_product_and_specs, unusual_display_product_and_specs):
        for chunk in build_chunks(product, specs):
            assert chunk.content.strip()


def test_chunker_does_not_repeat_full_document_in_every_chunk(oled_product_and_specs):
    product, specs = oled_product_and_specs
    chunks = build_chunks(product, specs)
    # A later section's chunk must not contain an earlier section's specs.
    display_chunk = next(c for c in chunks if c.section == "display")
    gaming_chunk = next(c for c in chunks if c.section == "gaming")
    assert "Игровой режим: Да" not in display_chunk.content
    assert "Поддержка форматов HDR" not in gaming_chunk.content


def test_chunker_preserves_all_non_duplicate_specs_across_chunks(oled_product_and_specs):
    product, specs = oled_product_and_specs
    chunks = build_chunks(product, specs)
    combined = "\n".join(c.content for c in chunks)
    non_header_dup_specs = [s for s in specs if s.spec_name not in HEADER_DUPLICATE_SPEC_NAMES]
    missing = [s for s in non_header_dup_specs if s.spec_value not in combined]
    assert missing == []


def test_chunker_metadata_has_section_and_product_association(oled_product_and_specs):
    product, specs = oled_product_and_specs
    chunks = build_chunks(product, specs)
    for chunk in chunks:
        assert chunk.metadata["product_id"] == product.id
        assert chunk.metadata["section"] == chunk.section
        assert "raw_payload" not in chunk.metadata


def test_chunker_unusual_display_product_produces_valid_chunks(unusual_display_product_and_specs):
    product, specs = unusual_display_product_and_specs
    chunks = build_chunks(product, specs)
    assert len(chunks) >= 2
    assert chunks[0].section == OVERVIEW


def test_chunker_one_relevant_spec_change_only_affects_its_chunk(oled_product_and_specs):
    import dataclasses

    from indexing.models import Spec

    product, specs = oled_product_and_specs
    original_chunks = {c.section: c for c in build_chunks(product, specs)}

    # Change a single gaming-section spec value.
    changed_specs = []
    changed_one = False
    for s in specs:
        if not changed_one and s.spec_group == "Игровой режим":
            changed_specs.append(dataclasses.replace(s, spec_value="Нет"))
            changed_one = True
        else:
            changed_specs.append(s)
    assert changed_one, "fixture must have a gaming-section spec to mutate"

    new_chunks = {c.section: c for c in build_chunks(product, changed_specs)}

    assert new_chunks["gaming"].content_hash != original_chunks["gaming"].content_hash
    for section in original_chunks:
        if section == "gaming":
            continue
        assert new_chunks[section].content_hash == original_chunks[section].content_hash
