"""Polite, retrying HTTP client for ingestion.

Wraps `requests` with: an explicit timeout, a realistic User-Agent,
exponential-backoff retries for transient server/rate-limit errors, and a
configurable delay before each request to avoid hammering the source site.
Retries are limited to status codes that are plausibly transient; a 404 is
never retried, since retrying a real "not found" only wastes requests and
delays surfacing the error.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})


class FetchError(Exception):
    """Raised when a URL could not be fetched after retries."""

    def __init__(self, url: str, message: str, status_code: int | None = None):
        super().__init__(f"{message} (url={url}, status_code={status_code})")
        self.url = url
        self.status_code = status_code


@dataclass
class HttpResponse:
    url: str
    status_code: int
    text: str


class HttpClient:
    """Sequential HTTP client: one request at a time, with a polite delay.

    Not thread-safe by design — ingestion is intentionally single-threaded
    to keep load on the source site predictable.
    """

    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float,
        max_retries: int,
        backoff_base_seconds: float,
        delay_seconds: float,
        session: requests.Session | None = None,
    ):
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.backoff_base_seconds = backoff_base_seconds
        self.delay_seconds = delay_seconds
        self._session = session or requests.Session()
        self._session.headers.update({"User-Agent": user_agent})
        self._last_request_at: float | None = None

    def _wait_for_polite_delay(self) -> None:
        if self.delay_seconds <= 0 or self._last_request_at is None:
            return
        elapsed = time.monotonic() - self._last_request_at
        remaining = self.delay_seconds - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def get(self, url: str) -> HttpResponse:
        """Fetch `url`, retrying transient failures with exponential backoff.

        Raises FetchError if the URL could not be fetched after retries, or
        immediately (no retry) on a 404 or other non-retryable status.
        """
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._wait_for_polite_delay()
            self._last_request_at = time.monotonic()
            try:
                resp = self._session.get(url, timeout=self.timeout_seconds)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    break
                self._sleep_backoff(attempt)
                continue

            if resp.status_code == 200:
                return HttpResponse(url=url, status_code=resp.status_code, text=resp.text)

            if resp.status_code not in RETRYABLE_STATUS_CODES:
                raise FetchError(
                    url, "non-retryable HTTP status", status_code=resp.status_code
                )

            last_exc = FetchError(url, "retryable HTTP status", status_code=resp.status_code)
            if attempt >= self.max_retries:
                break
            logger.warning(
                "Retryable status %s for %s (attempt %s/%s)",
                resp.status_code,
                url,
                attempt + 1,
                self.max_retries,
            )
            self._sleep_backoff(attempt)

        if isinstance(last_exc, FetchError):
            raise last_exc
        raise FetchError(url, f"request failed after retries: {last_exc}")

    def _sleep_backoff(self, attempt: int) -> None:
        time.sleep(self.backoff_base_seconds * (2**attempt))
