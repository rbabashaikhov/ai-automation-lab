"""Shared fixtures for tests that need a real PostgreSQL connection.

These tests are skipped unless SAMSUNG_TEST_DATABASE_URL is set, so a
plain `pytest` run (no database available) still passes cleanly.
`tests/run_db_tests.sh` sets it against a disposable Docker container --
see that script and projects/samsung-ai-consultant/db/test/run_local_tests.sh,
which this mirrors, for the pattern.
"""

from __future__ import annotations

import os

import psycopg2
import pytest

TEST_DATABASE_URL = os.environ.get("SAMSUNG_TEST_DATABASE_URL", "").strip()

requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="SAMSUNG_TEST_DATABASE_URL not set -- run tests/run_db_tests.sh for DB-backed tests",
)


@pytest.fixture
def db_conn():
    conn = psycopg2.connect(TEST_DATABASE_URL)
    yield conn
    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE products, product_specs, ingestion_runs, ingestion_errors RESTART IDENTITY CASCADE;")
    conn.commit()
    conn.close()
