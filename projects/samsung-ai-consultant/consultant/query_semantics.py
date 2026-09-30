"""Phase 4E.1 spike: minimal query semantics, ``raw query -> intent / explicit filters / preferences``.

Isolated prototype. Nothing in the runtime (tools, MCP server, planning, ranking) imports it.

Contract (:class:`QuerySemantics`):

* ``intent`` -- one of four closed values, or ``None`` when the message is not a catalog request
  (greeting, general explanation). ``None`` is "no catalog intent", not a fifth intent.
* ``filters`` -- hard constraints the user **stated**. The field names and value schemas are taken
  from the published ``search_tvs`` / ``compare_tvs`` tool contract (``agent_tools.TOOL_SCHEMAS``),
  so semantics can never hold a filter the Consultant tools would not accept.
* ``preferences`` -- use cases and qualitative wishes. They never exclude a product.
* ``notes`` -- debugging metadata: what was recognised but deliberately *not* structured (negated
  or optional panel, vague price / size / brightness / audio power, a named feature, a superlative).

Parser (:func:`parse_query_semantics`): deterministic, no LLM, no I/O. Explicit filters come only
from spans the Phase 4B extractor (``consultant.extract``) finds in the text; bounds are classified
here (the 4B extractor reads "не меньше" as an upper bound). Preferences come from a small closed
stem lexicon. Qualitative wording ("недорогой", "большой", "яркий", "мощный звук", "для PS5") can
become a preference or a note, never a number.

Failure contract: the parser never raises. Any problem returns :class:`SemanticParse` with
``status="unavailable"`` and no semantics. That means "no semantic information" -- the raw query
stays the input -- never "no filters" and never "no products match".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .agent_tools import TOOL_SCHEMAS, ToolArgumentError, constraints_from_args, schema_errors
from .extract import _PANEL_WORDS, _SIZE, extract
from .vocabulary import CatalogVocabulary, normalize_code

PARSER_VERSION = "query-semantics-4e1-deterministic-v1"
MAX_QUERY_CHARS = 1000


class SemanticValidationError(ValueError):
    """A semantics document is invalid (unknown key, bad enum, bad type, inconsistent range)."""


class SemanticIntent(str, Enum):
    RECOMMENDATION = "recommendation"
    COMPARISON = "comparison"
    PRODUCT_QUESTION = "product_question"
    CATALOG_QUESTION = "catalog_question"


class Preference(str, Enum):
    GAMING = "gaming"
    MOVIES = "movies"
    SPORTS = "sports"
    BRIGHT_ROOM = "bright_room"
    AUDIO = "audio"
    PICTURE_QUALITY = "picture_quality"


# ---- schema (derived from the published tool contract) ----------------------------------------

_TOOL_FILTERS = TOOL_SCHEMAS["search_tvs"]["inputSchema"]["properties"]
FILTER_KEYS = ("panel_technology", "resolution", "screen_size_inches", "min_screen_size_inches",
               "max_screen_size_inches", "min_price", "max_price", "price_basis", "min_refresh_rate_hz",
               "availability")
_MODELS = {**TOOL_SCHEMAS["compare_tvs"]["inputSchema"]["properties"]["models"], "minItems": 1}
NOTE_PATTERN = r"^[a-z_]+(?::[a-z0-9_]+)?$"

SEMANTICS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["filters", "preferences"],
    "properties": {
        "intent": {"type": "string", "enum": [i.value for i in SemanticIntent]},
        "filters": {"type": "object", "additionalProperties": False, "required": [],
                    "properties": {**{k: _TOOL_FILTERS[k] for k in FILTER_KEYS}, "models": _MODELS}},
        "preferences": {"type": "array", "items": {"type": "string", "enum": [p.value for p in Preference]},
                        "maxItems": len(Preference), "uniqueItems": True},
        "notes": {"type": "array", "items": {"type": "string", "maxLength": 60, "pattern": NOTE_PATTERN},
                  "maxItems": 20},
    },
}


@dataclass(frozen=True)
class SemanticFilters:
    panel_technology: tuple = ()
    resolution: tuple = ()
    screen_size_inches: Optional[float] = None
    min_screen_size_inches: Optional[float] = None
    max_screen_size_inches: Optional[float] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    price_basis: Optional[str] = None
    min_refresh_rate_hz: Optional[int] = None
    availability: Optional[str] = None
    models: tuple = ()

    def to_dict(self) -> dict:
        out = {}
        for key in (*FILTER_KEYS, "models"):
            v = getattr(self, key)
            if v in (None, ()):
                continue
            out[key] = list(v) if isinstance(v, tuple) else v
        return out


@dataclass(frozen=True)
class QuerySemantics:
    intent: Optional[SemanticIntent]
    filters: SemanticFilters = field(default_factory=SemanticFilters)
    preferences: tuple = ()               # Preference
    notes: tuple = ()                     # str, NOTE_PATTERN

    def to_dict(self) -> dict:
        return {"intent": self.intent.value if self.intent else None, "filters": self.filters.to_dict(),
                "preferences": [p.value for p in self.preferences], "notes": list(self.notes)}

    @staticmethod
    def from_dict(raw: Any) -> "QuerySemantics":
        """Strict validation: closed keys and enums, tool-contract value schemas, no coercion, and
        the tool boundary's range checks (exact size vs min/max, min <= max)."""
        if not isinstance(raw, dict):
            raise SemanticValidationError("semantics: expected object")
        doc = {k: v for k, v in raw.items() if v is not None}       # top-level null = not provided
        errs = schema_errors(SEMANTICS_SCHEMA, doc, "semantics")
        if errs:
            raise SemanticValidationError("; ".join(errs))
        f = doc["filters"]
        try:
            constraints_from_args({k: v for k, v in f.items() if k != "models"})
        except ToolArgumentError as e:
            raise SemanticValidationError("; ".join(f"semantics.filters: {x}" for x in e.errors)) from None
        filters = SemanticFilters(**{k: tuple(v) if isinstance(v, list) else v for k, v in f.items()})
        return QuerySemantics(
            intent=SemanticIntent(doc["intent"]) if "intent" in doc else None, filters=filters,
            preferences=tuple(Preference(p) for p in doc["preferences"]), notes=tuple(doc.get("notes", ())))


