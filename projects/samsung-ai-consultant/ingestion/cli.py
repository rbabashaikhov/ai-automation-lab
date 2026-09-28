"""CLI for the Samsung product ingestion pipeline.

    python -m ingestion discover --max-pages 1
    python -m ingestion fetch-product <url>
    python -m ingestion run --max-pages 1 --limit-products 3 --dry-run
    python -m ingestion run
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import sys

from .config import ConfigError, load_config
from .catalog import discover_catalog
from .http import FetchError, HttpClient
from .product import ExtractedProduct, fetch_product
from .repository import IngestionRunRepository, ProductRepository, connect
from .service import ProductOutcome, RunSummary, run_ingestion


def _build_http_client(config) -> HttpClient:
    return HttpClient(
        user_agent=config.user_agent,
        timeout_seconds=config.request_timeout_seconds,
        max_retries=config.max_retries,
        backoff_base_seconds=config.backoff_base_seconds,
        delay_seconds=config.delay_seconds,
    )


def _product_to_dict(product: ExtractedProduct) -> dict:
    d = dataclasses.asdict(product)
    d.pop("raw_payload", None)  # large; omit from CLI summaries
    return d


def _cmd_discover(args: argparse.Namespace) -> int:
    config = load_config(require_database=False)
    if args.delay is not None:
        config = dataclasses.replace(config, delay_seconds=args.delay)
    http_client = _build_http_client(config)

    entries, pages = discover_catalog(http_client, config.catalog_url, max_pages=args.max_pages)
    print(f"Pages visited: {len(pages)}")
    for page in pages:
        print(
            f"  {page.url} -> current_page={page.current_page} "
            f"pages_count={page.pages_count} entries={len(page.entries)}"
        )
    print(f"Total unique products discovered: {len(entries)}")
    for entry in entries[:20]:
        print(f"  {entry.external_id}\t{entry.mpn_code}\t{entry.name}\t{entry.product_url}")
    if len(entries) > 20:
        print(f"  ... and {len(entries) - 20} more")
    return 0


def _cmd_fetch_product(args: argparse.Namespace) -> int:
    config = load_config(require_database=False)
    if args.delay is not None:
        config = dataclasses.replace(config, delay_seconds=args.delay)
    http_client = _build_http_client(config)

    try:
        product = fetch_product(http_client, source=config.source, product_url=args.url)
    except FetchError as exc:
        print(f"FETCH FAILED: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(_product_to_dict(product), ensure_ascii=False, indent=2, default=str))
    return 0


def _print_run_summary(summary: RunSummary) -> None:
    print(f"dry_run={summary.dry_run} run_id={summary.run_id} status={summary.status}")
    print(f"pages_visited={len(summary.pages)}")
    print(
        f"discovered={summary.products_discovered} inserted={summary.products_inserted} "
        f"updated={summary.products_updated} failed={summary.products_failed}"
    )
    for outcome in summary.outcomes:
        line = f"  [{outcome.status}] {outcome.external_id}\t{outcome.product_url}"
        if outcome.error:
            line += f"\t{outcome.error}"
        print(line)


def _cmd_run(args: argparse.Namespace) -> int:
    config = load_config(require_database=not args.dry_run)
    if args.delay is not None:
        config = dataclasses.replace(config, delay_seconds=args.delay)
    http_client = _build_http_client(config)

    product_repo = None
    run_repo = None
    conn = None
    if not args.dry_run:
        try:
            conn = connect(config.database_url)
        except Exception as exc:  # noqa: BLE001
            print(f"DATABASE CONNECTION FAILED: {exc}", file=sys.stderr)
            return 1
        product_repo = ProductRepository(conn)
        run_repo = IngestionRunRepository(conn)

    try:
        summary = run_ingestion(
            http_client,
            source=config.source,
            source_url=config.catalog_url,
            max_pages=args.max_pages,
            limit_products=args.limit_products,
            dry_run=args.dry_run,
            product_repo=product_repo,
            run_repo=run_repo,
        )
    finally:
        if conn is not None:
            conn.close()

    _print_run_summary(summary)
    return 0 if summary.status in ("success", "partial") else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m ingestion")
    subparsers = parser.add_subparsers(dest="command", required=True)

    discover_p = subparsers.add_parser("discover", help="Discover catalog products/pagination")
    discover_p.add_argument("--max-pages", type=int, default=None)
    discover_p.add_argument("--delay", type=float, default=None, help="Override polite delay (seconds)")
    discover_p.set_defaults(func=_cmd_discover)

    fetch_p = subparsers.add_parser("fetch-product", help="Fetch and extract one product page")
    fetch_p.add_argument("url")
    fetch_p.add_argument("--delay", type=float, default=None)
    fetch_p.set_defaults(func=_cmd_fetch_product)

    run_p = subparsers.add_parser("run", help="Run catalog discovery + product ingestion")
    run_p.add_argument("--max-pages", type=int, default=None)
    run_p.add_argument("--limit-products", type=int, default=None)
    run_p.add_argument("--dry-run", action="store_true")
    run_p.add_argument("--delay", type=float, default=None)
    run_p.set_defaults(func=_cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
