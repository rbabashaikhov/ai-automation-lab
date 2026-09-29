"""Secret provisioning for the Samsung Consultant runtime (Phase 4D.2A). Stdlib only; runs on the VPS.

    python3 provision_env.py init-env /root/samsung-consultant/consultant.env
        Create the env file (mode 600) with a generated DB password and MCP token. Refuses to
        overwrite an existing file. Prints nothing secret.

    python3 provision_env.py psql-preamble /root/samsung-consultant/consultant.env | cat - create_consultant_role.sql | psql ...
        Emit ``\\set password_verifier '<SCRAM-SHA-256 verifier>'`` for create_consultant_role.sql, so
        PostgreSQL only ever receives a verifier, never the plaintext password. Meant to be piped,
        never displayed.

    python3 provision_env.py mcp-token /root/samsung-consultant/consultant.env | <consumer reading stdin>
        Emit the MCP token (for creating the n8n credential through a pipe). Never display it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sys
from pathlib import Path

DB_USER = "samsung_consultant"
DB_HOST = "postgres"            # Docker service name on n8n-compose_default
DB_NAME = "samsung_rag"
SCRAM_ITERATIONS = 4096


def scram_sha256_verifier(password: str, salt: bytes = None, iterations: int = SCRAM_ITERATIONS) -> str:
    """PostgreSQL SCRAM-SHA-256 verifier (RFC 5802/7677 as stored in pg_authid.rolpassword)."""
    salt = salt or os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode("ascii")      # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def read_env(path: Path) -> dict:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def init_env(path: Path) -> int:
    if path.exists():
        print(f"{path} already exists; not overwriting", file=sys.stderr)
        return 1
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    password, token = secrets.token_hex(32), secrets.token_hex(32)
    body = (f"CONSULTANT_DATABASE_URL=postgresql://{DB_USER}:{password}@{DB_HOST}:5432/{DB_NAME}\n"
            f"CONSULTANT_MCP_TOKEN={token}\n"
            "CONSULTANT_MAX_TOOL_CALLS_PER_TURN=3\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"created {path} (mode 600): CONSULTANT_DATABASE_URL, CONSULTANT_MCP_TOKEN, CONSULTANT_MAX_TOOL_CALLS_PER_TURN")
    return 0


def _password(env: dict) -> str:
    url = env["CONSULTANT_DATABASE_URL"]
    creds = url.split("://", 1)[1].split("@", 1)[0]
    user, password = creds.split(":", 1)
    if user != DB_USER:
        raise SystemExit("unexpected DB user in CONSULTANT_DATABASE_URL")
    return password


def main(argv: list) -> int:
    if len(argv) != 2 or argv[0] not in ("init-env", "psql-preamble", "mcp-token"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd, path = argv[0], Path(argv[1])
    if cmd == "init-env":
        return init_env(path)
    if sys.stdout.isatty():
        print("refusing to write a secret to a terminal; pipe the output", file=sys.stderr)
        return 2
    env = read_env(path)
    if cmd == "psql-preamble":
        sys.stdout.write(f"\\set password_verifier '{scram_sha256_verifier(_password(env))}'\n")
    else:
        sys.stdout.write(env["CONSULTANT_MCP_TOKEN"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
