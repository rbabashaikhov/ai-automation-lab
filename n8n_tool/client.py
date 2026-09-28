"""HTTP client for the n8n public REST API.

Uses the official n8n public API (base path ``/api/v1``), authenticated via
the ``X-N8N-API-KEY`` header. The exact contract (paths, schemas, which
fields are read-only) was confirmed against this instance's own published
OpenAPI spec at ``GET /api/v1/openapi.yml`` rather than guessed.

This client intentionally exposes only a small, fixed set of operations —
GET (list/get) plus create_workflow/update_workflow — and no generic
``request(method, path, body)`` escape hatch, so the CLI built on top of it
cannot be used to issue arbitrary write calls (activate/deactivate/delete/
credentials/etc. are simply not implemented here).
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

    def _request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        retry: bool = True,
    ) -> Any:
        """Internal request helper shared by get/create_workflow/update_workflow.

        Not exposed publicly with a caller-chosen method/path — every public
        method on this class corresponds to exactly one fixed n8n API
        operation, so the CLI can never turn this into an arbitrary-write
        tool. Retries (bounded, on 429/5xx) only apply to idempotent
        requests (GET); writes are never silently retried.
        """
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                    timeout=self.timeout,
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

            if (
                retry
                and response.status_code in RETRYABLE_STATUS_CODES
                and attempt <= self.max_retries
            ):
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
                detail = _extract_error_detail(response)
                message = f"n8n returned HTTP {response.status_code}"
                if detail:
                    message = f"{message}: {detail}"
                raise N8nApiError(message, status_code=response.status_code)

            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise N8nApiError("n8n returned a non-JSON response") from exc

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """Perform a GET request and return the parsed JSON body.

        Retries a bounded number of times on 429/5xx responses only.
        Never retries indefinitely.
        """
        return self._request("GET", path, params=params, retry=True)

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

    def create_workflow(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /api/v1/workflows — create a new workflow.

        ``payload`` must already be mapped to the API's writable field set
        (see n8n_tool.normalizer.build_create_payload) — this method does
        not filter or validate it. Never retried: a create is not
        idempotent, so a transient error must surface to the caller rather
        than risk a duplicate workflow from a blind retry.
        """
        data = self._request("POST", "/api/v1/workflows", json_body=payload, retry=False)
        if not isinstance(data, dict):
            raise N8nApiError("Unexpected response shape for POST /api/v1/workflows")
        return data

    def update_workflow(self, workflow_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """PUT /api/v1/workflows/{id} — update an existing workflow's content.

        ``payload`` must already be mapped to the API's writable field set
        (see n8n_tool.normalizer.build_update_payload). Never retried, for
        the same reason as create_workflow.
        """
        data = self._request(
            "PUT", f"/api/v1/workflows/{workflow_id}", json_body=payload, retry=False
        )
        if not isinstance(data, dict):
            raise N8nApiError("Unexpected response shape for PUT /api/v1/workflows/{id}")
        return data


def _extract_error_detail(response: requests.Response) -> str | None:
    """Best-effort extraction of n8n's JSON error message, without leaking secrets."""
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, str) and message:
            return message
    return None
