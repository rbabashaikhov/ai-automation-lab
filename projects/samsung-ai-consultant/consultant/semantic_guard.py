"""Phase 4E.2 semantic guard: conservative checks on Agent-proposed hard constraints.

The Agent (n8n) still selects the tool and fills its arguments. Before the unchanged Phase 4D
validation and pipeline run, :func:`guard_tool_arguments` removes a hard constraint only on
**positive evidence** that the user never stated it:

* ``min_price`` / ``max_price`` / screen sizes / ``min_refresh_rate_hz`` -- the value appears as
  no number in any user message of the conversation (digits anywhere count, including inside a
  named model code such as ``QE65S95HAUXPY``; ``150``, ``150к`` and ``0,15 млн`` all support
  150000). Spelled-out and slang numbers are read the same way ("сто тысяч", "до ста пятидесяти
  тысяч", "сотка", "полтинник": Phase 4F.3, :func:`spelled_numbers`). A conversation with a number
  word the guard cannot read with certainty ("пара сотен", "тысяч двести") is left alone;
* ``required_features`` -- the feature is mentioned in no user message (closed patterns, one per
  Feature Registry id). "для PS5" mentions no feature; "обязательно HDMI 2.1" does.

Absence from :class:`QuerySemantics` filters is never a reason on its own. Everything else --
panel, resolution, availability, models, sort, limit, stat, attributes, preferred features and
the Agent's ``use_cases`` -- is passed through unchanged. The guard adds only one thing: when it
removed a constraint from a ``recommend_tvs`` call, the use cases implied by the user's own words
(gaming, movies, sound, bright_room) are merged into ``use_cases`` so the soft ranking signal
survives. It never adds a filter, size, price, refresh rate, feature, technology or resolution.

Fail-safe: the guard never raises. No conversation context, original arguments the tool boundary
rejects anyway, guarded arguments that fail validation, or any internal error return the original
arguments unchanged (Phase 4D behaviour) with a status saying why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from .agent_tools import ToolArgumentError, constraints_from_args, validate_arguments
from .features import FEATURES
from .query_semantics import Preference, parse_query_semantics

GUARD_VERSION = "semantic-guard-4f3-v1"     # 4F.3: spelled / slang numbers are read instead of disabling the rules

_W = r"(?<![a-zа-я0-9])"

# One pattern per Feature Registry id: does the user's text name this feature at all?
FEATURE_MENTIONS = {
    "hz_120": r"(?<!\d)(?:1[2-9]\d|2[0-4]\d)\s*(?:гц|hz|герц|fps|кадр)",
    "vrr": _W + r"(?:vrr|переменн\w*\s+частот|variable\s+refresh)",
    "freesync_premium": r"free\s*-?sync|фри\s*-?синк",
    "freesync_premium_pro": r"(?:free\s*-?sync|фри\s*-?синк)\s*(?:premium\s*)?pro|премиум\s+про",
    "allm": _W + r"(?:allm|auto\s+low\s+latency|автовключени\w*\s+игров|автоматическ\w*\s+(?:\w+\s+)?игров\w*\s+режим)",
    "game_bar": r"game\s*bar|игров\w*\s+панел",
    "hdmi_2_1": r"hdmi\s*2[.,]?1",
    "earc": _W + r"e-?arc",
    "anti_glare": r"антиблик|anti[\s-]?(?:glare|reflection)|матов\w*\s+(?:экран|покрыти)|без\s+блик",
    "filmmaker_mode": r"filmmaker|режим\w*\s+режисс",
    "dolby_atmos": r"atmos|атмос",
    "sound_power_w": r"мощност\w*\s+звук|(?<![a-zа-я])\d+\s*(?:вт|w|ватт)(?![a-zа-я])|ватт",
    "depth_cm": r"толщин|глубин|тонк",
    "vesa": _W + r"(?:vesa|веса)(?![a-zа-я])|кронштейн|на\s+стен",
}
assert set(FEATURE_MENTIONS) == set(FEATURES), "one mention pattern per registry feature"

_NUMBER = re.compile(r"\d{1,3}(?:[   ]\d{3})+|\d+(?:[.,]\d+)?")
_DIGIT_AMOUNT = re.compile(r"\d[\d   .,]*\s*(?:тысяч\w*|тыщ\w*|тыс\.?|млн\.?|миллион\w*|к|k)?")
_SPELLED = re.compile(_W + r"(?:один|одна|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять|\w+надцать|"
                           r"двадцать|тридцать|сорок|пятьдесят|шестьдесят|семьдесят|восемьдесят|девяносто|сто|"
                           r"двести|триста|четыреста|пятьсот|\w+сот|тысяч\w*|тыщ\w*|миллион\w*|млн|полтор\w*|"
                           r"полтинник\w*|косар\w*|сотк\w*|сотни|сотен)(?![a-zа-я0-9])")

# ---- spelled-out and slang numbers (Phase 4F.3) -------------------------------------------------
# Before 4F.3 any number word switched the numeric rules off for the whole conversation, so "до сотки" in one
# turn let an invented max_price through in the next (4F.2 PA-08). The words are now read. The grammar is strict:
# whatever it does not accept is reported as unreadable, which is the old behaviour (rules off).

def _forms(table: dict) -> dict:
    return {form: value for forms, value in table.items() for form in forms.split()}


_ONES = _forms({"один одна одну одного одной": 1, "два две двух": 2, "три трех": 3, "четыре четырех": 4, "пять пяти": 5,
                "шесть шести": 6, "семь семи": 7, "восемь восьми": 8, "девять девяти": 9})
_TEENS = _forms({"десять десяти": 10, "одиннадцать одиннадцати": 11, "двенадцать двенадцати": 12,
                 "тринадцать тринадцати": 13, "четырнадцать четырнадцати": 14, "пятнадцать пятнадцати": 15,
                 "шестнадцать шестнадцати": 16, "семнадцать семнадцати": 17, "восемнадцать восемнадцати": 18,
                 "девятнадцать девятнадцати": 19})
_TENS = _forms({"двадцать двадцати": 20, "тридцать тридцати": 30, "сорок сорока": 40, "пятьдесят пятидесяти": 50,
                "шестьдесят шестидесяти": 60, "семьдесят семидесяти": 70, "восемьдесят восьмидесяти": 80,
                "девяносто девяноста": 90})
_HUNDREDS = _forms({"сто ста": 100, "двести двухсот": 200, "триста трехсот": 300, "четыреста четырехсот": 400,
                    "пятьсот пятисот": 500, "шестьсот шестисот": 600, "семьсот семисот": 700, "восемьсот восьмисот": 800,
                    "девятьсот девятисот": 900})
_FRACTIONS = _forms({"полтора полторы полутора": 1.5})
_HALF_MILLION = re.compile(r"пол(?:миллиона|ляма)")                    # one word; "пол миллиона" is left unreadable
_THOUSAND = re.compile(r"тысяч\w*|тыщ\w*|тыс|косар\w*")
_MILLION = re.compile(r"миллион\w*|млн|лям\w*")
_BARE_MULTIPLIERS = frozenset({"тысяча", "тысячу", "миллион", "лям", "косарь"})    # "тысяча двести" = 1200
_SLANG = ((re.compile(r"сотк\w*|сотня|сотню|сотни|сотен"), 100), (re.compile(r"полтинник\w*"), 50))
_NEEDS_COUNT = frozenset({"сотен"})                                    # "пять сотен" yes; "пара сотен" is not read
_VAGUE_BEFORE = frozenset({"#", "пара", "пару", "пары", "несколько", "нескольких", "пол"})   # "#" = a digit amount
_WORD = re.compile(r"#|[a-zа-я0-9]+")


def _number_word(word: str) -> Optional[tuple]:
    """``(kind, value)``: h/t/n/o = hundreds / tens / 10-19 / ones, f = fraction, s = slang unit, m = multiplier,
    x = a complete value, ? = a number word outside the grammar. ``None`` for any other word."""
    for kind, table in (("o", _ONES), ("n", _TEENS), ("t", _TENS), ("h", _HUNDREDS), ("f", _FRACTIONS)):
        if word in table:
            return kind, table[word]
    if _HALF_MILLION.fullmatch(word):
        return "x", 500_000
    if _THOUSAND.fullmatch(word):
        return "m", 1_000
    if _MILLION.fullmatch(word):
        return "m", 1_000_000
    for pattern, value in _SLANG:
        if pattern.fullmatch(word):
            return "s", value
    # The pre-4F.3 lexicon: a form it knew that the tables above do not hold ("шестисотый", "полторашка")
    return ("?", None) if _SPELLED.fullmatch(word) else None


def _read_run(run: list) -> Optional[float]:
    """One run of consecutive number words -> its value; ``None`` unless the run is exactly
    ``group [multiplier]`` repeated with strictly descending multipliers, where a group is
    ``[hundreds] [tens [ones] | 10-19 | ones]``, a fraction, or ``[ones | fraction] slang-unit``."""
    total, last_multiplier, i = 0.0, float("inf"), 0
    while i < len(run):
        group, order = None, 4                        # order: the magnitude the next simple word must be below
        while i < len(run) and run[i][1] in "htno":
            _, kind, value = run[i]
            rank = {"h": 3, "t": 2, "n": 1, "o": 1}[kind]
            if rank >= order or (kind == "n" and order == 2):          # "двадцать десять"
                return None
            group, order = (group or 0) + value, rank
            i += 1
        if i < len(run) and run[i][1] == "f" and group is None:
            group, order = run[i][2], 1
            i += 1
        if i < len(run) and run[i][1] == "s":
            word, _, unit = run[i]
            if (group is None and word in _NEEDS_COUNT) or (group is not None and order != 1):
                return None                            # "сотен" alone; "сто соток"
            group = (group or 1) * unit
            i += 1
        if i < len(run) and run[i][1] == "x" and group is None and len(run) == 1:
            return float(run[i][2])
        if i < len(run) and run[i][1] == "m":
            word, _, multiplier = run[i]
            if multiplier >= last_multiplier or (group is None and word not in _BARE_MULTIPLIERS):
                return None                            # "тысяч миллион"; "тысяч двести" (about 200 thousand)
            total, last_multiplier = total + (group or 1) * multiplier, multiplier
            i += 1
        elif group is not None and i == len(run):
            total += group
        else:
            return None
    return total


def spelled_numbers(low: str) -> tuple:
    """``(values, unreadable)`` for lower-cased text: the values of the spelled / slang numbers that
    were read, and whether any number word could not be read with certainty."""
    words = [m.group() for m in _WORD.finditer(_DIGIT_AMOUNT.sub(" # ", low))]
    values, unreadable, i = set(), False, 0
    while i < len(words):
        if _number_word(words[i]) is None:
            i += 1
            continue
        start, run = i, []
        while i < len(words) and _number_word(words[i]) is not None:
            run.append((words[i], *_number_word(words[i])))
            i += 1
        vague = (start > 0 and words[start - 1] in _VAGUE_BEFORE) or \
            any(w.startswith("половин") for w in words[i:i + 2])       # "два с половиной миллиона"
        value = None if vague or any(kind == "?" for _, kind, _ in run) else _read_run(run)
        if value is None:
            unreadable = True
        else:
            values.add(value)
    return values, unreadable


NUMERIC_ARGS = {"min_price": "unsupported_price_value", "max_price": "unsupported_price_value",
                "screen_size_inches": "unsupported_size_value", "min_screen_size_inches": "unsupported_size_value",
                "max_screen_size_inches": "unsupported_size_value",
                "min_refresh_rate_hz": "unsupported_refresh_rate_value"}
USE_CASE_FOR = {Preference.GAMING: "gaming", Preference.MOVIES: "movies", Preference.AUDIO: "sound",
                Preference.BRIGHT_ROOM: "bright_room"}


@dataclass(frozen=True)
class MessageEvidence:
    """What one user message states. Derived facts only; the text itself is not kept."""
    numbers: frozenset             # every number in the text (digits, spelled, slang), also x1 000 and x1 000 000
    spelled_numbers: bool          # a number word the guard could not read ("пара сотен"): numeric rules off
    features: frozenset            # registry feature ids mentioned
    use_cases: tuple               # use cases implied by the text (via QuerySemantics preferences)
    semantics_ok: bool

    @staticmethod
    def from_text(text: str) -> "MessageEvidence":
        low = str(text).lower().replace("ё", "е")
        numbers = set()
        for m in _NUMBER.finditer(low):
            v = float(re.sub(r"[   ]", "", m.group()).replace(",", "."))
            numbers |= {v, v * 1_000, v * 1_000_000}
        parse = parse_query_semantics(str(text))
        use_cases = tuple(USE_CASE_FOR[p] for p in parse.semantics.preferences if p in USE_CASE_FOR) if parse.ok else ()
        read, unreadable = spelled_numbers(low)                           # "150 тысяч" is a digit amount
        for v in read:
            numbers |= {v, v * 1_000, v * 1_000_000}
        return MessageEvidence(frozenset(numbers), unreadable,
                               frozenset(f for f, p in FEATURE_MENTIONS.items() if re.search(p, low)),
                               use_cases, parse.ok)


@dataclass(frozen=True)
class GuardAction:
    argument: str
    action: str                     # "removed" | "added" | "would_remove"
    value: Any
    reason: str


@dataclass(frozen=True)
class GuardResult:
    """``status``: ``unchanged`` | ``modified`` | ``report_only`` | ``skipped`` | ``fallback``.
    ``arguments`` is always safe to pass on: the guarded arguments for ``modified``, otherwise the
    original arguments object."""
    status: str
    arguments: Any
    actions: tuple = ()
    detail: Optional[str] = None
    version: str = GUARD_VERSION

    @property
    def removed(self) -> tuple:
        return tuple(a for a in self.actions if a.action == "removed")

    def summary(self) -> dict:
        """Loggable: argument names, values and reason codes; never user text."""
        return {"guard": self.status, "detail": self.detail,
                "actions": [[a.action, a.argument, a.value, a.reason] for a in self.actions]}


def _valid(tool: str, args: dict) -> Optional[str]:
    try:
        validate_arguments(tool, args)
        if tool in ("search_tvs", "recommend_tvs", "get_catalog_stats"):
            constraints_from_args(args)
    except ToolArgumentError as e:
        return "; ".join(e.errors)[:200]
    return None


def _rules(tool: str, args: dict, conversation: Sequence[MessageEvidence]) -> tuple:
    """Pure rule pass: ``(new_args, actions)``. Only removes, plus the use-case merge."""
    numbers = frozenset().union(*(m.numbers for m in conversation))
    features = frozenset().union(*(m.features for m in conversation))
    spelled = any(m.spelled_numbers for m in conversation)
    out, actions = dict(args), []

    if not spelled:
        for key, reason in NUMERIC_ARGS.items():
            if key in out and float(out[key]) not in numbers:
                actions.append(GuardAction(key, "removed", out.pop(key), reason))
        if "price_basis" in out and not ({"min_price", "max_price"} & set(out)) \
                and any(a.argument in ("min_price", "max_price") for a in actions):
            actions.append(GuardAction("price_basis", "removed", out.pop("price_basis"), "no_price_bound_left"))

    if "required_features" in out:
        kept = [f for f in out["required_features"] if f in features]
        for f in out["required_features"]:
            if f not in features:
                actions.append(GuardAction("required_features", "removed", f, "required_feature_not_mentioned"))
        if kept:
            out["required_features"] = kept
        else:
            out.pop("required_features")

    if actions and tool == "recommend_tvs":
        have = list(out.get("use_cases", ()))
        for uc in dict.fromkeys(uc for m in conversation for uc in m.use_cases):
            if uc not in have:
                have.append(uc)
                actions.append(GuardAction("use_cases", "added", uc, "preference_signal_kept"))
        if have:
            out["use_cases"] = have
    return out, tuple(actions)


def guard_tool_arguments(tool: str, arguments: Any, conversation: Optional[Sequence] = None,
                         act: bool = True) -> GuardResult:
    """``conversation``: the user's messages so far (``MessageEvidence`` or ``str``), current last.
    ``act=False`` computes the same decisions but applies none (``would_remove``) -- for a
    conversation whose earlier messages may be missing. Never raises."""
    try:
        if not conversation:
            return GuardResult("skipped", arguments, detail="no_conversation_context")
        if not isinstance(arguments, dict) or _valid(tool, {k: v for k, v in arguments.items() if v is not None}):
            return GuardResult("skipped", arguments, detail="original_arguments_rejected_by_tool_boundary")
        conv = [m if isinstance(m, MessageEvidence) else MessageEvidence.from_text(m) for m in conversation]
        original = {k: v for k, v in arguments.items() if v is not None}
        guarded, actions = _rules(tool, original, conv)
        if not actions:
            return GuardResult("unchanged", arguments)
        if not act:
            return GuardResult("report_only", arguments, tuple(
                GuardAction(a.argument, "would_remove" if a.action == "removed" else a.action, a.value, a.reason)
                for a in actions), detail="conversation_may_be_incomplete")
        problem = _valid(tool, guarded)
        if problem:
            return GuardResult("fallback", arguments, actions, detail=f"guarded_arguments_invalid: {problem}")
        return GuardResult("modified", guarded, actions)
    except Exception as e:                      # additive safety: never a new point of failure
        return GuardResult("fallback", arguments, detail=f"guard_error:{type(e).__name__}")


def not_applied_note(result: GuardResult) -> Optional[dict]:
    """Agent-facing, compact: which proposed constraints were not applied and why."""
    removed = result.removed if result.status == "modified" else ()
    if not removed:
        return None
    return {"constraints": [{"argument": a.argument, "value": a.value} for a in removed],
            "note": "Not stated by the user in this conversation, so not applied. Do not present the results as "
                    "satisfying these constraints; ask the user if a limit is needed. A feature listed here was not "
                    "required: a product has it only if its `features` says yes. Limits the user did state must be "
                    "passed exactly as stated."}
