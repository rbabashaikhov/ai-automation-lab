from indexing.metadata import (
    SECTION_ORDER,
    SPEC_GROUP_TO_SECTION,
    approx_token_count,
    build_structured_metadata,
    section_for_spec_group,
)
from indexing.models import Spec


def test_all_observed_spec_groups_map_to_a_declared_section():
    # The 18 spec_group values actually present in the 75-product production
    # corpus (see Phase 3A corpus analysis) -- every one must resolve to a
    # section in SECTION_ORDER, none silently unmapped.
    observed_groups = [
        "Интерфейсы", "Изображение", "Функции", "Поддержка Smart TV",
        "Размеры и вес", "Заводские данные", "Беспроводная связь",
        "Игровой режим", "Экран и разрешение", "Звук",
        "Основные характеристики", "Корпус", "Настройка изображения",
        "Технологии приема сигнала", "Комплектация", "Электропитание",
        "Проекционная система", "Поддерживаемые форматы",
    ]
    for group in observed_groups:
        assert group in SPEC_GROUP_TO_SECTION
        assert section_for_spec_group(group) in SECTION_ORDER


def test_unrecognized_spec_group_falls_back_not_dropped():
    assert section_for_spec_group("Совершенно новая группа") in SECTION_ORDER


def test_approx_token_count_empty_is_zero():
    assert approx_token_count("") == 0
    assert approx_token_count(None) == 0


def test_approx_token_count_nonempty_is_positive_and_reasonable():
    text = "Samsung QE65S95HAUXPY OLED 65 дюймов 4K"
    tokens = approx_token_count(text)
    assert 0 < tokens < len(text)


def test_build_structured_metadata_excludes_raw_payload(oled_product_and_specs):
    product, _ = oled_product_and_specs
    metadata = build_structured_metadata(product)
    assert "raw_payload" not in metadata
    assert "description" not in metadata  # marketing text never in filterable metadata
    assert metadata["model_code"] == product.model_code
    assert metadata["is_available"] == product.is_available
