import json

import pytest

from n8n_tool.cli import _run_create, _run_update
from n8n_tool.client import N8nApiError


def make_local_file(tmp_path, **overrides):
    workflow = {
        "name": "API Tool Test",
        "nodes": [
            {
                "id": "n1",
                "name": "Manual Trigger",
                "type": "n8n-nodes-base.manualTrigger",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
            },
            {
                "id": "n2",
                "name": "Set",
                "type": "n8n-nodes-base.set",
                "typeVersion": 3,
                "position": [200, 0],
                "parameters": {"message": "n8n API deployment works"},
            },
        ],
        "connections": {"Manual Trigger": {"main": [[{"node": "Set", "type": "main", "index": 0}]]}},
        "settings": {},
    }
    workflow.update(overrides)
    path = tmp_path / "workflow.json"
    path.write_text(json.dumps(workflow))
    return path, workflow


class FakeClient:
    def __init__(self, get_workflow_result=None, create_result=None, update_result=None):
        self._get_workflow_result = get_workflow_result
        self._create_result = create_result
        self._update_result = update_result
        self.get_calls = []
        self.create_calls = []
        self.update_calls = []

    def get_workflow(self, workflow_id):
        self.get_calls.append(workflow_id)
        if isinstance(self._get_workflow_result, Exception):
            raise self._get_workflow_result
        if callable(self._get_workflow_result):
            return self._get_workflow_result(len(self.get_calls))
        return self._get_workflow_result

    def create_workflow(self, payload):
        self.create_calls.append(payload)
        if isinstance(self._create_result, Exception):
            raise self._create_result
        return self._create_result

    def update_workflow(self, workflow_id, payload):
        self.update_calls.append((workflow_id, payload))
        if isinstance(self._update_result, Exception):
            raise self._update_result
        return self._update_result


# ------------------------------------------------------------- create() ---


def test_create_dry_run_performs_no_write(tmp_path):
    path, _ = make_local_file(tmp_path)
    client = FakeClient()
    exit_code, created = _run_create(client, str(path), dry_run=True, assume_yes=False, json_output=False)
    assert exit_code == 0
    assert created is None
    assert client.create_calls == []


def test_create_declined_confirmation_performs_no_write(tmp_path, monkeypatch):
    path, _ = make_local_file(tmp_path)
    monkeypatch.setattr("builtins.input", lambda *_: "")  # empty -> default N
    client = FakeClient()
    exit_code, created = _run_create(client, str(path), dry_run=False, assume_yes=False, json_output=False)
    assert exit_code == 1
    assert client.create_calls == []


def test_create_with_yes_skips_prompt_and_creates(tmp_path):
    path, workflow = make_local_file(tmp_path)
    created_response = dict(workflow, id="new-id", active=False)
    client = FakeClient(get_workflow_result=created_response, create_result=created_response)
    exit_code, created = _run_create(client, str(path), dry_run=False, assume_yes=True, json_output=False)
    assert exit_code == 0
    assert len(client.create_calls) == 1
    assert created["id"] == "new-id"
    # create payload must not contain server-only fields
    payload = client.create_calls[0]
    assert "id" not in payload
    assert "active" not in payload


def test_create_blocked_by_secret_scan(tmp_path):
    path, workflow = make_local_file(tmp_path)
    workflow["nodes"][1]["parameters"]["apiKey"] = "sk-liveabcdEFGH123456789012"
    path.write_text(json.dumps(workflow))
    client = FakeClient()
    exit_code, created = _run_create(client, str(path), dry_run=False, assume_yes=True, json_output=False)
    assert exit_code == 1
    assert created is None
    assert client.create_calls == []


def test_create_blocked_by_structural_validation_error(tmp_path):
    path, workflow = make_local_file(tmp_path)
    workflow["connections"]["Manual Trigger"]["main"][0][0]["node"] = "Missing Node"
    path.write_text(json.dumps(workflow))
    client = FakeClient()
    exit_code, created = _run_create(client, str(path), dry_run=True, assume_yes=True, json_output=False)
    assert exit_code == 1
    assert created is None


def test_create_readback_verification_success(tmp_path):
    path, workflow = make_local_file(tmp_path)
    readback = dict(workflow, id="new-id", active=False)
    client = FakeClient(get_workflow_result=readback, create_result={"id": "new-id"})
    exit_code, created = _run_create(client, str(path), dry_run=False, assume_yes=True, json_output=False)
    assert exit_code == 0
    assert created["id"] == "new-id"


def test_create_readback_verification_failure(tmp_path):
    path, workflow = make_local_file(tmp_path)
    mismatched_readback = dict(workflow, id="new-id", active=False, name="Different Name")
    client = FakeClient(get_workflow_result=mismatched_readback, create_result={"id": "new-id"})
    exit_code, created = _run_create(client, str(path), dry_run=False, assume_yes=True, json_output=False)
    assert exit_code == 1  # verification failed even though the write happened


