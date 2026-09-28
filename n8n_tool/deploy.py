"""Sidecar deploy-metadata handling and read-back verification.

A local workflow file `workflows/foo.json` may have a sidecar
`workflows/foo.meta.json` recording which remote workflow it corresponds
to:

    {"remoteWorkflowId": "ZO8DXsaYdRGvk3cL", "environment": "production"}

This metadata is purely local tooling state — it is never sent to n8n as
part of a create/update payload. `workflows deploy` uses it to decide
whether to create (no id on file yet) or update (id present).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .diff import DiffResult, diff_workflows

logger = logging.getLogger("n8n_tool")


def meta_path_for(workflow_file: Path | str) -> Path:
    path = Path(workflow_file)
    return path.with_suffix("").with_suffix(".meta.json")


def load_meta(workflow_file: Path | str) -> dict[str, Any] | None:
    meta_path = meta_path_for(workflow_file)
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Could not read metadata file {meta_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Metadata file {meta_path} must contain a JSON object")
    return data


def save_meta(workflow_file: Path | str, data: dict[str, Any]) -> Path:
    meta_path = meta_path_for(workflow_file)
    meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    logger.info("Saved %s", meta_path)
    return meta_path


def resolve_remote_id(workflow_file: Path | str) -> str | None:
    """Return the remote workflow id recorded for this local file, if any."""
    meta = load_meta(workflow_file)
    if not meta:
        return None
    remote_id = meta.get("remoteWorkflowId")
    return remote_id if isinstance(remote_id, str) and remote_id else None


def verify_readback(expected: dict[str, Any], actual: dict[str, Any]) -> DiffResult:
    """Diff what we intended to deploy against what the API reports afterwards.

    Verification passes iff the diff is empty (no meaningful differences).
    """
    return diff_workflows(expected, actual)
