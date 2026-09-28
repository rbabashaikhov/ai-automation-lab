from n8n_tool.sanitizer import (
    REDACTED,
    find_sensitive_paths,
    sanitize_workflow,
    scan_value_for_secret_patterns,
    scan_workflow_for_secrets,
)


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


def test_scan_value_detects_openai_key():
    assert "OpenAI API key" in scan_value_for_secret_patterns("sk-abcdEFGH12345678901234")


def test_scan_value_detects_aws_access_key():
    assert "AWS access key ID" in scan_value_for_secret_patterns("AKIAABCDEFGHIJKLMNOP")


def test_scan_value_detects_jwt_supabase_style_key():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    assert "JWT / Supabase-style service key" in scan_value_for_secret_patterns(jwt)


def test_scan_value_detects_bearer_token():
    assert "Bearer token" in scan_value_for_secret_patterns("Authorization: Bearer abcdef0123456789")


def test_scan_value_detects_url_embedded_credentials():
    hits = scan_value_for_secret_patterns("postgres://myuser:hunter2@db.example.com:5432/mydb")
    assert "credentials embedded in a URL" in hits


def test_scan_value_no_false_positive_on_plain_text():
    assert scan_value_for_secret_patterns("https://example.com/products?page=1") == []


def test_scan_workflow_for_secrets_finds_key_and_value_hits():
    workflow = {
        "nodes": [
            {
                "name": "HTTP Request",
                "parameters": {
                    "apiKey": "irrelevant-because-key-name-already-flags-it",
                    "url": "https://api.example.com/v1?token=sk-liveabcdEFGH123456789012",
                },
            }
        ]
    }
    findings = scan_workflow_for_secrets(workflow)
    paths = {f.path for f in findings}
    assert "nodes[0].parameters.apiKey" in paths
    assert "nodes[0].parameters.url" in paths


def test_scan_workflow_for_secrets_ignores_credential_id_name_refs():
    workflow = {"nodes": [{"credentials": {"postgres": {"id": "42", "name": "Prod Postgres"}}}]}
    assert scan_workflow_for_secrets(workflow) == []


def test_scan_workflow_for_secrets_does_not_expose_secret_text():
    workflow = {"settings": {"password": "hunter2-super-secret"}}
    findings = scan_workflow_for_secrets(workflow)
    assert len(findings) == 1
    assert "hunter2-super-secret" not in findings[0].reason
    assert "hunter2-super-secret" not in findings[0].path