def test_create_api_error_on_write(tmp_path):
    path, _ = make_local_file(tmp_path)
    client = FakeClient(create_result=N8nApiError("HTTP 400: bad request"))
    exit_code, created = _run_create(client, str(path), dry_run=False, assume_yes=True, json_output=False)
    assert exit_code == 1
    assert created is None


def test_create_invalid_json_file(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not valid json")
    client = FakeClient()
    exit_code, created = _run_create(client, str(path), dry_run=True, assume_yes=True, json_output=False)
    assert exit_code == 1
    assert created is None


# ------------------------------------------------------------- update() ---


def test_update_dry_run_performs_no_write(tmp_path):
    path, workflow = make_local_file(tmp_path)
    remote = dict(workflow, id="wf-1", name="Old Name")
    client = FakeClient(get_workflow_result=remote)
    exit_code = _run_update(client, "wf-1", str(path), dry_run=True, assume_yes=False, json_output=False, allow_protected=False)
    assert exit_code == 0
    assert client.update_calls == []


def test_update_noop_when_no_meaningful_diff(tmp_path, capsys):
    path, workflow = make_local_file(tmp_path)
    remote = dict(workflow, id="wf-1", active=False)
    client = FakeClient(get_workflow_result=remote)
    exit_code = _run_update(client, "wf-1", str(path), dry_run=False, assume_yes=True, json_output=False, allow_protected=False)
    assert exit_code == 0
    assert client.update_calls == []  # no unnecessary API write
    assert "No changes detected" in capsys.readouterr().out


def test_update_declined_confirmation_performs_no_write(tmp_path, monkeypatch):
    path, workflow = make_local_file(tmp_path)
    remote = dict(workflow, id="wf-1", name="Old Name")
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    client = FakeClient(get_workflow_result=remote)
    exit_code = _run_update(client, "wf-1", str(path), dry_run=False, assume_yes=False, json_output=False, allow_protected=False)
    assert exit_code == 1
    assert client.update_calls == []


def test_update_with_yes_creates_backup_and_updates(tmp_path):
    path, workflow = make_local_file(tmp_path)
    remote = dict(workflow, id="wf-1", name="Old Name")
    readback = dict(workflow, id="wf-1")

    call_sequence = {"n": 0}

    def get_workflow_sequence(call_number):
        # 1st call: fetch remote before diff/backup; 2nd: backup_workflow's own
        # fetch; 3rd: read-back after update.
        return remote if call_number <= 2 else readback

    client = FakeClient(get_workflow_result=get_workflow_sequence, update_result=readback)
    exit_code = _run_update(
        client,
        "wf-1",
        str(path),
        dry_run=False,
        assume_yes=True,
        json_output=False,
        allow_protected=False,
        backup_dir=tmp_path / "backups",
    )
    assert exit_code == 0
    assert len(client.update_calls) == 1
    updated_id, payload = client.update_calls[0]
    assert updated_id == "wf-1"
    assert payload["name"] == "API Tool Test"


def test_update_protected_workflow_blocked_without_flag(tmp_path):
    path, workflow = make_local_file(tmp_path)
    client = FakeClient(get_workflow_result=dict(workflow, id="ZO8DXsaYdRGvk3cL"))
    exit_code = _run_update(
        client, "ZO8DXsaYdRGvk3cL", str(path), dry_run=False, assume_yes=True, json_output=False, allow_protected=False
    )
    assert exit_code == 1
    assert client.get_calls == []  # blocked before even fetching remote
    assert client.update_calls == []


def test_update_protected_workflow_allowed_with_flag(tmp_path):
    path, workflow = make_local_file(tmp_path)
    remote = dict(workflow, id="ZO8DXsaYdRGvk3cL", name="Old Name")
    client = FakeClient(get_workflow_result=remote, update_result=dict(workflow, id="ZO8DXsaYdRGvk3cL"))
    exit_code = _run_update(
        client, "ZO8DXsaYdRGvk3cL", str(path), dry_run=True, assume_yes=True, json_output=False, allow_protected=True
    )
    assert exit_code == 0  # dry-run, just confirming the guard doesn't block planning


def test_update_get_remote_api_error(tmp_path):
    path, _ = make_local_file(tmp_path)
    client = FakeClient(get_workflow_result=N8nApiError("HTTP 404"))
    exit_code = _run_update(client, "missing-id", str(path), dry_run=False, assume_yes=True, json_output=False, allow_protected=False)
    assert exit_code == 1
    assert client.update_calls == []