@dataclass(frozen=True)
class SemanticParse:
    """``status`` is ``ok`` (``semantics`` set) or ``unavailable`` (``semantics`` is ``None``;
    the caller keeps using the raw query and must not read this as "no constraints")."""
    query: str
    status: str
    semantics: Optional[QuerySemantics] = None
    error: Optional[str] = None
    parser: str = PARSER_VERSION

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---- lexicons ---------------------------------------------------------------------------------

_W = r"(?<![a-zа-я0-9])"                 # word start (text is lower-cased, ё -> е)

PREFERENCE_PATTERNS = {
    Preference.GAMING: _W + r"(?:игр|гейм|gaming|ps\s?[45]|playstation|плейстейшн|плойк|xbox|иксбокс|"
                            r"консол|приставк|nintendo|киберспорт)",
    Preference.MOVIES: _W + r"(?:фильм|кино|сериал|movie|film)",
    Preference.SPORTS: _W + r"(?:футбол|хокке|спорт|баскетбол|теннис|матч)",
    Preference.BRIGHT_ROOM: _W + r"(?:светл\w*\s+(?:комнат|гостин|помещени|кухн|зал|спальн|квартир)|"
                                 r"солнечн|солнц|блик|засвет|засвеч|"
                                 r"днем\W+(?:\w+\W+){0,4}?(?:блекл|тускл|видн|бледн))",
    Preference.AUDIO: _W + r"(?:звук|акустик|саундбар|колонк|динамик|громк|sound|audio)",
    Preference.PICTURE_QUALITY: _W + r"(?:качеств\w*\s+(?:картинк|изображени)|"
                                     r"(?:картинк|изображени)\w*\s+(?:\w+\s+){0,2}?(?:качествен|хорош|лучш|отличн|четк|сочн)|"
                                     r"(?:хорош|лучш|качествен|отличн|четк|сочн|красив)\w*\s+(?:\w+\s+)?(?:картинк|изображени)|"
                                     r"контраст|цветопередач|глубок\w*\s+черн|ярк)",
}

