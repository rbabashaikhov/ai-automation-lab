"""Sanitization of n8n workflow JSON before it is printed, saved, or committed.

Any dict key that looks like it could hold a secret (API keys, tokens,
passwords, authorization headers, cookies, ...) has its value replaced with
REDACTED, regardless of where it appears in the structure. The ``credentials``
key itself is *not* blanket-redacted: n8n's public API only ever exposes a
credential's id/name reference there (never the decrypted secret), and that
reference is useful for analysis, so it is preserved unless a nested key
independently looks sensitive.
"""

from __future__ import annotations

import copy
import re
from typing import Any

REDACTED = "***REDACTED***"

# Known sensitive field-name fragments (case-insensitive). Deliberately does
# NOT include "credential" — see module docstring.
_SENSITIVE_FRAGMENTS = (
    r"api[_-]?key",
    r"access[_-]?token",
    r"refresh[_-]?token",
    r"client[_-]?secret",
    r"webhook[_-]?secret",
    r"private[_-]?key",
    r"password",
    r"passwd",
    r"secret",
    r"token",
    r"authorization",
    r"cookie",
    r"bearer",
)
SENSITIVE_KEY_PATTERN = re.compile("|".join(_SENSITIVE_FRAGMENTS), re.IGNORECASE)


def is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_PATTERN.search(str(key)))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, sub_value in value.items():
            if is_sensitive_key(key):
                result[key] = REDACTED
            else:
                result[key] = _sanitize(sub_value)
        return result
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def sanitize_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """Return a deep-copied, sanitized version of a workflow dict."""
    return _sanitize(copy.deepcopy(workflow))


def find_sensitive_paths(data: Any, _path: str = "") -> list[str]:
    """Return dotted paths of keys that look sensitive, for reporting/logging."""
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{_path}.{key}" if _path else str(key)
            if is_sensitive_key(key):
                found.append(path)
            else:
                found.extend(find_sensitive_paths(value, path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            found.extend(find_sensitive_paths(item, f"{_path}[{index}]"))
    return found
