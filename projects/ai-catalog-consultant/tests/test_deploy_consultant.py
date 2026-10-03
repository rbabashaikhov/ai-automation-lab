"""Phase 4D.2A deployment artifacts: the internal-only container/compose contract, secret
provisioning and the read-only DB role (role tests run on the disposable DB only)."""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest

yaml = pytest.importorskip("yaml")

from .consultant_fixtures import seed
from .db_fixtures import TEST_DATABASE_URL, db_conn, requires_db  # noqa: F401

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "consultant"
sys.path.insert(0, str(DEPLOY))
import provision_env  # noqa: E402


# ---- compose / Dockerfile contract (no DB) ----------------------------------------------------------

def test_compose_service_is_internal_only():
    doc = yaml.safe_load((DEPLOY / "compose.yml").read_text(encoding="utf-8"))
    assert doc["name"] == "samsung-consultant" and list(doc["services"]) == ["samsung-consultant"]
    svc = doc["services"]["samsung-consultant"]
    for forbidden in ("ports", "network_mode", "privileged", "volumes", "cap_add", "devices", "pid", "ipc",
                      "extra_hosts", "build"):
        assert forbidden not in svc, forbidden
    assert svc["networks"] == ["n8n"]
    assert doc["networks"] == {"n8n": {"name": "n8n-compose_default", "external": True}}
    assert svc["read_only"] is True and svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["env_file"] == ["consultant.env"]
    labels = svc.get("labels", {})
    assert not [k for k in labels if re.match(r"traefik\.(http|tcp|udp)\.", k)] and labels.get("traefik.enable") == "false"
    assert "docker.sock" not in (DEPLOY / "compose.yml").read_text()


def test_dockerfile_runs_unprivileged_in_container_mode():
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER 10001$", text, re.M)
    assert '"--container", "--host", "0.0.0.0", "--port", "8765"' in text
    assert "HEALTHCHECK" in text and "/healthz" in text
    ignore = (DEPLOY / "Dockerfile.dockerignore").read_text(encoding="utf-8").splitlines()
    assert ignore[1] == "*" and "!consultant/" in ignore
    allowed = [line for line in ignore if line.startswith("!")]
    assert allowed == ["!consultant/", "!indexing/__init__.py", "!indexing/metadata.py", "!indexing/models.py"]


def test_env_example_has_no_secret_and_real_env_is_ignored():
    example = (DEPLOY / "consultant.env.example").read_text(encoding="utf-8")
    assert "<generated-on-vps>" in example and not re.search(r"[0-9a-f]{32}", example)
    root = Path(__file__).resolve().parents[3]
    out = subprocess.run(["git", "check-ignore", "-q", str(DEPLOY / "consultant.env")], cwd=root)
    assert out.returncode == 0


# ---- secret provisioning (no DB) ------------------------------------------------------------------------

def test_init_env_creates_private_file_and_refuses_overwrite(tmp_path, capsys):
    path = tmp_path / "sub" / "consultant.env"
    assert provision_env.init_env(path) == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    env = provision_env.read_env(path)
    assert re.fullmatch(r"postgresql://samsung_consultant:[0-9a-f]{64}@postgres:5432/samsung_rag",
                        env["CONSULTANT_DATABASE_URL"])
    assert re.fullmatch(r"[0-9a-f]{64}", env["CONSULTANT_MCP_TOKEN"])
    printed = capsys.readouterr().out
    assert env["CONSULTANT_MCP_TOKEN"] not in printed and provision_env._password(env) not in printed
    assert provision_env.init_env(path) == 1


def test_scram_verifier_format():
    v = provision_env.scram_sha256_verifier("pw", salt=b"\x00" * 16)
    assert re.fullmatch(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]{24}\$[A-Za-z0-9+/=]{44}:[A-Za-z0-9+/=]{44}", v)
    assert v == provision_env.scram_sha256_verifier("pw", salt=b"\x00" * 16)
    assert v != provision_env.scram_sha256_verifier("pw2", salt=b"\x00" * 16)


def test_secret_commands_refuse_a_terminal(tmp_path, monkeypatch):
    path = tmp_path / "consultant.env"
    provision_env.init_env(path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert provision_env.main(["mcp-token", str(path)]) == 2
    assert provision_env.main(["psql-preamble", str(path)]) == 2


# ---- read-only role (disposable DB only) ---------------------------------------------------------------

def _psql_preamble_role(conn, password: str) -> None:
    sql = (DEPLOY / "create_consultant_role.sql").read_text(encoding="utf-8")
    verifier = provision_env.scram_sha256_verifier(password)
    body = sql.split("BEGIN;", 1)[1].rsplit("COMMIT;", 1)[0].replace(":'password_verifier'", "%s")
    body = body.replace("samsung_rag", conn.info.dbname)
    with conn.cursor() as cur:
        cur.execute(body, (verifier,))
    conn.commit()


@requires_db
def test_consultant_role_can_read_and_cannot_write(db_conn):  # noqa: F811
    seed(db_conn)
    password = "p" * 40
    with db_conn.cursor() as cur:
        cur.execute("DROP ROLE IF EXISTS samsung_consultant")
    db_conn.commit()
    try:
        _psql_preamble_role(db_conn, password)
        info = db_conn.info
        role = psycopg2.connect(host=info.host, port=info.port, dbname=info.dbname,
                                user="samsung_consultant", password=password)   # SCRAM verifier accepted
        cur = role.cursor()
        cur.execute("SELECT current_setting('transaction_read_only'), count(*) FROM products")
        assert cur.fetchone() == ("on", 31)
        role.rollback()
        for sql, read_write in (("UPDATE products SET price = price WHERE false", False),
                                ("UPDATE products SET price = price WHERE false", True),
                                ("DELETE FROM chunks WHERE false", True),
                                ("SELECT count(*) FROM documents", False),
                                ("CREATE TABLE public.write_probe (i int)", True)):
            with pytest.raises(psycopg2.Error) as e:
                if read_write:
                    cur.execute("SET TRANSACTION READ WRITE")
                cur.execute(sql)
            assert e.value.pgcode in ("25006", "42501"), sql     # read_only_sql_transaction / insufficient_privilege
            role.rollback()
        role.close()
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DROP OWNED BY samsung_consultant")
            cur.execute("DROP ROLE IF EXISTS samsung_consultant")
        db_conn.commit()