# Wording that is understood but must never become a number.
VAGUE_PATTERNS = {
    "price": _W + r"(?:недорог|дешев|бюджетн|подешевле|эконом|не\s+дорог|не\s+слишком\s+дорог)",
    "size": _W + r"(?:больш\w*\s+(?:\w+\s+)?(?:телевизор|телек|экран|диагонал)|побольше|огромн|крупн)",
    "brightness": _W + r"ярк",
    "audio_power": _W + r"(?:мощн\w*\s+(?:\w+\s+)?звук|громк)",
}

# Registry features named in the text. The minimal contract has no feature field, so a named
# feature stays in the raw query; the note only records that it was seen.
FEATURE_MENTIONS = {
    "hdmi_2_1": r"hdmi\s*2[.,]1", "vrr": _W + r"vrr", "allm": _W + r"allm", "freesync": r"freesync",
    "dolby_atmos": r"atmos|атмос", "earc": _W + r"earc", "vesa": _W + r"vesa",
}

_GENERAL = re.compile(_W + r"(?:объясни|что\s+такое|зачем\s+(?:\w+\s+)?(?:нуж|телевизор)|как\s+работает|"
                           r"what\s+is|explain)")
_COMPARE = re.compile(_W + r"(?:сравн|или|vs|versus|против|разниц|отлича)")
_COUNT = re.compile(_W + r"(?:сколько|how\s+many)")
_RECOMMEND = re.compile(_W + r"(?:посовет|порекоменд|подбер|подобра|помоги|что\s+(?:взять|выбрать|купить)|"
                             r"какой\s+(?:\w+\s+){0,2}?(?:лучше|выбрать|взять|подойд)|хочу|хочет|хочется|"
                             r"нужен|нужна|нужно|ищу|recommend|suggest|need|want)")
_CATALOG = re.compile(_W + r"(?:какие|покажи|показать|список|перечисл|есть\s+ли|в\s+каталоге|show|list)|"
                           r"^\W*есть(?![a-zа-я0-9])")

# Bounds: the text right before a number. Negated comparatives first ("не меньше" is a minimum).
_BOUNDS = (
    ("min", re.compile(_W + r"не\s+(?:меньше|менее|дешевле|ниже)\s*$")),
    ("max", re.compile(_W + r"не\s+(?:больше|более|дороже|выше)\s*$")),
    ("approx", re.compile(_W + r"(?:около|примерно|приблизительно|в\s+районе|порядка|где-?то)\s*$")),
    ("min", re.compile(_W + r"(?:от|больше|более|дороже|свыше|минимум|хотя\s+бы|from|over|at\s+least)\s*$")),
    ("max", re.compile(_W + r"(?:до|дешевле|меньше|максимум|в\s+пределах|бюджет\w*|under|up\s+to|below)"
                            r"\s*[:—–-]?\s*$")),
)
_ALTERNATIVE_BEFORE = re.compile(r"\d{2,3}\s*(?:или|либо|/|-|–|—|or)\s*$")
_NEGATED_BEFORE = re.compile(_W + r"(?:только\s+не|но\s+не|кроме|без|не|никаких)\s+(?:\w+\s+)?$")
# "OLED не принципиален", "OLED, не важно", "OLED мне не важен". The skipped word may not cross
# punctuation, so in "для кино, OLED не принципиален" the optionality stays with OLED.
_OPTIONAL_AFTER = re.compile(r"^(?:\s*[,—–-]?\s*|\s+\w+\s+)(?:не\s+принципиал|не\s+обязател|необязател|не\s+важ|"
                             r"неважн|не\s+критич|не\s+нуж|без\s+разницы|все\s+равно)")
