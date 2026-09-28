from n8n_tool.validator import validate_workflow


def make_workflow(**overrides):
    workflow = {
        "name": "Test Workflow",
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
                "parameters": {},
            },
        ],
        "connections": {"Manual Trigger": {"main": [[{"node": "Set", "type": "main", "index": 0}]]}},
        "settings": {},
    }
    workflow.update(overrides)
    return workflow


def test_valid_workflow_passes():
    result = validate_workflow(make_workflow())
    assert result.ok
    assert result.errors == []
    assert result.node_count == 2
    assert result.connection_count == 1


def test_missing_name_is_error():
    workflow = make_workflow(name="")
    result = validate_workflow(workflow)
    assert not result.ok
    assert any("name" in e for e in result.errors)


def test_nodes_not_a_list_is_error():
    workflow = make_workflow(nodes="not-a-list")
    result = validate_workflow(workflow)
    assert any("'nodes' must be an array" in e for e in result.errors)


def test_connections_not_a_dict_is_error():
    workflow = make_workflow(connections=[])
    result = validate_workflow(workflow)
    assert any("'connections' must be an object" in e for e in result.errors)


def test_duplicate_node_ids_detected():
    workflow = make_workflow()
    workflow["nodes"][1]["id"] = workflow["nodes"][0]["id"]
    result = validate_workflow(workflow)
    assert any("duplicate node id" in e for e in result.errors)


def test_duplicate_node_names_detected():
    workflow = make_workflow()
    workflow["nodes"][1]["name"] = workflow["nodes"][0]["name"]
    result = validate_workflow(workflow)
    assert any("duplicate node name" in e for e in result.errors)


def test_connection_references_missing_node():
    workflow = make_workflow()
    workflow["connections"]["Manual Trigger"]["main"][0][0]["node"] = "Save Product"
    result = validate_workflow(workflow)
    assert any('missing node "Save Product"' in e for e in result.errors)


def test_connection_source_references_missing_node():
    workflow = make_workflow()
    workflow["connections"]["Nonexistent Node"] = {"main": []}
    result = validate_workflow(workflow)
    assert any('missing node "Nonexistent Node"' in e for e in result.errors)


def test_node_missing_type_is_error():
    workflow = make_workflow()
    del workflow["nodes"][0]["type"]
    result = validate_workflow(workflow)
    assert any("missing 'type'" in e for e in result.errors)


def test_node_missing_position_is_error():
    workflow = make_workflow()
    del workflow["nodes"][0]["position"]
    result = validate_workflow(workflow)
    assert any("missing 'position'" in e for e in result.errors)


def test_node_missing_id_is_error():
    workflow = make_workflow()
    del workflow["nodes"][0]["id"]
    result = validate_workflow(workflow)
    assert any("missing 'id'" in e for e in result.errors)


def test_node_missing_type_version_is_warning_not_error():
    workflow = make_workflow()
    del workflow["nodes"][0]["typeVersion"]
    result = validate_workflow(workflow)
    assert result.ok
    assert any("typeVersion" in w for w in result.warnings)


def test_missing_settings_is_warning_not_error():
    workflow = make_workflow()
    del workflow["settings"]
    result = validate_workflow(workflow)
    assert result.ok
    assert any("settings" in w for w in result.warnings)


def test_embedded_secret_blocks_validation():
    workflow = make_workflow()
    workflow["nodes"][0]["parameters"]["apiKey"] = "some-key-value"
    result = validate_workflow(workflow)
    assert not result.ok
    assert result.secret_findings
    assert "nodes[0]" in result.secret_findings[0].path


def test_credential_id_name_refs_do_not_block_validation():
    workflow = make_workflow()
    workflow["nodes"][0]["credentials"] = {"httpBasicAuth": {"id": "12", "name": "My Cred"}}
    result = validate_workflow(workflow)
    assert result.ok


def test_extra_read_only_fields_produce_warning():
    workflow = make_workflow(id="abc", active=True)
    result = validate_workflow(workflow)
    assert result.ok
    assert any("server-controlled" in w for w in result.warnings)


def test_non_dict_workflow_is_error():
    result = validate_workflow(["not", "a", "dict"])
    assert not result.ok
    assert result.errors
