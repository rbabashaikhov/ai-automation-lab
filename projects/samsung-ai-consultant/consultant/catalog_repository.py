"""Read-only typed access to the catalog. The only module that builds SQL.

Safety rules (Phase 4A §9):

* Identifiers (columns, sort/group expressions) come only from the closed mappings below,
  keyed by typed enums / ``Filters`` fields. Anything else raises ``TypeError``/``KeyError``.
* Every value is a ``%s`` parameter.
* Result sizes are hard-capped (:data:`MAX_LIST_LIMIT`, :data:`MAX_CANDIDATES`).
* Availability / recommendation-scope *defaults* are decided by ``planning``; this module only
  applies the filters it is given.
* The repository never writes; :func:`open_readonly_connection` additionally makes the session
  read-only with a statement timeout.
"""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from .schemas import (
    RESOLUTION_VALUES, Filters, GroupKey, ModelRef, ModelResolution, ProductKind, ProductRow,
    RefKind, SortDir, SortKey, SpecRow,
)
from .vocabulary import CatalogVocabulary, build_vocabulary, nearest_codes, normalize_code

# The one canonical effective-price rule (Phase 3D): sale price when present, else list price.
EFFECTIVE_PRICE_SQL = "COALESCE(p.sale_price, p.price)"

# Store product type from the catalog name's first word (73 'Телевизор', 2 'Дисплей').
# starts_with() rather than LIKE: no '%' to collide with DB-API parameter markers.
PRODUCT_KIND_SQL = ("CASE WHEN starts_with(p.name, 'Дисплей') THEN 'display' "
                    "WHEN starts_with(p.name, 'Телевизор') THEN 'tv' ELSE 'other' END")

MAX_LIST_LIMIT = 50
MAX_CANDIDATES = 200
STATEMENT_TIMEOUT_MS = 5000

_RANGE_COLUMNS = {
    "screen_size_inches": "p.screen_size_inches",
    "effective_price": EFFECTIVE_PRICE_SQL,
    "list_price": "p.price",
    "refresh_rate_hz": "p.refresh_rate_hz",
}
_SET_COLUMNS = {
    "panel_technology": "p.panel_technology",
    "category": "p.category",
    "product_ids": "p.id",
}
SORT_EXPR = {
    SortKey.EFFECTIVE_PRICE: EFFECTIVE_PRICE_SQL,
    SortKey.LIST_PRICE: "p.price",
    SortKey.SCREEN_SIZE: "p.screen_size_inches",
    SortKey.REFRESH_RATE: "p.refresh_rate_hz",
}
_DIR = {SortDir.ASC: "ASC", SortDir.DESC: "DESC"}
_GROUP_EXPR = {
    GroupKey.PANEL_TECHNOLOGY: "p.panel_technology",
    GroupKey.CATEGORY: "p.category",
    GroupKey.SCREEN_SIZE: "p.screen_size_inches",
    GroupKey.IS_AVAILABLE: "p.is_available",
    GroupKey.PRODUCT_KIND: PRODUCT_KIND_SQL,
}
_STABLE_ORDER = "p.source, p.external_id"

_PRODUCT_COLUMNS = (
    "p.id, p.source, p.external_id, p.model_code, p.name, p.category, p.series, "
    f"{PRODUCT_KIND_SQL}, p.year, p.screen_size_inches, p.resolution, p.panel_technology, "
    f"p.refresh_rate_hz, p.price, p.sale_price, {EFFECTIVE_PRICE_SQL}, p.currency, p.is_available, "
    "p.product_url"
)


def open_readonly_connection(dsn: str):
    """psycopg2 connection whose every transaction is read-only, with a statement timeout."""
    import psycopg2

    conn = psycopg2.connect(dsn, connect_timeout=8, options=(
        f"-c default_transaction_read_only=on -c statement_timeout={STATEMENT_TIMEOUT_MS}"))
    conn.set_session(readonly=True)
    return conn