_OPTIONAL_BEFORE = re.compile(_W + r"(?:не\s+обязательно|необязательно|можно\s+и\s+не|не\s+важно)\s+$")


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def _bound(before: str) -> Optional[str]:
    tail = _norm(before)[-30:]
    for kind, pattern in _BOUNDS:
        if pattern.search(tail):
            return kind
    return None


def _spans(low: str, phrases) -> list:
    out = []
    for phrase in phrases:
        for m in re.finditer(r"(?<![a-zа-я0-9])" + re.escape(phrase) + r"(?![a-zа-я0-9])", low):
            out.append((m.start(), m.end()))
    return out


def _stance(low: str, spans: list, negation: bool = True) -> Optional[str]:
    """``negated`` / ``optional`` if every mention of a value is excluded or declared optional."""
    if not spans:
        return None
    kinds = set()
    for start, end in spans:
        before, after = low[max(0, start - 25):start], low[end:end + 30]
        if _OPTIONAL_AFTER.search(after) or _OPTIONAL_BEFORE.search(before):
            kinds.add("optional")
        elif negation and _NEGATED_BEFORE.search(before):
            kinds.add("negated")
        else:
            return None
    return "negated" if "negated" in kinds else "optional"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


# ---- parser -----------------------------------------------------------------------------------

def _filters(query: str, low: str, ex, notes: list) -> dict:
    f: dict = {}

    panels = []
    for value in ex.panel_technologies:
        stance = _stance(low, _spans(low, [p for p, v in _PANEL_WORDS if v == value]))
        if stance:
            notes.append(f"{stance}_panel_technology:{_slug(value)}")
        else:
            panels.append(value)
    if panels:
        f["panel_technology"] = panels
    if ex.resolution_classes:
        f["resolution"] = [r.value for r in ex.resolution_classes]

    sizes: dict = {}
    for m in _SIZE.finditer(query):
        value = float(m.group(1).replace(",", "."))
        if value not in ex.screen_sizes:
            continue
        if _ALTERNATIVE_BEFORE.search(_norm(query[:m.start()])[-12:]):     # "55 или 65 дюймов"
            notes.append("size_alternatives")
            sizes = {}
            break
        kind = _bound(query[:m.start()])
        if kind == "approx":
            notes.append("approximate_size")
            continue
        sizes.setdefault(kind or "exact", []).append(value)
    if sizes:
        if len(sizes.get("exact", ())) > 1:
            notes.append("size_alternatives")
        elif sizes.get("exact") and ("min" in sizes or "max" in sizes):
            notes.append("size_conflict")
        else:
            for kind, key in (("exact", "screen_size_inches"), ("min", "min_screen_size_inches"),
                              ("max", "max_screen_size_inches")):
                if sizes.get(kind):
                    f[key] = (max if kind == "min" else min)(sizes[kind])
            if f.get("min_screen_size_inches", 0) > f.get("max_screen_size_inches", 1e9):
                notes.append("size_conflict")
                f.pop("min_screen_size_inches"), f.pop("max_screen_size_inches")

    if len(ex.refresh_rates) == 1 and 24 <= ex.refresh_rates[0] <= 240:
        f["min_refresh_rate_hz"] = ex.refresh_rates[0]
    elif ex.refresh_rates:
        notes.append("refresh_rate_alternatives")

    cursor = 0
    for p in ex.prices:
        pos = query.find(p.text, cursor)
        pos = pos if pos >= 0 else query.find(p.text)
        cursor = max(cursor, pos + len(p.text))
        kind = _bound(query[:pos]) if pos >= 0 else None
        if kind in ("min", "max"):
            key = f"{kind}_price"
            f[key] = (max if kind == "min" else min)(f.get(key, p.value), p.value)
        else:
            notes.append("approximate_price" if kind == "approx" else "unbounded_price")
    if ("min_price" in f or "max_price" in f) and ex.price_basis == "list":
        f["price_basis"] = "list"
    if f.get("min_price", 0) > f.get("max_price", 1e12):
        notes.append("price_conflict")
        f.pop("min_price"), f.pop("max_price")

    if ex.is_available is not None:
        f["availability"] = "available" if ex.is_available else "unavailable"
    return f


