"""Command-line interface for n8n_tool.

Phase 1 scope: read-only operations only (doctor, workflows list/find/get/export).
No command in this module ever issues a POST/PUT/PATCH/DELETE request.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .client import N8nApiError, N8nAuthError, N8nClient, N8nConnectionError
from .config import Config, ConfigError, load_config
from .sanitizer import find_sensitive_paths, sanitize_workflow
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="n8n_tool",
        description="Read-only CLI for inspecting and exporting n8n workflows via the public API.",
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
