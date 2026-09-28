from n8n_tool.normalizer import (
    build_create_payload,
    build_update_payload,
    count_connections,
    count_nodes,
    find_extra_top_level_fields,
)


SAMPLE_WORKFLOW = {
    "id": "abc123",
    "name": "Test Workflow",
    "active": True,
    "createdAt": "2026-01-01T00:00:00Z",
    "updatedAt": "2026-01-02T00:00:00Z",
    "isArchived": False,
    "versionId": "v1",
    "triggerCount": 1,
    "meta": {"instanceId": "xyz"},
    "tags": [{"id": "1", "name": "prod"}],
    "shared": [{"role": "owner"}],
    "nodes": [
        {
            "id": "n1",
            "name": "Manual Trigger",
            "type": "n8n-nodes-base.manualTrigger",
            "typeVersion": 1,
            "position": [0, 0],
            "parameters": {},
            "createdAt": "2026-01-01T00:00:00Z",
            "updatedAt": "2026-01-01T00:00:00Z",
        },
        {
            "id": "n2",
            "name": "Set",
            "type": "n8n-nodes-base.set",
            "typeVersion": 3,
            "position": [200, 0],
            "parameters": {"mode": "manual"},
        },
    ],
    "connections": {
        "Manual Trigger": {"main": [[{"node": "Set", "type": "main", "index": 0}]]}
    },
    "settings": {"executionOrder": "v1"},
    "staticData": {"lastId": 1},
    "pinData": {},
}


def test_build_create_payload_only_has_writable_fields():
    payload = build_create_payload(SAMPLE_WORKFLOW)
    assert set(payload.keys()) <= {"name", "nodes", "connections", "settings", "staticData", "pinData"}
    assert payload["name"] == "Test Workflow"
    assert "id" not in payload
    assert "active" not in payload
    assert "createdAt" not in payload
    assert "versionId" not in payload
    assert "tags" not in payload


def test_build_create_payload_strips_node_read_only_fields():
    payload = build_create_payload(SAMPLE_WORKFLOW)
    node = payload["nodes"][0]
    assert "createdAt" not in node
    assert "updatedAt" not in node
    assert node["id"] == "n1"
    assert node["name"] == "Manual Trigger"


def test_build_create_payload_defaults_missing_settings_to_empty_dict():
    workflow = {"name": "X", "nodes": [], "connections": {}}
    payload = build_create_payload(workflow)
    assert payload["settings"] == {}


def test_build_update_payload_includes_description_when_present():
    workflow = dict(SAMPLE_WORKFLOW, description="A description")
    payload = build_update_payload(workflow)
    assert payload["description"] == "A description"
    assert "projectId" not in payload


def test_build_update_payload_omits_description_when_absent():
    payload = build_update_payload(SAMPLE_WORKFLOW)
    assert "description" not in payload


def test_count_nodes():
    assert count_nodes(SAMPLE_WORKFLOW) == 2
    assert count_nodes({"nodes": "not-a-list"}) == 0
    assert count_nodes({}) == 0


def test_count_connections():
    assert count_connections(SAMPLE_WORKFLOW) == 1
    assert count_connections({}) == 0


def test_count_connections_multiple_edges():
    connections = {
        "A": {"main": [[{"node": "B"}, {"node": "C"}]]},
        "B": {"main": [[{"node": "D"}], []]},
    }
    assert count_connections({"connections": connections}) == 3


def test_find_extra_top_level_fields():
    extras = find_extra_top_level_fields(SAMPLE_WORKFLOW)
    assert "id" in extras
    assert "active" in extras
    assert "versionId" in extras
    assert "name" not in extras
    assert "nodes" not in extras


def test_find_extra_top_level_fields_empty_for_clean_local_file():
    workflow = {"name": "X", "nodes": [], "connections": {}, "settings": {}}
    assert find_extra_top_level_fields(workflow) == []
