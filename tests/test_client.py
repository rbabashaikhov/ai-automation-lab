import requests

from n8n_tool.client import (
    N8nApiError,
    N8nAuthError,
    N8nClient,
    N8nConnectionError,
)

import pytest


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, content=b"{}"):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.content = content
        self.ok = 200 <= status_code < 400

    def json(self):
        return self._json_data


class FakeSession:
    """A fake requests.Session whose .request() returns queued responses/exceptions."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def request(self, method, url, headers=None, params=None, json=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "params": params, "json": json, "timeout": timeout}
        )
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(responses, **kwargs):
    session = FakeSession(responses)
    client = N8nClient(
        base_url="https://n8n.example.com",
        api_key="test-key-123",
        session=session,
        retry_backoff=0,  # no real sleeping in tests
        **kwargs,
    )
    return client, session


def test_get_sends_api_key_header_and_builds_url():
    client, session = make_client([FakeResponse(200, {"ok": True})])
    result = client.get("/api/v1/workflows")
    assert result == {"ok": True}
    call = session.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://n8n.example.com/api/v1/workflows"
    assert call["headers"]["X-N8N-API-KEY"] == "test-key-123"
    assert "n8n-tool-automation" in call["headers"]["User-Agent"]


def test_get_raises_auth_error_on_401():
    client, _ = make_client([FakeResponse(401)])
    with pytest.raises(N8nAuthError):
        client.get("/api/v1/workflows")


def test_get_raises_auth_error_on_403():
    client, _ = make_client([FakeResponse(403)])
    with pytest.raises(N8nAuthError):
        client.get("/api/v1/workflows")


def test_get_raises_connection_error_on_timeout():
    client, _ = make_client([requests.exceptions.Timeout()])
    with pytest.raises(N8nConnectionError):
        client.get("/api/v1/workflows")


def test_get_raises_connection_error_on_connection_error():
    client, _ = make_client([requests.exceptions.ConnectionError()])
    with pytest.raises(N8nConnectionError):
        client.get("/api/v1/workflows")


def test_get_raises_api_error_on_non_retryable_4xx():
    client, _ = make_client([FakeResponse(404)])
    with pytest.raises(N8nApiError):
        client.get("/api/v1/workflows/does-not-exist")


def test_api_error_includes_server_message_detail():
    client, _ = make_client([FakeResponse(400, {"message": "request/body must NOT have additional properties"})])
    with pytest.raises(N8nApiError, match="additional properties"):
        client.get("/api/v1/workflows")


def test_get_retries_on_500_then_succeeds():
    client, session = make_client(
        [FakeResponse(500), FakeResponse(200, {"ok": True})],
        max_retries=3,
    )
    result = client.get("/api/v1/workflows")
    assert result == {"ok": True}
    assert len(session.calls) == 2


def test_get_gives_up_after_max_retries():
    client, session = make_client(
        [FakeResponse(503), FakeResponse(503), FakeResponse(503)],
        max_retries=2,
    )
    with pytest.raises(N8nApiError):
        client.get("/api/v1/workflows")
    assert len(session.calls) == 3  # initial attempt + 2 retries


def test_get_workflows_page_wraps_dict_response():
    client, _ = make_client(
        [FakeResponse(200, {"data": [{"id": "1"}], "nextCursor": "abc"})]
    )
    page = client.get_workflows_page()
    assert page == {"data": [{"id": "1"}], "nextCursor": "abc"}


def test_get_workflows_page_handles_bare_list_response():
    client, _ = make_client([FakeResponse(200, [{"id": "1"}, {"id": "2"}])])
    page = client.get_workflows_page()
    assert page == {"data": [{"id": "1"}, {"id": "2"}], "nextCursor": None}


def test_get_workflows_page_passes_cursor_param():
    client, session = make_client([FakeResponse(200, {"data": [], "nextCursor": None})])
    client.get_workflows_page(limit=50, cursor="page2")
    assert session.calls[0]["params"] == {"limit": 50, "cursor": "page2"}


def test_get_workflow_returns_dict():
    client, _ = make_client([FakeResponse(200, {"id": "1", "name": "Parsing"})])
    workflow = client.get_workflow("1")
    assert workflow == {"id": "1", "name": "Parsing"}


def test_get_workflow_rejects_non_dict_response():
    client, _ = make_client([FakeResponse(200, ["not", "a", "dict"])])
    with pytest.raises(N8nApiError):
        client.get_workflow("1")


def test_create_workflow_sends_post_with_payload():
    client, session = make_client([FakeResponse(200, {"id": "new-1", "name": "Test"})])
    payload = {"name": "Test", "nodes": [], "connections": {}, "settings": {}}
    result = client.create_workflow(payload)
    assert result == {"id": "new-1", "name": "Test"}
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://n8n.example.com/api/v1/workflows"
    assert call["json"] == payload


def test_create_workflow_does_not_retry_on_5xx():
    client, session = make_client([FakeResponse(500)], max_retries=3)
    with pytest.raises(N8nApiError):
        client.create_workflow({"name": "Test", "nodes": [], "connections": {}, "settings": {}})
    assert len(session.calls) == 1  # no retry for a non-idempotent write


def test_update_workflow_sends_put_with_payload():
    client, session = make_client([FakeResponse(200, {"id": "wf-1", "name": "Updated"})])
    payload = {"name": "Updated", "nodes": [], "connections": {}, "settings": {}}
    result = client.update_workflow("wf-1", payload)
    assert result == {"id": "wf-1", "name": "Updated"}
    call = session.calls[0]
    assert call["method"] == "PUT"
    assert call["url"] == "https://n8n.example.com/api/v1/workflows/wf-1"
    assert call["json"] == payload


def test_update_workflow_does_not_retry_on_5xx():
    client, session = make_client([FakeResponse(503)], max_retries=3)
    with pytest.raises(N8nApiError):
        client.update_workflow("wf-1", {"name": "X", "nodes": [], "connections": {}, "settings": {}})
    assert len(session.calls) == 1


def test_create_workflow_rejects_non_dict_response():
    client, _ = make_client([FakeResponse(200, ["not", "a", "dict"])])
    with pytest.raises(N8nApiError):
        client.create_workflow({"name": "X", "nodes": [], "connections": {}, "settings": {}})
