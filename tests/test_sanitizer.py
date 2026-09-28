from n8n_tool.sanitizer import REDACTED, find_sensitive_paths, sanitize_workflow


def test_sanitize_redacts_known_sensitive_keys():
    workflow = {
        "id": "123",
        "name": "Parsing",
        "nodes": [
            {
                "name": "HTTP Request",
                "type": "n8n-nodes-base.httpRequest",
                "parameters": {
                    "url": "https://example.com",
                    "apiKey": "sk-live-abc123",
                    "headers": {
                        "Authorization": "Bearer xyz",
                        "Cookie": "session=abc",
                    },
                },
            }
        ],
        "settings": {"password": "hunter2", "token": "tok_abc"},
    }

    sanitized = sanitize_workflow(workflow)

    assert sanitized["nodes"][0]["parameters"]["apiKey"] == REDACTED
    assert sanitized["nodes"][0]["parameters"]["headers"]["Authorization"] == REDACTED
    assert sanitized["nodes"][0]["parameters"]["headers"]["Cookie"] == REDACTED
    assert sanitized["settings"]["password"] == REDACTED
    assert sanitized["settings"]["token"] == REDACTED

    # Non-sensitive fields untouched.
    assert sanitized["id"] == "123"
    assert sanitized["name"] == "Parsing"
    assert sanitized["nodes"][0]["parameters"]["url"] == "https://example.com"


def test_sanitize_preserves_credential_id_and_name_references():
    workflow = {
        "nodes": [
            {
                "name": "Postgres",
                "credentials": {
                    "postgres": {"id": "42", "name": "Prod Postgres"},
                },
            }
        ]
    }

    sanitized = sanitize_workflow(workflow)

    creds = sanitized["nodes"][0]["credentials"]["postgres"]
    assert creds["id"] == "42"
    assert creds["name"] == "Prod Postgres"


def test_sanitize_does_not_mutate_input():
    workflow = {"settings": {"apiKey": "secret"}}
    sanitize_workflow(workflow)
    assert workflow["settings"]["apiKey"] == "secret"


def test_sanitize_handles_lists_of_scalars_and_dicts():
    workflow = {
        "tags": ["a", "b"],
        "items": [{"secret": "shh"}, {"value": 1}],
    }
    sanitized = sanitize_workflow(workflow)
    assert sanitized["tags"] == ["a", "b"]
    assert sanitized["items"][0]["secret"] == REDACTED
    assert sanitized["items"][1]["value"] == 1


def test_find_sensitive_paths_reports_dotted_paths():
    workflow = {
        "settings": {"password": "x"},
        "nodes": [{"parameters": {"accessToken": "y"}}],
    }
    paths = find_sensitive_paths(workflow)
    assert "settings.password" in paths
    assert "nodes[0].parameters.accessToken" in paths


def test_find_sensitive_paths_ignores_safe_credential_refs():
    workflow = {"nodes": [{"credentials": {"postgres": {"id": "1", "name": "Prod"}}}]}
    assert find_sensitive_paths(workflow) == []


def test_find_sensitive_paths_empty_for_clean_workflow():
    workflow = {"id": "1", "name": "Parsing", "active": True, "nodes": []}
    assert find_sensitive_paths(workflow) == []
