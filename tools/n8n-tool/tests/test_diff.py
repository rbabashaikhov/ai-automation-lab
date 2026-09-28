from n8n_tool.diff import diff_workflows, normalize_for_diff


def make_workflow(**overrides):
    workflow = {
        "id": "abc",
        "name": "Test",
        "active": False,
        "createdAt": "2026-01-01T00:00:00Z",
        "updatedAt": "2026-01-01T00:00:00Z",
        "versionId": "v1",
        "nodes": [
            {"id": "n1", "name": "A", "type": "t", "typeVersion": 1, "position": [0, 0], "parameters": {}},
        ],
        "connections": {},
        "settings": {},
    }
    workflow.update(overrides)
    return workflow


def test_identical_workflows_have_no_diff():
    old = make_workflow()
    new = make_workflow()
    diff = diff_workflows(old, new)
    assert not diff.has_changes


def test_server_noise_ignored():
    old = make_workflow(updatedAt="2026-01-01T00:00:00Z", versionId="v1")
    new = make_workflow(updatedAt="2026-02-02T00:00:00Z", versionId="v2", active=True)
    diff = diff_workflows(old, new)
    assert not diff.has_changes


def test_name_change_detected():
    old = make_workflow(name="Old Name")
    new = make_workflow(name="New Name")
    diff = diff_workflows(old, new)
    assert diff.has_changes
    paths = [c.path for c in diff.changes]
    assert "name" in paths


def test_node_parameter_change_detected():
    old = make_workflow()
    new = make_workflow()
    new["nodes"][0]["parameters"] = {"value": 1}
    diff = diff_workflows(old, new)
    assert diff.has_changes
    assert any("nodes.A.parameters" in c.path for c in diff.changes)


def test_added_node_detected():
    old = make_workflow()
    new = make_workflow()
    new["nodes"].append(
        {"id": "n2", "name": "B", "type": "t", "typeVersion": 1, "position": [100, 0], "parameters": {}}
    )
    diff = diff_workflows(old, new)
    assert diff.has_changes
    assert diff.new_node_count == 2
    assert diff.old_node_count == 1
    added = [c for c in diff.changes if c.path == "nodes.B"]
    assert len(added) == 1
    assert added[0].to_dict()["added"] is True


def test_removed_node_detected():
    old = make_workflow()
    old["nodes"].append(
        {"id": "n2", "name": "B", "type": "t", "typeVersion": 1, "position": [100, 0], "parameters": {}}
    )
    new = make_workflow()
    diff = diff_workflows(old, new)
    removed = [c for c in diff.changes if c.path == "nodes.B"]
    assert len(removed) == 1
    assert removed[0].to_dict()["removed"] is True


def test_node_reorder_alone_is_not_a_change():
    old = make_workflow()
    old["nodes"].append(
        {"id": "n2", "name": "B", "type": "t", "typeVersion": 1, "position": [100, 0], "parameters": {}}
    )
    new = make_workflow()
    new["nodes"] = list(reversed(old["nodes"]))
    diff = diff_workflows(old, new)
    assert not diff.has_changes


def test_connection_change_detected():
    old = make_workflow(connections={})
    new = make_workflow(connections={"A": {"main": [[{"node": "A", "type": "main", "index": 0}]]}})
    diff = diff_workflows(old, new)
    assert diff.has_changes
    assert diff.old_connection_count == 0
    assert diff.new_connection_count == 1


def test_change_to_dict_serializes_missing_as_none_with_flag():
    old = make_workflow()
    new = make_workflow()
    new["nodes"].append(
        {"id": "n2", "name": "B", "type": "t", "typeVersion": 1, "position": [100, 0], "parameters": {}}
    )
    diff = diff_workflows(old, new)
    change = next(c for c in diff.changes if c.path == "nodes.B")
    as_dict = change.to_dict()
    assert as_dict["old"] is None
    assert as_dict["added"] is True


def test_normalize_for_diff_defaults_are_consistent_for_absent_fields():
    minimal = {"name": "X", "nodes": [], "connections": {}}
    full = {"name": "X", "nodes": [], "connections": {}, "settings": {}, "staticData": None, "pinData": {}}
    assert normalize_for_diff(minimal) == normalize_for_diff(full)
