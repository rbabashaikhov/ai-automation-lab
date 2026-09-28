"""Extraction cascade for Samsung product/catalog pages.

Each module here is responsible for exactly one structured-data source
found on GalaxyStore pages, identified by `extraction_source`:

- `json_ld`      -- <script type="application/ld+json"> Product/BreadcrumbList blocks
- `digital_data` -- window.digitalData
- `sm_params`    -- SM_PARAMS.catalog (listing pages) / SM_PARAMS.product (detail pages)
- `html`         -- last-resort regex/BeautifulSoup fallback over raw HTML

None of these raise on missing data -- each returns whatever it found (an
object with fields set to None where absent), which callers merge under an
explicit precedence order documented in `ingestion/product.py` and
`ingestion/catalog.py`.
"""

# Imported under a non-"html" name deliberately: `ingestion.extractors.html`
# is also a *submodule* of this package (the HTML-fallback extractor), and
# `from .extractors import html` must resolve to that submodule, not get
# shadowed by an `extractors.html` attribute pointing at the stdlib module.
import html as _html_stdlib
import re


def decode_entities(text: str | None) -> str | None:
    """Decode HTML entities (e.g. `&quot;`, `&#39;`) in scraped text fields.

    GalaxyStore embeds pre-encoded HTML in JSON string values (product
    names contain literal `&quot;` for the inch mark), so plain
    `json.loads` alone leaves entities undecoded.
    """
    if text is None:
        return None
    return _html_stdlib.unescape(text)


def extract_balanced_json(html: str, start_pattern: str) -> str | None:
    """Extract a balanced `{...}` object following `start_pattern`.

    `start_pattern` must be a regex matching up to and including the
    opening `{` (e.g. `r"SM_PARAMS\\.product\\s*=\\s*\\{"`). A naive
    non-greedy regex like `\\{.*?\\};` breaks as soon as the object
    contains a nested `};`-shaped substring inside a string value (this
    happens in real GalaxyStore payloads), so this walks the text
    character-by-character, tracking string state and brace depth.
    Returns None if the pattern isn't found or braces never balance.
    """
    m = re.search(start_pattern, html)
    if not m:
        return None
    start = m.end() - 1  # position of the opening '{'
    i = start
    depth = 0
    in_string = False
    quote_char = ""
    n = len(html)
    while i < n:
        ch = html[i]
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == quote_char:
                in_string = False
        else:
            if ch in ("'", '"'):
                in_string = True
                quote_char = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return html[start : i + 1]
        i += 1
    return None
