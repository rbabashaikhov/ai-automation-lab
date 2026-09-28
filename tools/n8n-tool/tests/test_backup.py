import json

import pytest

from n8n_tool.backup import BackupError, backup_workflow


class FakeClient:
    def __init__(self, workflow=None, raise_exc=None):
        self._workflow = workflow
        self._raise_exc = raise_exc
        self.calls = []

    def get_workflow(self, workflow_id):
        self.calls.append(workflow_id)
        if self._raise_exc:
            raise self._raise_exc
        return self._workflow


def test_backup_writes_sanitized_file(tmp_path):
    workflow = {
        "id": "wf-1",
        "name": "Parsing",
        "nodes": [{"parameters": {"apiKey": "super-secret"}}],
    }
    client = FakeClient(workflow=workflow)
    path = backup_workflow(client, "wf-1", backup_dir=tmp_path)

    assert path.exists()
    assert path.parent.name == "parsing"
    assert path.name.endswith("_wf-1.json")

    saved = json.loads(path.read_text())
    assert saved["nodes"][0]["parameters"]["apiKey"] == "***REDACTED***"
    assert saved["name"] == "Parsing"


def test_backup_raises_and_writes_nothing_on_fetch_failure(tmp_path):
    client = FakeClient(raise_exc=RuntimeError("boom"))
    with pytest.raises(BackupError):
        backup_workflow(client, "wf-1", backup_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_backup_uses_workflow_id_when_name_missing(tmp_path):
    client = FakeClient(workflow={"id": "wf-2", "nodes": []})
    path = backup_workflow(client, "wf-2", backup_dir=tmp_path)
    assert path.parent.name == "wf-2"
