"""Phase 4D Agent tool facade: five closed, typed domain tools over the Phase 4B/4C core.

The Agent (n8n) decides *which* domain operation it needs and supplies arguments; this module
decides *how* it is executed. Every tool:

* validates its arguments against the same closed JSON Schema that is published to the Agent
  (:data:`TOOL_SCHEMAS`; unknown fields, wrong types, bad enums and malformed model references
  are rejected -- never coerced), then applies semantic checks (min <= max, exact vs range);
* builds a ``QueryPlanDelta`` and runs the accepted pipeline unchanged:
  ``planning.resolve_plan -> router.route -> retrieval.execute -> evidence.build_evidence``
  (semantic tie-break OFF, no embedder: this runtime never generates embeddings);
* returns the compact Agent-facing projection of the ``EvidenceBundle``
  (``agent_payload.to_agent_payload``), never the bundle itself.

There is no SQL, WHERE clause, vector or ranking argument anywhere in the tool surface.
:class:`ConsultantTools` adds the per-turn tool-call cap enforced at the Python boundary.

Phase 4F.3 (MVP hardening), both additive: ``get_catalog_stats`` counts registry features and counts
inside one model family (exact aggregates from structured rows), and :func:`add_feature_evidence`
gives every asked-about feature an explicit three-state value on every returned product.
"""

from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import nullcontext
from dataclasses import replace
from typing import Any, Callable, Optional

from .agent_payload import (
    AGENT_CONTRACT_VERSION, NOT_LISTED_DETAIL, NOT_LISTED_FOR_ALL_NOTE, error_payload, feature_view, stats_payload,
    to_agent_payload,
)
from .evidence import build_evidence
from .features import FEATURES, USE_CASES, evaluate_features, spec_names_for
from .planning import resolve_plan
from .retrieval import execute
from .router import route
from .schemas import (
    GroupKey, PlanValidationError, QueryPlanDelta, ResolutionClass, SortDir, SortKey,
)
from .schemas import Gap as PlanGap
from .vocabulary import normalize_code

log = logging.getLogger("consultant.agent_tools")

DEFAULT_MAX_TOOL_CALLS_PER_TURN = 3
MAX_TOOL_CALLS_ENV = "CONSULTANT_MAX_TOOL_CALLS_PER_TURN"

# Closed catalog enums (Phase 4B inventory, evaluation/results/consultant_inventory_4b.json).
# Python additionally checks panel/category against the live catalog vocabulary (planning).
PANEL_TECHNOLOGIES = ("OLED", "Neo QLED", "QLED", "Mini LED", "Micro RGB", "Micro LED", "LED")
CATEGORIES = ("OLED", "Neo QLED", "Mini LED", "Micro RGB", "Micro LED", "The Frame", "The Movingstyle",
              "Crystal UHD", "Full HD", "HD")
RESOLUTIONS = tuple(rc.value for rc in ResolutionClass)
FEATURE_IDS = tuple(FEATURES)
USE_CASE_IDS = tuple(USE_CASES)

SEARCH_SORTS = {
    "price_asc": (SortKey.EFFECTIVE_PRICE, SortDir.ASC), "price_desc": (SortKey.EFFECTIVE_PRICE, SortDir.DESC),
    "screen_size_asc": (SortKey.SCREEN_SIZE, SortDir.ASC), "screen_size_desc": (SortKey.SCREEN_SIZE, SortDir.DESC),
    "refresh_rate_desc": (SortKey.REFRESH_RATE, SortDir.DESC),
}
EXTREME_STATS = {
    "cheapest": (SortKey.EFFECTIVE_PRICE, SortDir.ASC), "most_expensive": (SortKey.EFFECTIVE_PRICE, SortDir.DESC),
    "largest": (SortKey.SCREEN_SIZE, SortDir.DESC), "smallest": (SortKey.SCREEN_SIZE, SortDir.ASC),
    "highest_refresh_rate": (SortKey.REFRESH_RATE, SortDir.DESC),
}
COUNT_GROUPS = {"panel_technology": GroupKey.PANEL_TECHNOLOGY, "category": GroupKey.CATEGORY,
                "screen_size_inches": GroupKey.SCREEN_SIZE, "refresh_rate_hz": GroupKey.REFRESH_RATE}
STATES = ("yes", "no", "not_listed")
MAX_LISTED_VALUES = 12                 # a feature count lists each distinct value up to this many, else the range

