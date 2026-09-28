"""Remote workflow backups, taken immediately before every update.

Backups are always sanitized (see n8n_tool.sanitizer) before being written
to disk — never a raw copy of the API response.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import N8nClient
from .sanitizer import sanitize_workflow
from .workflows import slugify

logger = logging.getLogger("n8n_tool")


class BackupError(Exception):
    """Raised when a remote backup could not be created. Update must abort."""


def backup_workflow(
    client: N8nClient, workflow_id: str, backup_dir: Path | str = "backups"
) -> Path:
    """Fetch the current remote state of a workflow and save it, sanitized.

    Returns the path written. Raises BackupError (never leaves a partial
    file) if the remote fetch or the write fails — callers must treat that
    as "update is forbidden", not something to work around.
    """
    try:
        remote = client.get_workflow(workflow_id)
    except Exception as exc:  # noqa: BLE001 - any failure here blocks the update
        raise BackupError(f"Could not fetch remote workflow {workflow_id} for backup: {exc}") from exc

    name = remote.get("name") or workflow_id
    slug = slugify(str(name))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    out_dir = Path(backup_dir) / slug
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{timestamp}_{workflow_id}.json"
        sanitized = sanitize_workflow(remote)
        path.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as exc:
        raise BackupError(f"Could not write backup file: {exc}") from exc

    logger.info("Saved backup %s", path)
    return path