def compile_filters(f: Filters) -> tuple:
    """``(where_sql, params)`` for a :class:`Filters`. Pure; unit-tested without a database."""
    if not isinstance(f, Filters):
        raise TypeError(f"expected Filters, got {type(f).__name__}")
    clauses, params = [], []
    for key, col in _SET_COLUMNS.items():
        values = getattr(f, key)
        if values:
            clauses.append(f"{col} = ANY(%s)")
            params.append(list(values))
    if f.resolution_class:
        resolutions = [v for rc in f.resolution_class for v in RESOLUTION_VALUES[rc]]
        clauses.append("p.resolution = ANY(%s)")
        params.append(resolutions)
    for key, col in _RANGE_COLUMNS.items():
        r = getattr(f, key)
        if r is None:
            continue
        if r.min is not None:
            clauses.append(f"{col} >= %s")
            params.append(r.min)
        if r.max is not None:
            clauses.append(f"{col} <= %s")
            params.append(r.max)
    if f.is_available is not None:
        clauses.append("p.is_available = %s")
        params.append(f.is_available)
    if f.exclude_product_kinds:
        kinds = [ProductKind(k).value for k in f.exclude_product_kinds]
        clauses.append(f"NOT ({PRODUCT_KIND_SQL} = ANY(%s))")
        params.append(kinds)
    return (" AND ".join(clauses) or "TRUE"), params


def _order_sql(key: SortKey, direction: SortDir) -> str:
    if not isinstance(key, SortKey) or not isinstance(direction, SortDir):
        raise TypeError("sort key/direction must be SortKey/SortDir enums")
    return f"{SORT_EXPR[key]} {_DIR[direction]} NULLS LAST, {_STABLE_ORDER}"


def _num(v):
    return None if v is None else float(v)


def _row(r) -> ProductRow:
    return ProductRow(
        id=r[0], source=r[1], external_id=r[2], model_code=r[3], name=r[4], category=r[5],
        series=r[6], product_kind=ProductKind(r[7]), year=r[8], screen_size_inches=_num(r[9]),
        resolution=r[10], panel_technology=r[11], refresh_rate_hz=r[12], price=_num(r[13]),
        sale_price=_num(r[14]), effective_price=_num(r[15]), currency=r[16], is_available=r[17],
        product_url=r[18],
    )


class SearchResult:
    def __init__(self, rows: list, total_count: int, limit: int):
        self.rows = rows
        self.total_count = total_count
        self.truncated = total_count > len(rows)
        self.limit = limit