# A full catalog-shaped model code (same shape as consultant.extract); anything else is a family token.
FULL_CODE = re.compile(r"^[A-Z]{2,3}\d{2,3}[A-Z][A-Z0-9]{4,11}$")
MODEL_REF_PATTERN = r"^[A-Za-z0-9А-Яа-яЁё][A-Za-z0-9А-Яа-яЁё -]{1,23}$"


# ---- JSON Schemas (the single source for the Agent-facing contract and for validation) ----------

def _arr(enum: tuple, description: str) -> dict:
    return {"type": "array", "items": {"type": "string", "enum": list(enum)}, "minItems": 1,
            "maxItems": len(enum), "uniqueItems": True, "description": description}


_FILTERS = {
    "panel_technology": _arr(PANEL_TECHNOLOGIES, "Panel technology, only if the user named it."),
    "category": _arr(CATEGORIES, "Storefront line, e.g. 'The Frame', 'The Movingstyle', 'Crystal UHD'."),
    "resolution": _arr(RESOLUTIONS, "4K = 3840x2160, QHD = 2560x1440, FHD = 1920x1080, HD = 1366x768."),
    "screen_size_inches": {"type": "number", "minimum": 10, "maximum": 130,
                           "description": "Exact screen diagonal in inches (e.g. 65). Do not combine with min/max."},
    "min_screen_size_inches": {"type": "number", "minimum": 10, "maximum": 130,
                               "description": "Lower bound of the diagonal, inches ('от 75 дюймов')."},
    "max_screen_size_inches": {"type": "number", "minimum": 10, "maximum": 130,
                               "description": "Upper bound of the diagonal, inches."},
    "min_price": {"type": "number", "minimum": 0, "maximum": 100000000, "description": "Lower price bound, RUB."},
    "max_price": {"type": "number", "minimum": 0, "maximum": 100000000,
                  "description": "Budget / upper price bound, RUB ('до 200 тысяч' = 200000)."},
    "price_basis": {"type": "string", "enum": ["effective", "list"],
                    "description": "Default 'effective' (current price incl. discount). 'list' only if the user "
                                   "explicitly asks about the price without discount."},
    "min_refresh_rate_hz": {"type": "integer", "minimum": 24, "maximum": 240,
                            "description": "Minimum refresh rate, Hz ('120 Гц' = 120)."},
    "availability": {"type": "string", "enum": ["available", "unavailable"],
                     "description": "Default 'available'. 'unavailable' only if the user asks about products "
                                    "that are out of stock."},
}
_MODEL_REF = {"type": "string", "minLength": 2, "maxLength": 24, "pattern": MODEL_REF_PATTERN}
# Up to the whole registry (Gate 4D.2D): the 4D.1 cap of 8 had no Core reason (compare_tvs already checks all
# attributes by default) and made legitimate overview/comparison calls fail with invalid_arguments in 4D.2C.
_ATTRIBUTES = _arr(FEATURE_IDS, "Registry attributes to check (tri-state yes/no/not_listed). hz_120 = 120 Hz; "
                                "vrr; freesync_premium / freesync_premium_pro; allm; game_bar; hdmi_2_1; earc; "
                                "anti_glare; filmmaker_mode; dolby_atmos; sound_power_w (W); depth_cm; vesa. "
                                "Only the features the user asks about.")


