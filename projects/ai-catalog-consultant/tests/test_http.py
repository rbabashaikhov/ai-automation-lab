from unittest.mock import MagicMock

import pytest
import requests

from ingestion.http import FetchError, HttpClient


def _resp(status_code: int, text: str = "ok") -> MagicMock:
    m = MagicMock()
    m.status_code = status_code
    m.text = text
    return m


def _make_client(session: MagicMock, **overrides) -> HttpClient:
    defaults = dict(
        user_agent="test-agent",
        timeout_seconds=5.0,
        max_retries=3,
        backoff_base_seconds=0.0,  # no real sleeping in tests
        delay_seconds=0.0,
        session=session,
    )
    defaults.update(overrides)
    return HttpClient(**defaults)


def test_get_returns_response_on_first_success():
    session = MagicMock()
    session.get.return_value = _resp(200, "hello")
    client = _make_client(session)

    result = client.get("https://example.test/")

    assert result.status_code == 200
    assert result.text == "hello"
    assert session.get.call_count == 1


def test_get_retries_on_retryable_status_then_succeeds():
    session = MagicMock()
    session.get.side_effect = [_resp(503), _resp(502), _resp(200, "ok")]
    client = _make_client(session)

    result = client.get("https://example.test/")

    assert result.status_code == 200
    assert session.get.call_count == 3


def test_get_does_not_retry_404():
    session = MagicMock()
    session.get.return_value = _resp(404)
    client = _make_client(session)

    with pytest.raises(FetchError) as excinfo:
        client.get("https://example.test/missing")

    assert excinfo.value.status_code == 404
    assert session.get.call_count == 1


def test_get_raises_after_exhausting_retries():
    session = MagicMock()
    session.get.return_value = _resp(500)
    client = _make_client(session, max_retries=2)

    with pytest.raises(FetchError) as excinfo:
        client.get("https://example.test/")

    assert excinfo.value.status_code == 500
    assert session.get.call_count == 3  # initial attempt + 2 retries


def test_get_retries_on_connection_error():
    session = MagicMock()
    session.get.side_effect = [requests.ConnectionError("boom"), _resp(200, "ok")]
    client = _make_client(session)

    result = client.get("https://example.test/")

    assert result.status_code == 200
    assert session.get.call_count == 2
