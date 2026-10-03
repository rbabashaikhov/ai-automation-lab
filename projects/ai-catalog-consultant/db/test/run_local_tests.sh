#!/usr/bin/env bash
# run_local_tests.sh
#
# Spins up a disposable pgvector/pgvector:0.8.6-pg16-bookworm container
# (the same image family running on the VPS), applies db/migrations/ in
# order, loads the synthetic smoke-test dataset, and runs db/test/assertions.sql.
# Also re-applies the migrations a second time to confirm they are safe to
# re-run. Everything happens in a throwaway container; nothing here ever
# touches the VPS or any shared database.
#
# Usage: db/test/run_local_tests.sh
# Requires: docker

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE="pgvector/pgvector:0.8.6-pg16-bookworm"
CONTAINER_NAME="samsung_rag_schema_test_$$"
DB_NAME="samsung_rag_test"
DB_USER="samsung_rag_test"
DB_PASSWORD="samsung_rag_test_local_only"

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Starting disposable PostgreSQL+pgvector container (${IMAGE})"
docker run -d --name "${CONTAINER_NAME}" \
    -e "POSTGRES_DB=${DB_NAME}" \
    -e "POSTGRES_USER=${DB_USER}" \
    -e "POSTGRES_PASSWORD=${DB_PASSWORD}" \
    "${IMAGE}" >/dev/null

psql_exec() {
    docker exec -i \
        -e "PGPASSWORD=${DB_PASSWORD}" \
        "${CONTAINER_NAME}" \
        psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${DB_NAME}" "$@"
}

echo "==> Waiting for PostgreSQL to accept connections"
for _ in $(seq 1 30); do
    if docker exec "${CONTAINER_NAME}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
if ! docker exec "${CONTAINER_NAME}" pg_isready -U "${DB_USER}" -d "${DB_NAME}" >/dev/null 2>&1; then
    echo "PostgreSQL did not become ready in time" >&2
    exit 1
fi

apply_migrations() {
    for f in "${DB_DIR}"/migrations/*.sql; do
        echo "   applying $(basename "${f}")"
        psql_exec -f - < "${f}"
    done
}

echo "==> Applying migrations (1st pass)"
apply_migrations

echo "==> Loading synthetic smoke-test dataset"
psql_exec -f - < "${DB_DIR}/seed/001_smoke_test_data.sql"

echo "==> Running assertions"
psql_exec -f - < "${DB_DIR}/test/assertions.sql"

echo "==> Re-applying migrations (2nd pass, idempotency check)"
apply_migrations

echo "==> Verifying re-applied migrations did not disturb existing data"
SMOKE_COUNT="$(psql_exec -tAc "SELECT count(*) FROM products WHERE source = 'smoke_test';")"
if [ "${SMOKE_COUNT}" != "3" ]; then
    echo "Expected 3 smoke_test products after re-applying migrations, found ${SMOKE_COUNT}" >&2
    exit 1
fi
echo "   OK: smoke_test product count unchanged (${SMOKE_COUNT})"

echo "==> All local schema tests passed"