def _obj(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


TOOL_SCHEMAS = {
    "search_tvs": {
        "description": (
            "List Samsung TVs from the live catalog that satisfy explicit constraints the user stated "
            "(panel, size, price, resolution, refresh rate, availability, storefront line). Use for "
            "'покажи / какие есть / есть ли ...' requests. Not for advice ('посоветуй', 'для игр') -> "
            "recommend_tvs; not for counts, 'все ли / у всех ли ...' or 'самый ...' -> get_catalog_stats "
            "(a list is a filtered selection, not the whole catalog). Default: available "
            "products only, ordered by current price ascending."),
        "inputSchema": _obj({**_FILTERS,
                             "sort": {"type": "string", "enum": list(SEARCH_SORTS),
                                      "description": "Ordering; omit for price ascending."},
                             "limit": {"type": "integer", "minimum": 1, "maximum": 20,
                                       "description": "Maximum products to return (default 20)."}}),
    },
    "get_tv": {
        "description": (
            "Authoritative facts about ONE named model code (e.g. QE65S95HAUXPY) or model family "
            "(e.g. S95H): price, availability, key specs, and optionally specific attributes or a "
            "long-tail feature question ('есть ли AirPlay'). For a general overview ('расскажи про ...') "
            "pass only `model`. Unknown models are reported as not found with nearest catalog codes. A "
            "feature missing from the catalog is 'not_listed' (unknown), never 'no'."),
        "inputSchema": _obj({
            "model": {**_MODEL_REF, "description": "Model code or family exactly as the user wrote it, "
                                                   "without the brand ('QE65S95HAUXPY', 'S95H')."},
            "screen_size_inches": {"type": "number", "minimum": 10, "maximum": 130,
                                   "description": "Only for a family: pick the member with this diagonal."},
            "attributes": _ATTRIBUTES,
            "question": {"type": "string", "minLength": 2, "maxLength": 80, "pattern": r"^[^\r\n<>{}]+$",
                         "description": "Short feature phrase NOT covered by attributes, e.g. 'AirPlay', "
                                        "'Wi-Fi 6E', 'Bixby'. Checked against the product's spec rows."},
        }, ("model",)),
    },
    "compare_tvs": {
        "description": (
            "Compare 2-4 explicitly named model codes or families (e.g. ['S95H', 'S90H']). Returns "
            "prices, availability, specs and feature differences. Omit `attributes` for a general "
            "comparison: all registry attributes are compared. If families share several sizes and "
            "no size is given, returns clarification_needed with the size options -- ask the user."),
        "inputSchema": _obj({
            "models": {"type": "array", "items": {**_MODEL_REF, "description": "Model code or family."},
                       "minItems": 2, "maxItems": 4, "uniqueItems": True},
            "screen_size_inches": {"type": "number", "minimum": 10, "maximum": 130,
                                   "description": "Diagonal to compare families at, if the user named one."},
            "attributes": _ATTRIBUTES,
        }, ("models",)),
    },
    "recommend_tvs": {
        "description": (
            "Recommend Samsung TVs for a use case and/or constraints ('посоветуй OLED для PS5 до 200 "
            "тысяч', 'хороший звук без саундбара'). The Consultant ranks deterministically; keep the "
            "returned order. use_cases: gaming (consoles, PS5/Xbox), movies, sound, bright_room, "
            "thin_wall (thin / wall mounting), compact. A goal ('для PS5', 'для игр') is a use case, not "
            "required features. Call with no arguments only if the user gave no need at all."),
        "inputSchema": _obj({
            **_FILTERS,
            "use_cases": _arr(USE_CASE_IDS, "Needs the user stated, mapped to these ids."),
            "required_features": _arr(FEATURE_IDS, "Only features the user explicitly said are mandatory "
                                                   "('обязательно HDMI 2.1'). Never inferred from a use case."),
            "preferred_features": _arr(FEATURE_IDS, "Only features the user mentioned as wishes (not mandatory). "
                                                    "Never add features the user did not mention."),
        }),
    },
    "get_catalog_stats": {
        "description": (
            "Exact catalog aggregates. stat='count': how many products match the filters, in total and "
            "available (`group_by` lists the values that exist). To count a feature ('сколько моделей с "
            "Dolby Atmos', 'у всех ли OLED есть VRR') pass `attributes`: the result gives yes / no / "
            "not_listed per attribute for the counted products. To count inside one series ('все ли S90H в "
            "наличии') pass `model`. Other stats are the tie-aware extremes: cheapest, most_expensive, "
            "largest, smallest, highest_refresh_rate (all products sharing the extreme value are "
            "returned). Never use for advice."),
        "inputSchema": _obj({
            "stat": {"type": "string", "enum": ["count", *EXTREME_STATS]},
            "group_by": {"type": "string", "enum": list(COUNT_GROUPS),
                         "description": "Only with stat='count': the count per existing value."},
            "attributes": _arr(FEATURE_IDS, "Only with stat='count': registry features to count among the "
                                            "counted products (yes / no / not_listed each; for hz_120, "
                                            "sound_power_w and depth_cm also `values`: the listed values and how "
                                            "many products have each). Not a filter."),
            "model": {**_MODEL_REF, "description": "Only with stat='count': count inside this model family "
                                                   "('QN70H') or for one model code."},
            **_FILTERS,
        }, ("stat",)),
    },
}
TOOL_NAMES = tuple(TOOL_SCHEMAS)


# ---- validation ------------------------------------------------------------------------------

class ToolArgumentError(ValueError):
    def __init__(self, errors: list):
        super().__init__("; ".join(errors))
        self.errors = errors


def _type_ok(kind: str, v: Any) -> bool:
    if kind == "object":
        return isinstance(v, dict)
    if kind == "array":
        return isinstance(v, list)
    if kind == "string":
        return isinstance(v, str)
    if kind == "boolean":
        return isinstance(v, bool)
    if isinstance(v, bool):
        return False
    if kind == "integer":
        return isinstance(v, int)
    if kind == "number":
        return isinstance(v, (int, float)) and math.isfinite(v)
    raise ValueError(f"unsupported schema type {kind!r}")


def schema_errors(schema: dict, value: Any, where: str = "arguments") -> list:
    """The JSON Schema subset used by :data:`TOOL_SCHEMAS` (type, enum, bounds, lengths, pattern,
    items, uniqueness, required, additionalProperties=false). Deliberately strict: no coercion."""
    kind = schema["type"]
    if not _type_ok(kind, value):
        return [f"{where}: expected {kind}"]
    errs = []
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{where}: {str(value)[:40]!r} not in {schema['enum']}")
    if kind in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{where}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{where}: must be <= {schema['maximum']}")
    if kind == "string":
        if len(value) < schema.get("minLength", 0):
            errs.append(f"{where}: too short")
        if len(value) > schema.get("maxLength", 10 ** 6):
            errs.append(f"{where}: too long (max {schema['maxLength']})")
        elif "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            errs.append(f"{where}: malformed value")
    if kind == "array":
        if len(value) < schema.get("minItems", 0):
            errs.append(f"{where}: at least {schema['minItems']} item(s) required")
        if len(value) > schema.get("maxItems", 10 ** 6):
            errs.append(f"{where}: at most {schema['maxItems']} item(s)")
        if schema.get("uniqueItems") and len({repr(x) for x in value}) != len(value):
            errs.append(f"{where}: duplicate items")
        for i, item in enumerate(value[:50]):
            errs.extend(schema_errors(schema["items"], item, f"{where}[{i}]"))
    if kind == "object":
        props = schema.get("properties", {})
        unknown = sorted(set(value) - set(props))
        if unknown and schema.get("additionalProperties") is False:
            errs.append(f"{where}: unknown field(s) {[str(u)[:40] for u in unknown[:5]]}")
        for key in schema.get("required", ()):
            if key not in value:
                errs.append(f"{where}.{key}: required")
        for key, sub in props.items():
            if key in value:
                errs.extend(schema_errors(sub, value[key], f"{where}.{key}"))
    return errs


def validate_arguments(tool: str, arguments: Any) -> dict:
    """Return the validated argument dict (top-level ``null`` values dropped as 'not provided')."""
    if tool not in TOOL_SCHEMAS:
        raise ToolArgumentError([f"unknown tool {str(tool)[:40]!r}; available: {list(TOOL_NAMES)}"])
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ToolArgumentError(["arguments: expected object"])
    args = {k: v for k, v in arguments.items() if v is not None}
    errs = schema_errors(TOOL_SCHEMAS[tool]["inputSchema"], args)
    if errs:
        raise ToolArgumentError(errs)
    return args


def _range(args: dict, exact: Optional[str], lo: str, hi: str, errs: list):
    if exact is not None and exact in args and (lo in args or hi in args):
        errs.append(f"{exact}: use either the exact value or {lo}/{hi}, not both")
        return None
    if exact is not None and exact in args:
        return args[exact]
    if lo in args and hi in args and args[lo] > args[hi]:
        errs.append(f"{lo} > {hi}")
        return None
    if lo in args or hi in args:
        return {k: args[a] for k, a in (("min", lo), ("max", hi)) if a in args}
    return None


def constraints_from_args(args: dict) -> dict:
    """Tool filter arguments -> ``QueryPlanDelta.constraints`` (the 4B contract; no new semantics)."""
    errs: list = []
    c: dict = {}
    for key, target in (("panel_technology", "panel_technology"), ("category", "category"),
                        ("resolution", "resolution_class")):
        if key in args:
            c[target] = list(args[key])
    size = _range(args, "screen_size_inches", "min_screen_size_inches", "max_screen_size_inches", errs)
    if size is not None:
        c["screen_size_inches"] = size
    price = _range(args, None, "min_price", "max_price", errs)
    if price is not None:
        c["list_price" if args.get("price_basis") == "list" else "effective_price"] = price
    if "min_refresh_rate_hz" in args:
        c["refresh_rate_hz"] = {"min": args["min_refresh_rate_hz"]}
    if "availability" in args:
        c["is_available"] = args["availability"] == "available"
    if errs:
        raise ToolArgumentError(errs)
    return c


def model_ref(text: str) -> dict:
    """Deterministic ref kind: a catalog-shaped full code is ``full`` (resolved exactly or reported
    not found with suggestions); anything else is a family token (``S95H``)."""
    return {"text": text.strip(), "kind": "full" if FULL_CODE.match(normalize_code(text)) else "family"}


# ---- tools -----------------------------------------------------------------------------------------

def _pipeline(delta_raw: dict, repo, tool: str, args: dict, adjust_plan: Optional[Callable] = None) -> dict:
    delta = QueryPlanDelta.from_dict(delta_raw)
    vocab = repo.vocabulary()
    plan = resolve_plan(delta, repo, vocab)
    if adjust_plan is not None:
        plan = adjust_plan(plan)
    rp = route(plan)
    result = execute(plan, rp, repo)
    bundle = build_evidence(plan, rp, result, repo, None)     # no embedder; semantic tie-break stays off
    return to_agent_payload(tool, bundle, plan, args, result.relaxation)


def search_tvs(repo, args: dict) -> dict:
    delta = {"intent": "list", "constraints": constraints_from_args(args)}
    if "sort" in args:
        key, direction = SEARCH_SORTS[args["sort"]]
        if key is SortKey.EFFECTIVE_PRICE and args.get("price_basis") == "list":
            key = SortKey.LIST_PRICE
        delta["sort"] = {"key": key.value, "dir": direction.value}
    if "limit" in args:
        delta["limit"] = args["limit"]
    return _pipeline(delta, repo, "search_tvs", args)


def _family_size(size: float):
    """Restrict family resolutions to one diagonal; a size the family does not offer is a gap."""
    def adjust(plan):
        resolutions, gaps = [], list(plan.gaps)
        for r in plan.resolutions:
            if r.status != "family":
                resolutions.append(r)
                continue
            kept = tuple(p for p in r.products if p.screen_size_inches == float(size))
            if not kept:
                gaps.append(PlanGap("family_size_not_offered",
                                    f"{r.ref.text!r} is not offered at {size:g}\"; catalog sizes: "
                                    f"{[f'{s:g}' for s in r.sizes]}"))
            resolutions.append(replace(r, products=kept))
        return replace(plan, resolutions=tuple(resolutions), gaps=tuple(gaps))
    return adjust


def get_tv(repo, args: dict) -> dict:
    ref = model_ref(args["model"])
    delta: dict = {"model_refs": [ref]}
    if "attributes" in args or "question" in args:
        delta["intent"] = "spec_question"
        delta["attributes_asked"] = list(args.get("attributes", ()))
        if "question" in args:
            delta["free_text_need"] = args["question"]
    else:
        delta["intent"] = "lookup"
    adjust = _family_size(args["screen_size_inches"]) if ("screen_size_inches" in args
                                                          and ref["kind"] == "family") else None
    return _pipeline(delta, repo, "get_tv", args, adjust)


def compare_tvs(repo, args: dict) -> dict:
    refs = [model_ref(m) for m in args["models"]]
    if len({normalize_code(r["text"]) for r in refs}) != len(refs):
        raise ToolArgumentError(["models: the same model is listed twice"])
    delta = {"intent": "compare", "model_refs": refs,
             "attributes_asked": list(args.get("attributes", FEATURE_IDS))}
    if "screen_size_inches" in args:
        delta["compare_size_inches"] = args["screen_size_inches"]
    return _pipeline(delta, repo, "compare_tvs", args)


def recommend_tvs(repo, args: dict) -> dict:
    required = list(args.get("required_features", ()))
    overlap = sorted(set(required) & set(args.get("preferred_features", ())))
    if overlap:
        raise ToolArgumentError([f"feature(s) {overlap} are both required and preferred"])
    delta = {"intent": "recommend", "constraints": constraints_from_args(args),
             "use_cases": list(args.get("use_cases", ())),
             "features": ([{"id": f, "strength": "required"} for f in required]
                          + [{"id": f, "strength": "preferred"} for f in args.get("preferred_features", ())])}
    return _pipeline(delta, repo, "recommend_tvs", args)


def _value_distribution(values: list) -> dict:
    """``{value: products}`` for a short list of distinct values, otherwise the range."""
    distinct = sorted(set(values))
    if len(distinct) > MAX_LISTED_VALUES:
        return {"min": distinct[0], "max": distinct[-1], "distinct_values": len(distinct)}
    return {f"{v:g}": values.count(v) for v in distinct}


def _attribute_counts(repo, filters, attributes: list, buckets: tuple) -> dict:
    """Per attribute and availability bucket, over the products ``filters`` select: the exact tri-state counts
    and, for attributes that carry a value (refresh rate behind hz_120, sound power, depth), ``values``: how many
    products have each listed value -- "no" for hz_120 says "below 120 Hz", not which rate. The values sit inside
    the same entry as the counts: as a separate field they were overlooked in 3 of 10 measured answers.
    Evaluated by the Feature Registry on the structured rows: never from a list sample or semantic search."""
    found = repo.candidates(filters)
    if found.truncated:
        raise ToolArgumentError([f"attributes: more than {found.limit} products match; add filters"])
    specs = repo.get_specs([p.id for p in found.rows], spec_names_for(attributes))
    results = evaluate_features(found.rows, specs, attributes)
    counts = {}
    for a in attributes:
        counts[a] = {}
        for available, name in buckets:
            rows = [results[p.id][a] for p in found.rows if available is None or p.is_available == available]
            states = [r.state.value for r in rows]
            counts[a][name] = {s: states.count(s) for s in STATES}
            listed = [r.value for r in rows if r.value is not None]
            if listed:
                counts[a][name]["values"] = _value_distribution(listed)
                # The direct answer to "do they all have the same value?" -- asked as "are they all 60 Hz?", a
                # 'no' for 120 Hz was read as "all are 60 Hz" in 3 of 20 measured answers even with the values listed.
                counts[a][name]["same_value_for_all"] = len(set(listed)) == 1 and len(listed) == len(rows)
    return counts


def get_catalog_stats(repo, args: dict) -> dict:
    stat = args["stat"]
    count_only = [k for k in ("group_by", "attributes", "model") if k in args]
    if stat != "count" and count_only:
        raise ToolArgumentError([f"{k}: only valid with stat='count'" for k in count_only])
    if "attributes" in args and "group_by" in args:
        raise ToolArgumentError(["attributes: cannot be combined with group_by; count the attributes for one set of "
                                 "filters (or one model) per call"])
    constraints = constraints_from_args(args)
    if stat == "count":
        delta = {"intent": "lookup", "constraints": constraints}
        if "model" in args:
            delta["model_refs"] = [model_ref(args["model"])]
        plan = resolve_plan(QueryPlanDelta.from_dict(delta), repo)
        f = plan.filters                           # validated / canonical filters; lookup = no policy default
        if plan.resolutions:
            if any(r.status == "not_found" for r in plan.resolutions):
                return stats_payload(plan, None, None, [], args)        # nothing is counted for an unknown model
            f = replace(f, product_ids=tuple(p.id for p in plan.resolved_products))
        buckets = ((None, "total"), (True, "available"), (False, "unavailable"))
        if f.is_available is not None:
            buckets = ((f.is_available, "available" if f.is_available else "unavailable"),)
        counts = {name: repo.count(replace(f, is_available=av))[0][1] for av, name in buckets}
        groups = []
        if "group_by" in args:
            key = COUNT_GROUPS[args["group_by"]]
            avail = dict(repo.count(replace(f, is_available=True), key)) if f.is_available is None else {}
            for value, n in repo.count(f, key):
                g = {"value": value, "count": n}
                if f.is_available is None:
                    g["available"] = avail.get(value, 0)
                groups.append(g)
        attribute_counts = _attribute_counts(repo, f, list(args["attributes"]), buckets) if "attributes" in args else None
        return stats_payload(plan, counts, args.get("group_by"), groups, args, attribute_counts)
    key, direction = EXTREME_STATS[stat]
    if key is SortKey.EFFECTIVE_PRICE and args.get("price_basis") == "list":
        key = SortKey.LIST_PRICE
    delta = {"intent": "superlative", "constraints": constraints, "sort": {"key": key.value, "dir": direction.value},
             "limit": 1}
    return _pipeline(delta, repo, "get_catalog_stats", args)


TOOL_FUNCTIONS = {"search_tvs": search_tvs, "get_tv": get_tv, "compare_tvs": compare_tvs,
                  "recommend_tvs": recommend_tvs, "get_catalog_stats": get_catalog_stats}
assert set(TOOL_FUNCTIONS) == set(TOOL_SCHEMAS)


def add_feature_evidence(payload: dict, repo, asked: tuple = ()) -> dict:
    """Phase 4F.3: three-state evidence for every feature that was asked about.

    ``asked``: registry features the conversation asked about that the Core's plan may not contain -- the
    required features the semantic guard removed, and the features the user named. Each returned product
    (and alternative) gets its state for them (``yes`` / ``no`` / ``not_listed``), and each feature that is not
    listed gets one gap entry, so a feature that was not applied is shown as unknown instead of being absent
    from the result. A feature that is not listed for *any* returned product is also named in
    ``confidence_notes`` (and a ``strong`` result becomes ``partial``), the way ``get_tv`` reports a lookup of
    an attribute the catalog does not list.

    Read-only and additive, and nothing at all is added when no feature was asked about: products, their
    order and every existing field are left as they are. (A list-wide summary of the whole registry and a
    pre-written price text were tried in this phase and removed: the measured answers started listing every
    returned product and mixing up prices. See docs/PHASE_4F_3_MVP_HARDENING.md.)"""
    views = [v for v in (*(payload.get("products") or []), *(payload.get("alternatives") or [])) if v.get("model_code")]
    asked = [f for f in FEATURE_IDS if f in asked]
    if not views or not asked:
        return payload
    rows = {r.model_code: r for r in repo.get_products_by_codes([v["model_code"] for v in views])}
    specs = repo.get_specs([r.id for r in rows.values()], spec_names_for(asked))
    results = evaluate_features(list(rows.values()), specs, asked)

    not_listed: dict = {}
    for view in views:
        row = rows.get(view["model_code"])
        if row is None:
            continue
        features = dict(view.get("features") or {})
        for f in asked:
            if f not in features:
                r = results[row.id][f]
                features[f] = feature_view(r.state.value, r.value, r.data_quality)
                if r.state.value == "not_listed":
                    not_listed.setdefault(f, []).append(view["ref"])
        view["features"] = features
    if isinstance(payload.get("request"), dict):
        payload["request"]["features_checked"] = asked
    payload.setdefault("gaps", []).extend(
        {"kind": "attribute_not_listed_for_product", "products": refs, "attributes": [f], "detail": NOT_LISTED_DETAIL}
        for f, refs in not_listed.items())
    unknown_for_all = [f for f, refs in not_listed.items() if len(refs) == len(views)]
    if unknown_for_all:
        # Said where the result's own caveats are read first, as get_tv does for an attribute that is not listed.
        if payload.get("confidence") == "strong":
            payload["confidence"] = "partial"
        payload["confidence_notes"] = [*payload.get("confidence_notes", ()), NOT_LISTED_FOR_ALL_NOTE.format(
            features=", ".join(unknown_for_all))]
    return payload


def run_tool(name: str, arguments: Any, repo) -> dict:
    """Validate and execute one tool call. Argument problems come back as a structured
    ``invalid_arguments`` result the Agent can correct; they never reach SQL."""
    try:
        args = validate_arguments(name, arguments)
        return TOOL_FUNCTIONS[name](repo, args)
    except ToolArgumentError as e:
        return error_payload(str(name)[:40], "invalid_arguments", e.errors)
    except PlanValidationError as e:
        return error_payload(name, "invalid_arguments", [str(e)])


# ---- per-turn cap ------------------------------------------------------------------------------------

class TurnBudget:
    """Counts domain-tool calls per turn key (bounded memory). Thread-safe."""

    def __init__(self, max_calls: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN, max_turns: int = 10000):
        if max_calls < 1:
            raise ValueError("max_calls must be >= 1")
        self.max_calls = max_calls
        self._max_turns = max_turns
        self._counts: OrderedDict = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, turn_id: str) -> tuple:
        """``(allowed, call_number)``. Calls past the cap are refused and still counted."""
        with self._lock:
            n = self._counts.pop(turn_id, 0) + 1
            self._counts[turn_id] = n
            while len(self._counts) > self._max_turns:
                self._counts.popitem(last=False)
            return n <= self.max_calls, n


