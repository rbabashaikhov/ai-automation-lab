"""Command-line interface for n8n_tool.

Phase 1 scope: read-only operations (doctor, workflows list/find/get/export).

Phase 2 adds a small, explicit set of write operations (create/update/
deploy), always gated by local validation, secret scanning, a diff/plan the
caller can inspect (--dry-run), and an explicit confirmation (or --yes).
There is deliberately no delete, no activate/deactivate, and no credential
or execution write path anywhere in this module.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .backup import BackupError, backup_workflow
from .client import N8nApiError, N8nAuthError, N8nClient, N8nConnectionError
from .config import PROTECTED_WORKFLOW_IDS, Config, ConfigError, load_config
from .deploy import load_meta, resolve_remote_id, save_meta
from .diff import DiffResult, diff_workflows
from .normalizer import build_create_payload, build_update_payload, count_connections, count_nodes
from .sanitizer import find_sensitive_paths, sanitize_workflow
from .validator import ValidationResult, validate_workflow
from .workflows import find_workflows_by_name, list_workflows, slugify, summarize

logger = logging.getLogger("n8n_tool")


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s %(message)s", stream=sys.stderr)


def _build_client(config: Config) -> N8nClient:
    return N8nClient(base_url=config.base_url, api_key=config.api_key)


def _print_workflow_row(workflow: dict[str, Any]) -> None:
    summary = summarize(workflow)
    print(
        f"{summary['id']}\t{summary['name']}\t{summary['active']}\t{summary['updatedAt']}"
    )


# ---------------------------------------------------------------- Phase 1 --


def cmd_doctor(args: argparse.Namespace) -> int:
    print("n8n Tool Doctor\n")

    try:
        config = load_config(args.env_file)
    except ConfigError as exc:
        print(f"Configuration: FAILED ({exc})")
        print("\nResult: unhealthy")
        return 1

    print(f"Base URL: {config.base_url}")
    logger.info("Connecting to n8n")

    client = _build_client(config)
    try:
        client.get("/api/v1/workflows", params={"limit": 1})
    except N8nConnectionError as exc:
        print(f"API connectivity: FAILED ({exc})")
        print("\nResult: unhealthy")
        return 1
    except N8nAuthError as exc:
        print("API connectivity: OK")
        print(f"Authentication: FAILED ({exc})")
        print("\nResult: unhealthy")
        return 1
    except N8nApiError as exc:
        print("API connectivity: OK")
        print("Authentication: OK")
        print(f"Workflows endpoint: FAILED ({exc})")
        print("\nResult: unhealthy")
        return 1

    print("API connectivity: OK")
    print("Authentication: OK")
    print("Workflows endpoint: OK")
    print("\nResult: healthy")
    return 0


def cmd_workflows_list(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    client = _build_client(config)
    logger.info("Fetching workflows")
    try:
        workflows = list_workflows(client)
    except N8nApiError as exc:
        logger.error(str(exc))
        return 1

    print("ID\tName\tActive\tUpdated At")
    for workflow in workflows:
        _print_workflow_row(workflow)
    return 0


def cmd_workflows_find(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    client = _build_client(config)
    logger.info("Searching workflows for name: %s", args.name)
    try:
        matches = find_workflows_by_name(client, args.name)
    except N8nApiError as exc:
        logger.error(str(exc))
        return 1

    if not matches:
        print(f"No workflow found matching: {args.name}")
        return 1

    if len(matches) > 1:
        print(f"Multiple workflows match '{args.name}':\n")
        print("ID\tName\tActive\tUpdated At")
        for workflow in matches:
            _print_workflow_row(workflow)
        print("\nRefine your search or use 'workflows get <id>' with a specific ID.")
        return 2

    workflow = matches[0]
    summary = summarize(workflow)
    print("Found workflow:\n")
    print(f"ID: {summary['id']}")
    print(f"Name: {summary['name']}")
    print(f"Active: {summary['active']}")
    print(f"Updated: {summary['updatedAt']}")
    return 0


def cmd_workflows_get(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    client = _build_client(config)
    logger.info("Fetching workflow %s", args.workflow_id)
    try:
        workflow = client.get_workflow(args.workflow_id)
    except N8nApiError as exc:
        logger.error(str(exc))
        return 1

    sanitized = sanitize_workflow(workflow)
    print(json.dumps(sanitized, indent=2, ensure_ascii=False))
    return 0


def cmd_workflows_export(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    client = _build_client(config)
    logger.info("Fetching workflow %s", args.workflow_id)
    try:
        workflow = client.get_workflow(args.workflow_id)
    except N8nApiError as exc:
        logger.error(str(exc))
        return 1

    name = str(workflow.get("name") or args.workflow_id)
    slug = slugify(name)
    out_dir = Path(args.output_dir) / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Exporting workflow %s", args.workflow_id)

    sensitive_paths = find_sensitive_paths(workflow)
    sanitized = sanitize_workflow(workflow)

    sanitized_path = out_dir / "sanitized.json"
    sanitized_path.write_text(
        json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    logger.info("Saved %s", sanitized_path)

    raw_path = out_dir / "raw.json"
    if sensitive_paths:
        logger.warning(
            "Raw API response appears to contain %d sensitive field(s) (%s); "
            "skipping raw.json, sanitized.json only",
            len(sensitive_paths),
            ", ".join(sensitive_paths[:5]) + ("..." if len(sensitive_paths) > 5 else ""),
        )
        if raw_path.exists():
            raw_path.unlink()
    else:
        raw_path.write_text(
            json.dumps(workflow, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        logger.info("Saved %s", raw_path)

    print(f"\nExported workflow '{name}' (id={workflow.get('id')}) to {out_dir}/")
    print(f"  sanitized.json: {'saved' if sanitized_path.exists() else 'not saved'}")
    print(f"  raw.json: {'saved' if raw_path.exists() else 'skipped (sensitive fields detected)'}")
    return 0


# ---------------------------------------------------------------- Phase 2 --


def _load_local_workflow(file_path: str) -> dict[str, Any]:
    path = Path(file_path)
    if not path.exists():
        raise ValueError(f"File not found: {file_path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{file_path} must contain a JSON object")
    return data


def _validation_to_dict(result: ValidationResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "errors": result.errors,
        "warnings": result.warnings,
        "secretFindings": [
            {"path": f.path, "reason": f.reason} for f in result.secret_findings
        ],
        "nodeCount": result.node_count,
        "connectionCount": result.connection_count,
    }


def _diff_to_dict(diff: DiffResult) -> dict[str, Any]:
    return {
        "hasChanges": diff.has_changes,
        "changes": [c.to_dict() for c in diff.changes],
        "oldNodeCount": diff.old_node_count,
        "newNodeCount": diff.new_node_count,
        "oldConnectionCount": diff.old_connection_count,
        "newConnectionCount": diff.new_connection_count,
    }


def _print_validation_result(result: ValidationResult) -> None:
    if result.errors or result.secret_findings:
        print("Workflow validation: FAILED\n")
        for err in result.errors:
            print(f"ERROR {err}")
        for finding in result.secret_findings:
            print(f"ERROR potential embedded secret at {finding.path}: {finding.reason}")
    else:
        print("Workflow validation: OK")
        print(f"Nodes: {result.node_count}")
        print(f"Connections: {result.connection_count}")
    for warning in result.warnings:
        print(f"WARNING {warning}")


def cmd_workflows_validate(args: argparse.Namespace) -> int:
    try:
        workflow = _load_local_workflow(args.file)
    except ValueError as exc:
        if args.json_output:
            print(json.dumps({"ok": False, "errors": [str(exc)]}, indent=2))
        else:
            print("Workflow validation: FAILED\n")
            print(f"ERROR {exc}")
        return 1

    result = validate_workflow(workflow)
    if args.json_output:
        print(json.dumps(_validation_to_dict(result), indent=2, ensure_ascii=False))
    else:
        _print_validation_result(result)
    return 0 if result.ok else 1


def cmd_workflows_diff(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    client = _build_client(config)
    try:
        local = _load_local_workflow(args.file)
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    logger.info("Fetching workflow %s", args.workflow_id)
    try:
        remote = client.get_workflow(args.workflow_id)
    except N8nApiError as exc:
        logger.error(str(exc))
        return 1

    diff = diff_workflows(remote, local)
    if args.json_output:
        print(json.dumps(_diff_to_dict(diff), indent=2, ensure_ascii=False))
        return 0

    print(f"Diff: remote {args.workflow_id} vs local {args.file}\n")
    if not diff.has_changes:
        print("No meaningful differences.")
    else:
        for line in diff.format_lines():
            print(line)
    print(f"\nNodes: {diff.old_node_count} -> {diff.new_node_count}")
    print(f"Connections: {diff.old_connection_count} -> {diff.new_connection_count}")
    return 0


def _confirm(assume_yes: bool) -> bool:
    if assume_yes:
        return True
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _run_create(
    client: N8nClient,
    file_path: str,
    dry_run: bool,
    assume_yes: bool,
    json_output: bool,
) -> tuple[int, dict[str, Any] | None]:
    """Returns (exit_code, created_workflow_or_None)."""
    try:
        local = _load_local_workflow(file_path)
    except ValueError as exc:
        logger.error(str(exc))
        return 1, None

    result = validate_workflow(local)
    if not json_output:
        print("Validation:", "OK" if not result.errors else "FAILED")
        if result.errors:
            for err in result.errors:
                print(f"  ERROR {err}")
        print("Secret scan:", "OK" if not result.secret_findings else "BLOCKED")
        if result.secret_findings:
            print("\nDEPLOY BLOCKED\n\nPotential embedded secret(s):")
            for finding in result.secret_findings:
                print(f"  {finding.path}: {finding.reason}")
    if result.errors or result.secret_findings:
        if json_output:
            print(json.dumps({"action": "create", "ok": False, "validation": _validation_to_dict(result)}, indent=2))
        return 1, None

    node_count = count_nodes(local)
    connection_count = count_connections(local)

    if dry_run:
        if json_output:
            print(json.dumps({
                "action": "create",
                "dryRun": True,
                "name": local.get("name"),
                "nodeCount": node_count,
                "connectionCount": connection_count,
            }, indent=2))
        else:
            print(f"\nAction: CREATE")
            print(f"Name: {local.get('name')}")
            print(f"Nodes: {node_count}")
            print(f"Connections: {connection_count}")
            print("No remote changes made")
        return 0, None

    if not json_output:
        print("\nAbout to CREATE workflow:\n")
        print(f"Name: {local.get('name')}")
        print(f"Local file: {file_path}")
        print(f"Nodes: {node_count}")
        print(f"Connections: {connection_count}\n")
        if not _confirm(assume_yes):
            print("Aborted (not confirmed).")
            return 1, None
    elif not assume_yes:
        logger.error("--json requires --yes for a real (non-dry-run) write")
        return 1, None

    payload = build_create_payload(local)
    try:
        created = client.create_workflow(payload)
    except N8nApiError as exc:
        logger.error(str(exc))
        return 1, None

    workflow_id = created.get("id")
    try:
        readback = client.get_workflow(workflow_id)
    except N8nApiError as exc:
        logger.error("Create succeeded but read-back failed: %s", exc)
        readback = created

    verify_diff = diff_workflows(local, readback)
    verification_ok = not verify_diff.has_changes

    if json_output:
        print(json.dumps({
            "action": "create",
            "ok": True,
            "id": workflow_id,
            "name": readback.get("name"),
            "active": readback.get("active"),
            "nodeCount": count_nodes(readback),
            "verification": {"ok": verification_ok, "changes": [c.to_dict() for c in verify_diff.changes]},
        }, indent=2, ensure_ascii=False))
    else:
        print("Workflow created\n")
        print(f"ID: {workflow_id}")
        print(f"Name: {readback.get('name')}")
        print(f"Active: {readback.get('active')}")
        print(f"Nodes: {count_nodes(readback)}")
        print(f"Verification: {'OK' if verification_ok else 'FAILED'}")
        if not verification_ok:
            print("\nRead-back differs from what was deployed:")
            for line in verify_diff.format_lines():
                print(f"  {line}")

    return (0 if verification_ok else 1), readback


def _run_update(
    client: N8nClient,
    workflow_id: str,
    file_path: str,
    dry_run: bool,
    assume_yes: bool,
    json_output: bool,
    allow_protected: bool,
    backup_dir: str = "backups",
) -> int:
    if workflow_id in PROTECTED_WORKFLOW_IDS and not allow_protected:
        logger.error(
            "Workflow %s is protected during Phase 2 (legacy asset). "
            "Pass --allow-protected to override.",
            workflow_id,
        )
        return 1

    try:
        local = _load_local_workflow(file_path)
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    result = validate_workflow(local)
    if not json_output:
        print("Validation:", "OK" if not result.errors else "FAILED")
        if result.errors:
            for err in result.errors:
                print(f"  ERROR {err}")
        print("Secret scan:", "OK" if not result.secret_findings else "BLOCKED")
        if result.secret_findings:
            print("\nDEPLOY BLOCKED\n\nPotential embedded secret(s):")
            for finding in result.secret_findings:
                print(f"  {finding.path}: {finding.reason}")
    if result.errors or result.secret_findings:
        if json_output:
            print(json.dumps({"action": "update", "ok": False, "validation": _validation_to_dict(result)}, indent=2))
        return 1

    try:
        remote = client.get_workflow(workflow_id)
    except N8nApiError as exc:
        logger.error(str(exc))
        return 1

    diff = diff_workflows(remote, local)

    if dry_run:
        if json_output:
            print(json.dumps({
                "action": "update",
                "dryRun": True,
                "remoteId": workflow_id,
                "remoteName": remote.get("name"),
                "diff": _diff_to_dict(diff),
            }, indent=2, ensure_ascii=False))
        else:
            print(f"\nRemote ID: {workflow_id}")
            print(f"Remote name: {remote.get('name')}")
            print(f"Local file: {file_path}")
            if not diff.has_changes:
                print("\nNo changes detected.")
            else:
                print(f"\nNodes: {diff.old_node_count} -> {diff.new_node_count}")
                print(f"Connections: {diff.old_connection_count} -> {diff.new_connection_count}")
            print("\nAction: UPDATE")
            print("No remote changes made")
        return 0

    if not diff.has_changes:
        if json_output:
            print(json.dumps({"action": "update", "ok": True, "noop": True}, indent=2))
        else:
            print("No changes detected.\nNothing deployed.")
        return 0

    if not json_output:
        print("\nAbout to UPDATE workflow:\n")
        print(f"Remote ID: {workflow_id}")
        print(f"Remote name: {remote.get('name')}")
        print(f"Local file: {file_path}\n")
        print("Nodes:")
        print(f"{diff.old_node_count} -> {diff.new_node_count}\n")
        print("Connections:")
        print(f"{diff.old_connection_count} -> {diff.new_connection_count}\n")
        print("Backup:")
        print("will be created\n")
        if not _confirm(assume_yes):
            print("Aborted (not confirmed).")
            return 1
    elif not assume_yes:
        logger.error("--json requires --yes for a real (non-dry-run) write")
        return 1

    try:
        backup_path = backup_workflow(client, workflow_id, backup_dir=backup_dir)
    except BackupError as exc:
        logger.error("Backup failed, update aborted: %s", exc)
        return 1

    payload = build_update_payload(local)
    try:
        client.update_workflow(workflow_id, payload)
    except N8nApiError as exc:
        logger.error(str(exc))
        print(f"\nRemote backup:\n{backup_path}")
        print("\nAutomatic rollback was NOT performed.")
        return 1

    try:
        readback = client.get_workflow(workflow_id)
    except N8nApiError as exc:
        logger.error("Update succeeded but read-back failed: %s", exc)
        return 1

    verify_diff = diff_workflows(local, readback)
    verification_ok = not verify_diff.has_changes

    if json_output:
        print(json.dumps({
            "action": "update",
            "ok": verification_ok,
            "id": workflow_id,
            "name": readback.get("name"),
            "active": readback.get("active"),
            "backupPath": str(backup_path),
            "verification": {"ok": verification_ok, "changes": [c.to_dict() for c in verify_diff.changes]},
        }, indent=2, ensure_ascii=False))
    else:
        print(f"\nID: {workflow_id}")
        print(f"Name: {readback.get('name')}")
        print(f"Active: {readback.get('active')}")
        print(f"Read-back verification: {'OK' if verification_ok else 'FAILED'}")
        if not verification_ok:
            print("\nVerification failed.")
            print(f"\nRemote backup:\n{backup_path}")
            print("\nAutomatic rollback was NOT performed.")
            print("\nDifferences:")
            for line in verify_diff.format_lines():
                print(f"  {line}")

    return 0 if verification_ok else 1


def cmd_workflows_create(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    client = _build_client(config)
    exit_code, _ = _run_create(client, args.file, args.dry_run, args.yes, args.json_output)
    return exit_code


def cmd_workflows_update(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    client = _build_client(config)
    return _run_update(
        client, args.workflow_id, args.file, args.dry_run, args.yes, args.json_output, args.allow_protected
    )


def cmd_workflows_deploy(args: argparse.Namespace) -> int:
    config = load_config(args.env_file)
    client = _build_client(config)

    try:
        remote_id = resolve_remote_id(args.file)
    except ValueError as exc:
        logger.error(str(exc))
        return 1

    if remote_id is None:
        if not args.json_output:
            print("No remoteWorkflowId on file — this will CREATE a new workflow.\n")
        exit_code, created = _run_create(client, args.file, args.dry_run, args.yes, args.json_output)
        if exit_code == 0 and not args.dry_run and created is not None:
            meta = load_meta(args.file) or {}
            meta["remoteWorkflowId"] = created.get("id")
            save_meta(args.file, meta)
        return exit_code

    if not args.json_output:
        print(f"remoteWorkflowId={remote_id} found — this will UPDATE the existing workflow.\n")
    return _run_update(
        client, remote_id, args.file, args.dry_run, args.yes, args.json_output, args.allow_protected
    )


# ---------------------------------------------------------------- parser ---


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="n8n_tool",
        description="CLI for safely inspecting, validating, and deploying n8n workflows via the public API.",
    )
    parser.add_argument(
        "--env-file", default=None, help="Path to a .env file (default: ./.env)"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="Check connectivity and authentication")
    doctor_parser.set_defaults(func=cmd_doctor)

    workflows_parser = subparsers.add_parser("workflows", help="Workflow operations")
    workflows_sub = workflows_parser.add_subparsers(dest="workflows_command", required=True)

    list_parser = workflows_sub.add_parser("list", help="List all workflows")
    list_parser.set_defaults(func=cmd_workflows_list)

    find_parser = workflows_sub.add_parser("find", help="Find a workflow by name")
    find_parser.add_argument("name", help="Workflow name to search for")
    find_parser.set_defaults(func=cmd_workflows_find)

    get_parser = workflows_sub.add_parser("get", help="Get a workflow by ID")
    get_parser.add_argument("workflow_id", help="Workflow ID")
    get_parser.set_defaults(func=cmd_workflows_get)

    export_parser = workflows_sub.add_parser("export", help="Export a workflow to local JSON")
    export_parser.add_argument("workflow_id", help="Workflow ID")
    export_parser.add_argument(
        "--output-dir", default="exports", help="Base directory for exports (default: exports)"
    )
    export_parser.set_defaults(func=cmd_workflows_export)

    validate_parser = workflows_sub.add_parser("validate", help="Validate a local workflow file")
    validate_parser.add_argument("file", help="Path to a local workflow JSON file")
    validate_parser.add_argument("--json", dest="json_output", action="store_true", help="Machine-readable output")
    validate_parser.set_defaults(func=cmd_workflows_validate)

    diff_parser = workflows_sub.add_parser("diff", help="Diff a remote workflow against a local file")
    diff_parser.add_argument("workflow_id", help="Remote workflow ID")
    diff_parser.add_argument("file", help="Path to a local workflow JSON file")
    diff_parser.add_argument("--json", dest="json_output", action="store_true", help="Machine-readable output")
    diff_parser.set_defaults(func=cmd_workflows_diff)

    create_parser = workflows_sub.add_parser("create", help="Create a new workflow from a local file")
    create_parser.add_argument("file", help="Path to a local workflow JSON file")
    create_parser.add_argument("--dry-run", action="store_true", help="Validate/plan only, no API writes")
    create_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    create_parser.add_argument("--json", dest="json_output", action="store_true", help="Machine-readable output")
    create_parser.set_defaults(func=cmd_workflows_create)

    update_parser = workflows_sub.add_parser("update", help="Update an existing workflow from a local file")
    update_parser.add_argument("workflow_id", help="Remote workflow ID")
    update_parser.add_argument("file", help="Path to a local workflow JSON file")
    update_parser.add_argument("--dry-run", action="store_true", help="Validate/plan only, no API writes")
    update_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    update_parser.add_argument("--json", dest="json_output", action="store_true", help="Machine-readable output")
    update_parser.add_argument(
        "--allow-protected", action="store_true", help="Allow updating a protected legacy workflow"
    )
    update_parser.set_defaults(func=cmd_workflows_update)

    deploy_parser = workflows_sub.add_parser(
        "deploy", help="Create or update a workflow, based on its .meta.json sidecar"
    )
    deploy_parser.add_argument("file", help="Path to a local workflow JSON file")
    deploy_parser.add_argument("--dry-run", action="store_true", help="Validate/plan only, no API writes")
    deploy_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")
    deploy_parser.add_argument("--json", dest="json_output", action="store_true", help="Machine-readable output")
    deploy_parser.add_argument(
        "--allow-protected", action="store_true", help="Allow updating a protected legacy workflow"
    )
    deploy_parser.set_defaults(func=cmd_workflows_deploy)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    try:
        return args.func(args)
    except ConfigError as exc:
        logger.error(str(exc))
        return 1
    except N8nConnectionError as exc:
        logger.error(str(exc))
        return 1
    except N8nAuthError as exc:
        logger.error(str(exc))
        return 1
    except N8nApiError as exc:
        logger.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
