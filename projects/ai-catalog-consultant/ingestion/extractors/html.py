"""Last-resort HTML fallback extraction.

Used only for fields the structured sources (`json_ld`, `digital_data`,
`sm_params`) didn't provide. Confirmed present on live GalaxyStore pages
as of 2026-09: `<link rel="next">` for pagination, `og:image`/`og:title`
meta tags, and `<title>`. This module never attempts to scrape the spec
table from raw markup -- `sm_params` already gives a clean structured
version of that data, and re-deriving it from HTML would just duplicate
the "grouped" bulldozer parsing the legacy workflow used, adding fragility
without adding coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_NEXT_LINK_RE = re.compile(
    r'<link[^>]+rel=["\']next["\'][^>]+href=["\']([^"\']+)["\']', re.IGNORECASE
)
_OG_IMAGE_RE = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_META_DESCRIPTION_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE
)
_TITLE_RE = re.compile(r"<title>([^<]*)</title>", re.IGNORECASE)
_CANONICAL_RE = re.compile(
    r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', re.IGNORECASE
)


@dataclass
class HtmlFallback:
    extraction_source: str = "html"
    next_page_url: str | None = None
    canonical_url: str | None = None
    title: str | None = None
    og_image: str | None = None
    meta_description: str | None = None


def extract_html_fallback(html: str) -> HtmlFallback:
    next_match = _NEXT_LINK_RE.search(html)
    canonical_match = _CANONICAL_RE.search(html)
    title_match = _TITLE_RE.search(html)
    image_match = _OG_IMAGE_RE.search(html)
    description_match = _META_DESCRIPTION_RE.search(html)

    return HtmlFallback(
        next_page_url=next_match.group(1) if next_match else None,
        canonical_url=canonical_match.group(1) if canonical_match else None,
        title=title_match.group(1).strip() if title_match else None,
        og_image=image_match.group(1) if image_match else None,
        meta_description=description_match.group(1) if description_match else None,
    )