def max_calls_from_env(default: int = DEFAULT_MAX_TOOL_CALLS_PER_TURN) -> int:
    raw = os.environ.get(MAX_TOOL_CALLS_ENV, "").strip()
    if not raw:
        return default
    value = int(raw)
    if not 1 <= value <= 10:
        raise ValueError(f"{MAX_TOOL_CALLS_ENV} must be between 1 and 10")
    return value


class ConsultantTools:
    """Tool dispatcher with the per-turn cap. ``repo_provider()`` returns a context manager that
    yields a read-only ``CatalogRepository`` (see ``mcp_server.ReadOnlyRepositoryProvider``)."""

    def __init__(self, repo_provider: Callable, max_calls_per_turn: Optional[int] = None):
        self._repo_provider = repo_provider
        self.budget = TurnBudget(max_calls_per_turn or max_calls_from_env())

    @classmethod
    def for_repository(cls, repo, max_calls_per_turn: Optional[int] = None) -> "ConsultantTools":
        return cls(lambda: nullcontext(repo), max_calls_per_turn)

    def call(self, name: str, arguments: Any, turn_id: Optional[str] = None,
             conversation: Optional[tuple] = None, act: bool = True) -> dict:
        """``conversation``: evidence of the user's messages so far (Phase 4E.2 semantic guard). Without
        it the guard is skipped and the call is exactly the Phase 4D path."""
        started = time.monotonic()
        if turn_id is not None:
            allowed, n = self.budget.consume(turn_id)
            if not allowed:
                payload = error_payload(str(name)[:40], "tool_call_limit_reached", [
                    f"At most {self.budget.max_calls} catalog tool calls per user message (this was call {n}). "
                    "Answer from the tool results you already have and say what is still unknown."])
                self._log(name, payload, started)
                return payload
        guard = None
        if conversation:
            try:
                from .semantic_guard import guard_tool_arguments   # lazy: semantic_guard imports this module
                guard = guard_tool_arguments(name, arguments, conversation, act)
                arguments = guard.arguments
            except Exception:                       # additive safety: fall back to the Phase 4D path
                log.exception("semantic guard failed; original arguments used")
                guard = None
        asked = self._asked_features(guard, conversation)
        try:
            with self._repo_provider() as repo:
                payload = run_tool(name, arguments, repo)
                try:
                    payload = add_feature_evidence(payload, repo, asked)
                except Exception:                   # additive evidence: on any failure the plain result stands
                    log.exception("feature evidence unavailable for %s", str(name)[:40])
        except Exception:                           # never leak internals (SQL, DSN, traceback) to the Agent
            log.exception("tool %s failed", str(name)[:40])
            payload = error_payload(str(name)[:40], "error", ["The catalog is temporarily unavailable."])
        if guard is not None and guard.status == "modified" and isinstance(payload.get("request"), dict):
            from .semantic_guard import not_applied_note
            payload["request"]["not_applied"] = not_applied_note(guard)
        self._log(name, payload, started, guard)
        return payload

    @staticmethod
    def _asked_features(guard, conversation: Optional[tuple]) -> tuple:
        """Registry features this conversation asked about: required features the guard did not apply, and
        features the user named (``MessageEvidence.features``; derived ids, never text)."""
        asked = [a.value for a in guard.removed if a.argument == "required_features"] if guard is not None else []
        for message in conversation or ():
            asked.extend(getattr(message, "features", ()))
        return tuple(dict.fromkeys(asked))

    @staticmethod
    def _log(name: str, payload: dict, started: float, guard=None) -> None:
        log.info("tool_call %s", {"tool": str(name)[:40], "status": payload.get("status"),
                                  "products": len(payload.get("products", ())),
                                  "gaps": [g.get("kind") for g in payload.get("gaps", ())],
                                  "ms": round((time.monotonic() - started) * 1000),
                                  "contract": AGENT_CONTRACT_VERSION,
                                  **(guard.summary() if guard is not None else {})})
