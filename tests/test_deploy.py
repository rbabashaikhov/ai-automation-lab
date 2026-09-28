import json

import pytest

from n8n_tool.deploy import load_meta, meta_path_for, resolve_remote_id, save_meta, verify_readback


def test_meta_path_for_json_file():
    assert meta_path_for("workflows/foo.json") == __import__("pathlib").Path("workflows/foo.meta.json")


def test_load_meta_returns_none_when_absent(tmp_path):
    workflow_file = tmp_path / "foo.json"
    workflow_file.write_text("{}")
    assert load_meta(workflow_file) is None


def test_save_and_load_meta_roundtrip(tmp_path):
    workflow_file = tmp_path / "foo.json"
    workflow_file.write_text("{}")
    save_meta(workflow_file, {"remoteWorkflowId": "abc123", "environment": "production"})
    meta = load_meta(workflow_file)
    assert meta == {"remoteWorkflowId": "abc123", "environment": "production"}


def test_resolve_remote_id_returns_id_when_present(tmp_path):
    workflow_file = tmp_path / "foo.json"
    workflow_file.write_text("{}")
    save_meta(workflow_file, {"remoteWorkflowId": "abc123"})
    assert resolve_remote_id(workflow_file) == "abc123"


def test_resolve_remote_id_none_when_no_meta_file(tmp_path):
    workflow_file = tmp_path / "foo.json"
    workflow_file.write_text("{}")
    assert resolve_remote_id(workflow_file) is None


def test_resolve_remote_id_none_when_field_missing(tmp_path):
    workflow_file = tmp_path / "foo.json"
    workflow_file.write_text("{}")
    save_meta(workflow_file, {"environment": "production"})
    assert resolve_remote_id(workflow_file) is None


def test_load_meta_raises_on_invalid_json(tmp_path):
    workflow_file = tmp_path / "foo.json"
    workflow_file.write_text("{}")
    meta_path_for(workflow_file).write_text("not json")
    with pytest.raises(ValueError):
        load_meta(workflow_file)


def test_meta_never_sent_as_part_of_deploy_payload():
    # Sanity check on the contract: build_create_payload/build_update_payload
    # only ever look at the workflow dict passed to them, and meta.json is a
    # wholly separate file never merged into that dict anywhere in this codebase.
    from n8n_tool.normalizer import build_create_payload

    workflow = {"name": "X", "nodes": [], "connections": {}, "settings": {}}
    payload = build_create_payload(workflow)
    assert "remoteWorkflowId" not in payload
    assert "environment" not in payload


def test_verify_readback_ok_when_equivalent():
    expected = {"name": "X", "nodes": [], "connections": {}, "settings": {}}
    actual = {"id": "1", "name": "X", "nodes": [], "connections": {}, "settings": {}, "active": False}
    diff = verify_readback(expected, actual)
    assert not diff.has_changes


def test_verify_readback_fails_when_different():
    expected = {"name": "X", "nodes": [], "connections": {}, "settings": {}}
    actual = {"name": "Y", "nodes": [], "connections": {}, "settings": {}}
    diff = verify_readback(expected, actual)
    assert diff.has_changes
