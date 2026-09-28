#!/usr/bin/env bash
# run_db_tests.sh
#
# Spins up a disposable pgvector/pgvector:0.8.6-pg16-bookworm container
# (same image family as the VPS and as db/test/run_local_tests.sh),
# applies db/migrations/, exposes it on a local port, and runs the
# repository/idempotency tests (tests/test_repository.py,
# tests/test_idempotency.py) against it via SAMSUNG_TEST_DATABASE_URL.
# The container is removed on exit regardless of pass/fail. Nothing here
# ever touches the VPS or samsung_rag.
#
# Usage: projects/samsung-ai-consultant/tests/run_db_tests.sh
# Requires: docker (set DOCKER_HOST=unix:///var/run/docker.sock if the
# active context points at a non-running Docker Desktop socket).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
DB_DIR="${PROJECT_DIR}/db"
IMAGE="pgvector/pgvector:0.8.6-pg16-bookworm"
CONTAINER_NAME="samsung_rag_ingestion_test_$$"
DB_NAME="samsung_rag_test"
DB_USER="samsung_rag_test"
DB_PASSWORD="samsung_rag_test_local_only"
HOST_PORT="${SAMSUNG_TEST_DB_PORT:-55432}"

cleanup() {
    docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Starting disposable PostgreSQL+pgvector container (${IMAGE})"
docker run -d --name "${CONTAINER_NAME}" \
    -e "POSTGRES_DB=${DB_NAME}" \
    -e "POSTGRES_USER=${DB_USER}" \
    -e "POSTGRES_PASSWORD=${DB_PASSWORD}" \
    -p "127.0.0.1:${HOST_PORT}:5432" \
    "${IMAGE}" >/dev/null

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

echo "==> Applying migrations"
for f in "${DB_DIR}"/migrations/*.sql; do
    echo "   applying $(basename "${f}")"
    docker exec -i -e "PGPASSWORD=${DB_PASSWORD}" "${CONTAINER_NAME}" \
        psql -v ON_ERROR_STOP=1 -U "${DB_USER}" -d "${DB_NAME}" -f - < "${f}"
done

export SAMSUNG_TEST_DATABASE_URL="postgresql://${DB_USER}:${DB_PASSWORD}@127.0.0.1:${HOST_PORT}/${DB_NAME}"

echo "==> Running repository/idempotency tests"
cd "${PROJECT_DIR}"
python3 -m pytest \
    tests/test_repository.py tests/test_idempotency.py \
    tests/test_indexing_repository.py tests/test_indexing_service.py \
    tests/test_indexing_embedding_sync.py \
    -v

echo "==> All DB-backed tests passed"