def _models(low: str, ex) -> list:
    """Model codes and family tokens as the user wrote them (normalized), in order of mention."""
    normalized = normalize_code(low)

    def position(code: str) -> tuple:
        pos = normalized.find(code)
        return (pos if pos >= 0 else len(normalized), code)
    return sorted(dict.fromkeys([*ex.model_codes, *ex.unknown_model_codes, *ex.family_tokens]), key=position)


def _preferences(low: str, notes: list) -> list:
    out = []
    for pref, pattern in PREFERENCE_PATTERNS.items():
        spans = [(m.start(), m.end()) for m in re.finditer(pattern, low)]
        # "без саундбара" / "без отдельной акустики" negate the external device, not the audio need.
        stance = _stance(low, spans, negation=pref is not Preference.AUDIO)
        if stance:
            notes.append(f"{stance}_preference:{pref.value}")
        elif spans:
            out.append(pref)
    return out


def _intent(low: str, models: list, filters: dict, prefs: list, notes: list, ex) -> Optional[SemanticIntent]:
    if len(models) >= 2:
        return SemanticIntent.COMPARISON
    if len(models) == 1:
        return SemanticIntent.PRODUCT_QUESTION
    if len(filters.get("panel_technology", ())) >= 2 and _COMPARE.search(low):
        return SemanticIntent.COMPARISON
    if _COUNT.search(low) or ex.superlative is not None:
        return SemanticIntent.CATALOG_QUESTION
    if prefs or _RECOMMEND.search(low):
        return SemanticIntent.RECOMMENDATION
    if _CATALOG.search(low):
        return SemanticIntent.CATALOG_QUESTION
    if filters or any(n.startswith("vague_") for n in notes):
        return SemanticIntent.RECOMMENDATION
    return None


def _parse(query: str, vocab: Optional[CatalogVocabulary]) -> QuerySemantics:
    low = _norm(query)
    ex = extract(query, vocab)
    notes: list = []
    models = _models(low, ex)

    if _GENERAL.search(low) and not models:
        notes.append("general_question")
        return QuerySemantics(None, SemanticFilters(), (), tuple(notes))

    filters = _filters(query, low, ex, notes)
    if models:
        if len(models) > 4:
            notes.append("too_many_models")
        filters["models"] = models[:4]
    prefs = _preferences(low, notes)

    if ex.superlative is not None:
        notes.append(f"superlative:{ex.superlative.key.value}_{ex.superlative.direction.value}")
    for kind, pattern in VAGUE_PATTERNS.items():
        if re.search(pattern, low) and not (kind == "price" and ex.superlative is not None):
            notes.append(f"vague_{kind}")
    for fid, pattern in FEATURE_MENTIONS.items():
        if re.search(pattern, low):
            notes.append(f"feature_mention:{fid}")

    intent = _intent(low, models, filters, prefs, notes, ex)
    if intent is None:
        filters, prefs = {}, []
    sem = {"intent": intent.value if intent else None, "filters": filters,
           "preferences": [p.value for p in prefs], "notes": list(dict.fromkeys(notes))}
    return QuerySemantics.from_dict(sem)


def parse_query_semantics(query: Any, vocab: Optional[CatalogVocabulary] = None) -> SemanticParse:
    """Never raises. ``vocab`` (``CatalogRepository.vocabulary()``) enables family tokens (``S95H``)
    and restricts panel values to the catalog; without it only full model codes are recognised."""
    text = query if isinstance(query, str) else ""
    try:
        if not isinstance(query, str) or not query.strip():
            return SemanticParse(text, "unavailable", error="empty_query")
        if len(query) > MAX_QUERY_CHARS:
            return SemanticParse(text[:MAX_QUERY_CHARS], "unavailable", error="query_too_long")
        return SemanticParse(query, "ok", _parse(query, vocab))
    except Exception as e:                  # the failure contract: unavailable, never a guessed constraint
        return SemanticParse(text, "unavailable", error=type(e).__name__)
