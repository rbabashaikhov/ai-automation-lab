from ingestion.catalog import parse_catalog_page, discover_catalog
from ingestion.http import HttpClient, HttpResponse


def test_parse_catalog_page_extracts_entries_and_next_url(catalog_page1_html):
    page = parse_catalog_page(
        "https://galaxystore.ru/catalog/televizory/year=2026/", catalog_page1_html
    )

    assert page.current_page == 1
    assert page.pages_count == 3
    assert len(page.entries) > 0
    assert page.next_url == "https://galaxystore.ru/catalog/televizory/year=2026/?page=2"

    first = page.entries[0]
    assert first.external_id == "3965052"
    assert first.mpn_code == "UE50M70HAUXPY"
    assert first.series_hint == "M70"
    assert first.price == 53990
    assert first.sale_price == 45490


def test_parse_last_catalog_page_has_no_next_url(catalog_page3_html):
    page = parse_catalog_page(
        "https://galaxystore.ru/catalog/televizory/year=2026/?page=3", catalog_page3_html
    )
    assert page.current_page == 3
    assert page.pages_count == 3
    assert page.next_url is None


def test_parse_catalog_page_deduplicates_by_external_id(catalog_page1_html):
    page = parse_catalog_page("https://example.test/", catalog_page1_html)
    ids = [e.external_id for e in page.entries]
    assert len(ids) == len(set(ids))


class _FakeHttpClient:
    """Serves fixed responses keyed by URL, for pagination-loop tests."""

    def __init__(self, responses: dict[str, str]):
        self._responses = responses
        self.requested_urls: list[str] = []

    def get(self, url: str) -> HttpResponse:
        self.requested_urls.append(url)
        return HttpResponse(url=url, status_code=200, text=self._responses[url])


def test_discover_catalog_respects_max_pages(catalog_page1_html, catalog_page3_html):
    start_url = "https://galaxystore.ru/catalog/televizory/year=2026/"
    next_url = "https://galaxystore.ru/catalog/televizory/year=2026/?page=2"
    client = _FakeHttpClient({start_url: catalog_page1_html, next_url: catalog_page3_html})

    entries, pages = discover_catalog(client, start_url, max_pages=1)

    assert len(pages) == 1
    assert client.requested_urls == [start_url]
    assert len(entries) == len(pages[0].entries)


def test_discover_catalog_stops_on_pagination_loop(catalog_page1_html):
    """A page whose 'next' link points back at itself must not loop forever."""
    start_url = "https://galaxystore.ru/catalog/televizory/year=2026/"
    # Rewrite the HTML so rel="next" points back at start_url itself.
    looping_html = catalog_page1_html.replace(
        'href="https://galaxystore.ru/catalog/televizory/year=2026/?page=2"',
        f'href="{start_url}"',
    )
    client = _FakeHttpClient({start_url: looping_html})

    entries, pages = discover_catalog(client, start_url, max_pages=None)

    assert client.requested_urls == [start_url]
    assert len(pages) == 1
