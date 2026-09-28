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
from dataclasses import dataclass
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


# Recognizable secret *values*, independent of the key they're stored under —
# catches a real key hardcoded into an innocuously-named field (e.g. a "url"
# or "message" parameter), which key-name scanning alone would miss.
_VALUE_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("OpenAI API key", re.compile(r"sk-(proj-)?[A-Za-z0-9_-]{20,}")),
    ("AWS access key ID", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    (
        "JWT / Supabase-style service key",
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    ),
    ("PEM private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9\-_.~+/]{10,}=*", re.IGNORECASE)),
    ("Basic auth header value", re.compile(r"\bBasic\s+[A-Za-z0-9+/]{10,}={0,2}", re.IGNORECASE)),
    (
        "credentials embedded in a URL",
        re.compile(r"[a-z][a-z0-9+.-]*://[^\s:@/]+:[^\s:@/]+@"),
    ),
)


def scan_value_for_secret_patterns(value: str) -> list[str]:
    """Return labels of any known secret-shaped patterns found in ``value``."""
    return [label for label, pattern in _VALUE_SECRET_PATTERNS if pattern.search(value)]


@dataclass(frozen=True)
class SecretFinding:
    path: str
    reason: str


def scan_workflow_for_secrets(data: Any, _path: str = "") -> list[SecretFinding]:
    """Recursively scan a workflow for likely embedded secrets.

    Flags both sensitive-looking *keys* (same rule as sanitize_workflow) and
    string *values* that match a known secret pattern (OpenAI/AWS/Google/
    Slack keys, JWT-style service keys such as Supabase's, PEM private keys,
    Bearer/Basic auth headers, credentials embedded in a URL). Does not
    include the matched secret text itself in the finding.
    """
    findings: list[SecretFinding] = []
    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{_path}.{key}" if _path else str(key)
            if is_sensitive_key(key):
                findings.append(SecretFinding(path, f"key name looks sensitive ({key})"))
                continue
            findings.extend(scan_workflow_for_secrets(value, path))
    elif isinstance(data, list):
        for index, item in enumerate(data):
            findings.extend(scan_workflow_for_secrets(item, f"{_path}[{index}]"))
    elif isinstance(data, str):
        for label in scan_value_for_secret_patterns(data):
            findings.append(SecretFinding(_path, f"value matches pattern: {label}"))
    return findings
