"""CLI for the RAG document/chunk indexing pipeline (Phase 3A -- no embeddings).

    python -m indexing inspect
    python -m indexing build --product-id 13
    python -m indexing build --limit 3 --dry-run
    python -m indexing build
"""

from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_config
from .metadata import SECTION_ORDER, approx_token_count
from .repository import IndexingRepository, ProductReader, connect
from .service import IndexingOutcome, index_product, run_indexing


def _cmd_inspect(args: argparse.Namespace) -> int:
    config = load_config()
    conn = connect(config.database_url)
    reader = ProductReader(conn)

    ids = reader.all_product_ids()
    print(f"products: {len(ids)}")

    group_counts: dict[str, int] = {}
    spec_count_per_product: list[int] = []
    for pid in ids:
        specs = reader.get_specs(pid)
        spec_count_per_product.append(len(specs))
        for spec in specs:
            group_counts[spec.spec_group] = group_counts.get(spec.spec_group, 0) + 1

    print(f"total product_specs rows: {sum(spec_count_per_product)}")
    print(
        f"specs/product: min={min(spec_count_per_product)} "
        f"max={max(spec_count_per_product)} "
        f"avg={sum(spec_count_per_product)/len(spec_count_per_product):.1f}"
    )
    print("\nspec_group frequency:")
    for group, count in sorted(group_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {group}: {count}")

    conn.close()
    return 0


def _print_outcome(outcome: IndexingOutcome, *, verbose: bool = False) -> None:
    change = "CHANGED" if outcome.document_changed else "unchanged"
    persisted = "persisted" if outcome.persisted else "dry-run"
    line = (
        f"[{persisted}] product_id={outcome.product_id} model_code={outcome.model_code} "
        f"chunks={outcome.chunk_count} doc_hash={outcome.content_hash[:12]}... {change}"
    )
    if outcome.chunk_sync is not None:
        cs = outcome.chunk_sync
        line += (
            f" | chunks preserved={cs.preserved} invalidated={cs.invalidated} "
            f"inserted={cs.inserted} deleted={cs.deleted}"
        )
    print(line)
    if verbose:
        print(f"    sections: {outcome.chunk_sections}")


def _cmd_build(args: argparse.Namespace) -> int:
    config = load_config(require_database=True)
    conn = connect(config.database_url)
    reader = ProductReader(conn)
    repository = None if args.dry_run else IndexingRepository(conn)

    try:
        if args.product_id is not None:
            outcome = index_product(reader, args.product_id, dry_run=args.dry_run, repository=repository)
            _print_outcome(outcome, verbose=True)
            if args.render:
                product = reader.get_product(args.product_id)
                specs = reader.get_specs(args.product_id)
                from .builder import build_document
                from .chunker import build_chunks

                document = build_document(product, specs)
                chunks = build_chunks(product, specs)
                print("\n=== DOCUMENT ===")
                print(document.content)
                print(f"\n(content_hash={document.content_hash}, "
                      f"~{approx_token_count(document.content)} tokens)")
                for chunk in chunks:
                    print(f"\n=== CHUNK {chunk.chunk_index} [{chunk.section}] ===")
                    print(chunk.content)
                    print(
                        f"(content_hash={chunk.content_hash}, "
                        f"~{approx_token_count(chunk.content)} tokens)"
                    )
            outcomes = [outcome]
        else:
            outcomes = run_indexing(
                reader, limit=args.limit, dry_run=args.dry_run, repository=repository
            )
            for outcome in outcomes:
                _print_outcome(outcome)

        changed = sum(1 for o in outcomes if o.document_changed)
        inserted = sum(1 for o in outcomes if o.document_inserted)
        updated = sum(1 for o in outcomes if o.document_inserted is False)
        total_chunks = sum(o.chunk_count for o in outcomes)
        print(
            f"\nproducts={len(outcomes)} changed={changed} inserted={inserted} "
            f"updated={updated} total_chunks={total_chunks} dry_run={args.dry_run}"
        )
    finally:
        conn.close()

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m indexing")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_p = subparsers.add_parser("inspect", help="Corpus analysis over products/product_specs")
    inspect_p.set_defaults(func=_cmd_inspect)

    build_p = subparsers.add_parser("build", help="Build (and optionally persist) documents/chunks")
    build_p.add_argument("--product-id", type=int, default=None)
    build_p.add_argument("--limit", type=int, default=None)
    build_p.add_argument("--dry-run", action="store_true")
    build_p.add_argument(
        "--render", action="store_true", help="With --product-id, print the full document/chunk text"
    )
    build_p.set_defaults(func=_cmd_build)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
