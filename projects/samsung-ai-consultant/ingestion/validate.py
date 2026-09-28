"""Explicit validation of extracted products before persistence.

Validation never invents missing data to make a product pass -- it only
checks what was actually extracted and reports structured errors. A
product that fails validation is recorded in `ingestion_errors` and
skipped; it never gets a best-effort partial write to `products`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .product import ExtractedProduct

MIN_SPEC_COUNT = 3
ACCEPTABLE_BRANDS = {"samsung"}


@dataclass
class ValidationError:
    code: str
    message: str


@dataclass
class ValidationResult:
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    def add(self, code: str, message: str) -> None:
        self.errors.append(ValidationError(code=code, message=message))


def validate_product(product: ExtractedProduct) -> ValidationResult:
    result = ValidationResult()

    if not product.external_id or not product.external_id.strip():
        result.add("missing_external_id", "external_id is missing or empty")

    if not product.name or not product.name.strip():
        result.add("missing_name", "name is missing or empty")

    if not product.product_url or not product.product_url.strip():
        result.add("missing_product_url", "product_url is missing or empty")

    if not product.brand or product.brand.strip().lower() not in ACCEPTABLE_BRANDS:
        result.add(
            "unexpected_brand",
            f"brand {product.brand!r} is not Samsung or a known-compatible brand",
        )

    if len(product.spec_rows) < MIN_SPEC_COUNT and not (
        product.specs_text and len(product.specs_text.strip()) > 40
    ):
        result.add(
            "insufficient_specifications",
            f"only {len(product.spec_rows)} spec row(s) and no substantial specs_text",
        )

    return result