class CatalogRepository:
    """Wraps a DB-API (psycopg2) connection. Callers own the connection's lifecycle."""

    def __init__(self, conn):
        self._conn = conn

    def _fetch(self, sql: str, params: Sequence = ()) -> list:
        with self._conn.cursor() as cur:
            cur.execute(sql, list(params))
            return cur.fetchall()

    # ---- vocabulary ------------------------------------------------------------------------

    def vocabulary(self) -> CatalogVocabulary:
        """Closed vocabularies only (codes, enums, sizes, families); no live prices/availability."""
        rows = self._fetch("SELECT p.model_code, p.series, p.panel_technology, p.category, p.resolution, "
                           "p.screen_size_inches, p.year FROM products p")
        return build_vocabulary(rows)

    def price_range(self, filters: Optional[Filters] = None) -> tuple:
        """Live (min, max) effective price -- deliberately not part of the cached vocabulary."""
        where, params = compile_filters(filters or Filters())
        r = self._fetch(f"SELECT MIN({EFFECTIVE_PRICE_SQL}), MAX({EFFECTIVE_PRICE_SQL}) FROM products p "
                        f"WHERE {where}", params)[0]
        return _num(r[0]), _num(r[1])

    # ---- products ----------------------------------------------------------------------------

    def get_products(self, ids: Iterable[int]) -> list:
        ids = list(ids)
        if not ids:
            return []
        rows = {r[0]: _row(r) for r in self._fetch(
            f"SELECT {_PRODUCT_COLUMNS} FROM products p WHERE p.id = ANY(%s)", [ids])}
        return [rows[i] for i in ids if i in rows]

    def get_products_by_codes(self, codes: Iterable[str]) -> list:
        codes = list(codes)
        if not codes:
            return []
        return [_row(r) for r in self._fetch(
            f"SELECT {_PRODUCT_COLUMNS} FROM products p WHERE p.model_code = ANY(%s) "
            f"ORDER BY p.screen_size_inches, {_STABLE_ORDER}", [codes])]

    def search(self, filters: Filters, sort: Optional[tuple] = None,
               limit: int = 20, cap: int = MAX_LIST_LIMIT) -> SearchResult:
        """Filtered rows (``sort`` = ``(SortKey, SortDir)``; default effective price asc).
        ``limit`` is clamped to ``cap`` (itself at most :data:`MAX_CANDIDATES`)."""
        cap = min(cap, MAX_CANDIDATES)
        limit = max(1, min(int(limit), cap))
        where, params = compile_filters(filters)
        key, direction = sort or (SortKey.EFFECTIVE_PRICE, SortDir.ASC)
        rows = self._fetch(
            f"SELECT {_PRODUCT_COLUMNS}, COUNT(*) OVER () FROM products p WHERE {where} "
            f"ORDER BY {_order_sql(key, direction)} LIMIT %s", [*params, limit])
        total = rows[0][-1] if rows else 0
        return SearchResult([_row(r[:-1]) for r in rows], int(total), limit)

    def candidates(self, filters: Filters) -> SearchResult:
        """Constraint-first candidate set (bounded by :data:`MAX_CANDIDATES`)."""
        return self.search(filters, limit=MAX_CANDIDATES, cap=MAX_CANDIDATES)

    def extreme(self, filters: Filters, key: SortKey, direction: SortDir) -> list:
        """Tie-aware superlative: every row whose ``key`` equals the MIN/MAX within ``filters``."""
        if not isinstance(key, SortKey) or not isinstance(direction, SortDir):
            raise TypeError("key/direction must be SortKey/SortDir enums")
        where, params = compile_filters(filters)
        expr = SORT_EXPR[key]
        agg = "MIN" if direction is SortDir.ASC else "MAX"
        rows = self._fetch(
            f"SELECT {_PRODUCT_COLUMNS} FROM products p WHERE {where} AND {expr} = "
            f"(SELECT {agg}({expr}) FROM products p WHERE {where}) "
            f"ORDER BY {_STABLE_ORDER} LIMIT %s", [*params, *params, MAX_LIST_LIMIT])
        return [_row(r) for r in rows]

    def count(self, filters: Filters, group_by: Optional[GroupKey] = None) -> list:
        """``[(group_value, count)]``; ``[(None, total)]`` without ``group_by``."""
        where, params = compile_filters(filters)
        if group_by is None:
            return [(None, self._fetch(f"SELECT COUNT(*) FROM products p WHERE {where}", params)[0][0])]
        if not isinstance(group_by, GroupKey):
            raise TypeError("group_by must be a GroupKey enum")
        expr = _GROUP_EXPR[group_by]
        rows = self._fetch(f"SELECT {expr} AS g, COUNT(*) FROM products p WHERE {where} "
                           f"GROUP BY g ORDER BY g NULLS LAST", params)
        return [(_num(g) if group_by is GroupKey.SCREEN_SIZE else g, n) for g, n in rows]

    def get_specs(self, product_ids: Iterable[int], spec_names: Optional[Sequence[str]] = None) -> dict:
        """``{product_id: [SpecRow]}`` in catalog order; ``spec_names`` restricts the rows."""
        ids = list(product_ids)
        if not ids:
            return {}
        sql = ("SELECT s.product_id, s.spec_group, s.spec_name, s.spec_key, s.spec_value "
               "FROM product_specs s WHERE s.product_id = ANY(%s)")
        params: list = [ids]
        if spec_names is not None:
            sql += " AND s.spec_name = ANY(%s)"
            params.append(list(spec_names))
        out: dict = {i: [] for i in ids}
        for pid, group, name, key, value in self._fetch(sql + " ORDER BY s.product_id, s.sort_order, s.id",
                                                        params):
            out[pid].append(SpecRow(pid, group, name, key, value))
        return out

    # ---- model / family resolution -----------------------------------------------------------

    def resolve_model_refs(self, refs: Sequence[ModelRef],
                           vocabulary: Optional[CatalogVocabulary] = None) -> list:
        vocab = vocabulary or self.vocabulary()
        out = []
        for ref in refs:
            if not isinstance(ref, ModelRef):
                raise TypeError("refs must be ModelRef")
            if ref.kind is RefKind.FULL:
                code = vocab.code_for(ref.text)
                if code is None:
                    out.append(ModelResolution(ref, "not_found", basis="model_code",
                                               suggestions=nearest_codes(normalize_code(ref.text), vocab.model_codes)))
                    continue
                rows = self.get_products_by_codes([code])
                out.append(ModelResolution(ref, "exact", tuple(rows), "model_code",
                                           tuple(sorted({r.screen_size_inches for r in rows}))))
            else:
                codes, basis = vocab.family_codes(ref.text)
                if not codes:
                    out.append(ModelResolution(ref, "not_found", basis=basis))
                    continue
                rows = self.get_products_by_codes(codes)
                out.append(ModelResolution(ref, "family", tuple(rows), basis,
                                           tuple(sorted({r.screen_size_inches for r in rows
                                                         if r.screen_size_inches is not None}))))
        return out
