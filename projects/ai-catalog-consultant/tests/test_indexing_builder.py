from indexing.builder import HEADER_DUPLICATE_SPEC_NAMES, build_document, build_document_content


def test_builder_deterministic_output(oled_product_and_specs):
    product, specs = oled_product_and_specs
    doc1 = build_document(product, specs)
    doc2 = build_document(product, specs)
    assert doc1.content == doc2.content
    assert doc1.content_hash == doc2.content_hash


def test_builder_stable_hash_is_sha256_hex(oled_product_and_specs):
    product, specs = oled_product_and_specs
    doc = build_document(product, specs)
    assert len(doc.content_hash) == 64
    int(doc.content_hash, 16)  # raises if not valid hex


def test_builder_includes_typed_attributes(oled_product_and_specs):
    product, specs = oled_product_and_specs
    doc = build_document(product, specs)
    assert product.model_code in doc.content
    assert product.name in doc.content
    assert "OLED" in doc.content
    assert '65"' in doc.content or "65.0" not in doc.content  # formatted, not raw float


def test_builder_includes_real_specs(oled_product_and_specs):
    product, specs = oled_product_and_specs
    doc = build_document(product, specs)
    non_header_dup_specs = [s for s in specs if s.spec_name not in HEADER_DUPLICATE_SPEC_NAMES]
    assert non_header_dup_specs, "fixture should have specs beyond the header-duplicate ones"
    sample = non_header_dup_specs[0]
    assert sample.spec_name in doc.content
    assert sample.spec_value in doc.content


def test_builder_excludes_marketing_description(oled_product_and_specs):
    product, specs = oled_product_and_specs
    assert product.description is not None  # sanity: fixture actually has one
    doc = build_document(product, specs)
    assert product.description not in doc.content
    # spot-check the specific marketing artifacts this field is known to contain
    assert "🚚" not in doc.content
    assert "Рассрочка" not in doc.content


def test_builder_no_html_entity_noise(oled_product_and_specs, neo_qled_product_and_specs):
    for product, specs in (oled_product_and_specs, neo_qled_product_and_specs):
        doc = build_document(product, specs)
        assert "&quot;" not in doc.content
        assert "&amp;" not in doc.content


def test_builder_no_none_textual_garbage(oled_product_and_specs, unusual_display_product_and_specs):
    for product, specs in (oled_product_and_specs, unusual_display_product_and_specs):
        doc = build_document(product, specs)
        assert "None" not in doc.content
        assert "null" not in doc.content.lower()


def test_builder_no_duplicate_header_specs_in_body(oled_product_and_specs):
    product, specs = oled_product_and_specs
    doc = build_document(product, specs)
    for header_spec_name in HEADER_DUPLICATE_SPEC_NAMES:
        # allowed to appear at most once, as a "Name: value" body line --
        # never repeated (see test below for the specific known case).
        assert doc.content.count(f"{header_spec_name}:") <= 1


def test_builder_header_duplicate_specs_not_repeated_verbatim(oled_product_and_specs):
    product, specs = oled_product_and_specs
    content = build_document_content(product, specs)
    # "Технология экрана: OLED" (the raw spec line) must not appear, since
    # "Тип экрана: OLED" already states it in the header.
    assert "Технология экрана: OLED" not in content


def test_builder_unusual_display_product_has_no_crash_and_valid_content(unusual_display_product_and_specs):
    product, specs = unusual_display_product_and_specs
    doc = build_document(product, specs)
    assert doc.content
    assert product.model_code in doc.content


def test_builder_metadata_has_structured_filter_fields(oled_product_and_specs):
    product, specs = oled_product_and_specs
    doc = build_document(product, specs)
    for key in ("product_id", "external_id", "model_code", "year", "screen_size_inches",
                "panel_technology", "refresh_rate_hz", "price", "is_available"):
        assert key in doc.metadata
    assert doc.metadata["product_id"] == product.id
    assert doc.metadata["document_type"] == "product_overview"
