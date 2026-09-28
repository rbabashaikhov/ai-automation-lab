"""Read-only helpers built on top of N8nClient: listing, lookup, summarizing."""

from __future__ import annotations

import logging
from typing import Any, Iterator

from .client import N8nClient

logger = logging.getLogger("n8n_tool")


def iter_workflows(client: N8nClient, page_size: int = 100) -> Iterator[dict[str, Any]]:
    """Yield every workflow, transparently following cursor-based pagination."""
    cursor: str | None = None
    while True:
        page = client.get_workflows_page(limit=page_size, cursor=cursor)
        for workflow in page["data"]:
            yield workflow
        cursor = page.get("nextCursor")
        if not cursor:
            break


def list_workflows(client: N8nClient, page_size: int = 100) -> list[dict[str, Any]]:
    workflows = list(iter_workflows(client, page_size=page_size))
    logger.info("Found %d workflows", len(workflows))
    return workflows


def find_workflows_by_name(client: N8nClient, name: str) -> list[dict[str, Any]]:
    """Find workflows whose name matches ``name``.

    Prefers exact (case-insensitive) matches; if none exist, falls back to
    substring matches. Never silently picks a single result when several
    workflows share/contain the given name — callers must inspect the list.
    """
    needle = name.strip().lower()
    exact: list[dict[str, Any]] = []
    partial: list[dict[str, Any]] = []
    for workflow in iter_workflows(client):
        wf_name = str(workflow.get("name", ""))
        wf_name_lower = wf_name.lower()
        if wf_name_lower == needle:
            exact.append(workflow)
        elif needle in wf_name_lower:
            partial.append(workflow)
    return exact if exact else partial


def summarize(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": workflow.get("id"),
        "name": workflow.get("name"),
        "active": workflow.get("active"),
        "updatedAt": workflow.get("updatedAt"),
    }


def slugify(name: str) -> str:
    """Turn a workflow name into a filesystem-safe directory slug."""
    slug_chars = []
    prev_dash = False
    for ch in name.strip().lower():
        if ch.isalnum():
            slug_chars.append(ch)
            prev_dash = False
        elif not prev_dash:
            slug_chars.append("-")
            prev_dash = True
    slug = "".join(slug_chars).strip("-")
    return slug or "workflow"
