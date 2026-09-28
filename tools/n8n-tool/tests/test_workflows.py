from n8n_tool.workflows import find_workflows_by_name, iter_workflows, list_workflows, slugify, summarize


class FakeClient:
    """Fake N8nClient exposing only get_workflows_page, paginated via cursor."""

    def __init__(self, pages):
        self._pages = pages  # list of {"data": [...], "nextCursor": ...}
        self.calls = []

    def get_workflows_page(self, limit=100, cursor=None):
        self.calls.append({"limit": limit, "cursor": cursor})
        index = len(self.calls) - 1
        return self._pages[index]


def test_iter_workflows_follows_pagination():
    pages = [
        {"data": [{"id": "1"}, {"id": "2"}], "nextCursor": "page2"},
        {"data": [{"id": "3"}], "nextCursor": None},
    ]
    client = FakeClient(pages)
    workflows = list(iter_workflows(client))
    assert [w["id"] for w in workflows] == ["1", "2", "3"]
    assert client.calls[0]["cursor"] is None
    assert client.calls[1]["cursor"] == "page2"


def test_iter_workflows_single_page():
    client = FakeClient([{"data": [{"id": "1"}], "nextCursor": None}])
    workflows = list(iter_workflows(client))
    assert len(workflows) == 1
    assert len(client.calls) == 1


def test_list_workflows_returns_all():
    client = FakeClient([{"data": [{"id": "1"}, {"id": "2"}], "nextCursor": None}])
    workflows = list_workflows(client)
    assert len(workflows) == 2


def test_find_workflows_by_name_exact_match():
    client = FakeClient(
        [{"data": [{"id": "1", "name": "Parsing"}, {"id": "2", "name": "Other"}], "nextCursor": None}]
    )
    matches = find_workflows_by_name(client, "Parsing")
    assert len(matches) == 1
    assert matches[0]["id"] == "1"


def test_find_workflows_by_name_case_insensitive():
    client = FakeClient([{"data": [{"id": "1", "name": "Parsing"}], "nextCursor": None}])
    matches = find_workflows_by_name(client, "parsing")
    assert len(matches) == 1


def test_find_workflows_by_name_no_match():
    client = FakeClient([{"data": [{"id": "1", "name": "Other"}], "nextCursor": None}])
    matches = find_workflows_by_name(client, "Parsing")
    assert matches == []


def test_find_workflows_by_name_multiple_matches_returned_not_picked():
    client = FakeClient(
        [
            {
                "data": [
                    {"id": "1", "name": "Parsing"},
                    {"id": "2", "name": "Parsing"},
                ],
                "nextCursor": None,
            }
        ]
    )
    matches = find_workflows_by_name(client, "Parsing")
    assert len(matches) == 2
    assert {m["id"] for m in matches} == {"1", "2"}


def test_find_workflows_by_name_prefers_exact_over_partial():
    client = FakeClient(
        [
            {
                "data": [
                    {"id": "1", "name": "Parsing"},
                    {"id": "2", "name": "Parsing v2 draft"},
                ],
                "nextCursor": None,
            }
        ]
    )
    matches = find_workflows_by_name(client, "Parsing")
    assert len(matches) == 1
    assert matches[0]["id"] == "1"


def test_find_workflows_by_name_falls_back_to_partial():
    client = FakeClient(
        [{"data": [{"id": "2", "name": "Parsing v2 draft"}], "nextCursor": None}]
    )
    matches = find_workflows_by_name(client, "Parsing")
    assert len(matches) == 1
    assert matches[0]["id"] == "2"


def test_summarize_extracts_key_fields():
    workflow = {"id": "1", "name": "Parsing", "active": True, "updatedAt": "2026-01-01", "nodes": []}
    summary = summarize(workflow)
    assert summary == {"id": "1", "name": "Parsing", "active": True, "updatedAt": "2026-01-01"}


def test_slugify_basic():
    assert slugify("Parsing") == "parsing"


def test_slugify_special_characters():
    assert slugify("Samsung TV / GalaxyStore Parsing!") == "samsung-tv-galaxystore-parsing"


def test_slugify_empty_falls_back():
    assert slugify("   ") == "workflow"
