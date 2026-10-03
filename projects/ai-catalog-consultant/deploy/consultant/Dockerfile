# Catalog Consultant MCP runtime (Phase 4D.2A; image name `samsung-consultant` is the deployed identifier): the Phase 4D tool facade behind the MCP adapter.
# Build context: projects/ai-catalog-consultant (see deploy/consultant/README.md).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken

# Only the runtime dependencies of consultant/ (+ indexing/metadata.py, models.py). The cl100k_base encoding is
# fetched at build time so token budgeting matches the tested behaviour without runtime downloads.
RUN pip install --no-cache-dir psycopg2-binary==2.9.12 tiktoken==0.14.0 \
    && python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')" \
    && chmod -R a+rX /opt/tiktoken \
    && useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin consultant

WORKDIR /app
COPY consultant/ consultant/
COPY indexing/__init__.py indexing/metadata.py indexing/models.py indexing/

USER 10001
# Internal container port only; the service must never be published on the host.
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8765/healthz', timeout=3).status == 200 else 1)"]
# --require-turn-key: tool calls must carry ?turn=<n8n execution id>, so the per-turn cap spans every MCP
# session of one Agent turn (Gate 4D.2B-R).
ENTRYPOINT ["python", "-m", "consultant.mcp_server", "--container", "--host", "0.0.0.0", "--port", "8765", \
            "--require-turn-key"]
