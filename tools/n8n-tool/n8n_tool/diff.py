"""Meaningful diffing between a remote workflow and a local file.

"Meaningful" excludes server-generated noise (updatedAt, createdAt,
versionId, active, isArchived, triggerCount, meta, tags, shared,
activeVersion/activeVersionId, id) and aligns nodes by name (not list
position), so reordering nodes in the source array doesn't show up as a
wholesale rewrite.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .normalizer import count_connections, count_nodes

_MISSING = object()


@dataclass(frozen=True)
class Change:
    path: str
    old: Any
    new: Any

    def format(self) -> str:
        if self.old is _MISSING:
            return f"+ {self.path}: {_short(self.new)}"
        if self.new is _MISSING:
            return f"- {self.path}: {_short(self.old)}"
        return f"~ {self.path}: {_short(self.old)} -> {_short(self.new)}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "old": None if self.old is _MISSING else self.old,
            "new": None if self.new is _MISSING else self.new,
            "added": self.old is _MISSING,
            "removed": self.new is _MISSING,
        }


def _short(value: Any, limit: int = 120) -> str:
    text = repr(value)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


@dataclass
class DiffResult:
    changes: list[Change] = field(default_factory=list)
    old_node_count: int = 0
    new_node_count: int = 0
    old_connection_count: int = 0
    new_connection_count: int = 0

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def format_lines(self) -> list[str]:
        return [change.format() for change in self.changes]


def _normalize_node_for_diff(node: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in node.items() if k not in ("createdAt", "updatedAt")}


def normalize_for_diff(workflow: dict[str, Any]) -> dict[str, Any]:
    """Reduce a workflow dict to the fields that matter for a content diff."""
    nodes_by_name: dict[str, Any] = {}
    for node in workflow.get("nodes") or []:
        if isinstance(node, dict):
            name = node.get("name", "")
            nodes_by_name[name] = _normalize_node_for_diff(node)
    return {
        "name": workflow.get("name") or "",
        "description": workflow.get("description") or "",
        "nodes": nodes_by_name,
        "connections": workflow.get("connections") or {},
        "settings": workflow.get("settings") or {},
        "staticData": workflow.get("staticData") or None,
        "pinData": workflow.get("pinData") or {},
    }


def _deep_diff(old: Any, new: Any, path: str = "") -> list[Change]:
    changes: list[Change] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new)):
            sub_path = f"{path}.{key}" if path else str(key)
            if key not in old:
                changes.append(Change(sub_path, _MISSING, new[key]))
            elif key not in new:
                changes.append(Change(sub_path, old[key], _MISSING))
            else:
                changes.extend(_deep_diff(old[key], new[key], sub_path))
    elif isinstance(old, list) and isinstance(new, list):
        if old != new:
            if len(old) == len(new):
                for index, (o, n) in enumerate(zip(old, new)):
                    changes.extend(_deep_diff(o, n, f"{path}[{index}]"))
            else:
                changes.append(Change(path, old, new))
    else:
        if old != new:
            changes.append(Change(path, old, new))
    return changes


def diff_workflows(old_workflow: dict[str, Any], new_workflow: dict[str, Any]) -> DiffResult:
    """Compute a meaningful diff between two workflow dicts (remote vs local)."""
    old_norm = normalize_for_diff(old_workflow)
    new_norm = normalize_for_diff(new_workflow)
    changes = _deep_diff(old_norm, new_norm)
    return DiffResult(
        changes=changes,
        old_node_count=count_nodes(old_workflow),
        new_node_count=count_nodes(new_workflow),
        old_connection_count=count_connections(old_workflow),
        new_connection_count=count_connections(new_workflow),
    )
