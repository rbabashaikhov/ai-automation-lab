"""Minimal HTTP client for the n8n public REST API (read-only usage).

Uses the official n8n public API (base path ``/api/v1``), authenticated via
the ``X-N8N-API-KEY`` header, as documented at
https://docs.n8n.io/api/authentication/ and https://docs.n8n.io/api/api-reference/.
This client intentionally exposes only HTTP GET — Phase 1 tooling must never
create, update, activate or delete anything in n8n.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

import requests

from . import __version__

logger = logging.getLogger("n8n_tool")

USER_AGENT = f"n8n-tool-automation/{__version__}"

# Transient statuses worth a limited, bounded retry for idempotent GETs.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class N8nApiError(Exception):
    """Base error for problems talking to the n8n API."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class N8nAuthError(N8nApiError):
    """Raised on HTTP 401/403 — invalid or missing API key."""


class N8nConnectionError(N8nApiError):
    """Raised when the request could not reach the server at all."""


class N8nClient:
    """Thin, read-only wrapper around the n8n public REST API."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 15.0,
        max_retries: int = 3,
        retry_backoff: float = 1.0,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.session = session or requests.Session()

    def _headers(self) -> dict[str, str]:
        return {
            "X-N8N-API-KEY": self._api_key,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Perform a GET request and return the parsed JSON body.

        Retries a bounded number of times on 429/5xx responses only.
        Never retries indefinitely.
        """
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.get(
                    url, headers=self._headers(), params=params, timeout=self.timeout
                )
            except requests.exceptions.Timeout as exc:
                raise N8nConnectionError(f"Timed out connecting to {url}") from exc
            except requests.exceptions.ConnectionError as exc:
                raise N8nConnectionError(f"Could not connect to {url}") from exc

            if response.status_code in (401, 403):
                raise N8nAuthError(
                    f"n8n returned HTTP {response.status_code} (authentication failed)",
                    status_code=response.status_code,
                )

            if response.status_code in RETRYABLE_STATUS_CODES and attempt <= self.max_retries:
                wait = self.retry_backoff * (2 ** (attempt - 1))
                logger.warning(
                    "n8n returned HTTP %s, retrying in %.1fs (attempt %s/%s)",
                    response.status_code,
                    wait,
                    attempt,
                    self.max_retries,
                )
                time.sleep(wait)
                continue

            if not response.ok:
                raise N8nApiError(
                    f"n8n returned HTTP {response.status_code}",
                    status_code=response.status_code,
                )

            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise N8nApiError("n8n returned a non-JSON response") from exc

    def get_workflows_page(self, limit: int = 100, cursor: str | None = None) -> dict[str, Any]:
        """Fetch one page of GET /api/v1/workflows.

        The n8n public API paginates via an opaque ``nextCursor`` field
        rather than page numbers/offsets.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        data = self.get("/api/v1/workflows", params=params)
        if isinstance(data, list):
            # Defensive fallback in case a server/version returns a bare array.
            return {"data": data, "nextCursor": None}
        if isinstance(data, dict):
            return {"data": data.get("data", []), "nextCursor": data.get("nextCursor")}
        raise N8nApiError("Unexpected response shape for GET /api/v1/workflows")

    def get_workflow(self, workflow_id: str) -> dict[str, Any]:
        """Fetch GET /api/v1/workflows/{id}."""
        data = self.get(f"/api/v1/workflows/{workflow_id}")
        if not isinstance(data, dict):
            raise N8nApiError("Unexpected response shape for GET /api/v1/workflows/{id}")
        return data
