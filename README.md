# AI Automation Lab

Production-oriented experiments and projects around AI automation, n8n,
RAG, AI agents, LLM integrations, PostgreSQL/pgvector, and workflow
engineering.

This repository is a multi-project lab, not a single application. It
contains reusable tooling under `tools/` and independent, self-contained
projects under `projects/`. Each project has its own README, status, and
scope; the tools are shared infrastructure any project may use.

```text
ai-automation-lab/
├── tools/
│   └── n8n-tool/              # reusable n8n workflow-as-code CLI
└── projects/
    └── ai-catalog-consultant/ # AI Catalog Consultant (RAG + agent)
```

## Tools

### [n8n Tool](tools/n8n-tool/)

Git-friendly CLI for managing n8n workflows through the official Public
REST API: read-only discovery/export, plus validated, diffed, and
confirmed create/update/deploy. No delete, no activation, secret
scanning on every write. See
[tools/n8n-tool/README.md](tools/n8n-tool/README.md) for setup and the
full command reference.

## Projects

### [AI Catalog Consultant](projects/ai-catalog-consultant/)

Production-like AI catalog consultant with deterministic ingestion,
PostgreSQL/pgvector, structured retrieval, semantic evidence, MCP tools,
n8n orchestration and formal evaluation. The reference dataset is a real
retailer's TV catalog; the consultant runs live behind a Telegram bot.

Status: completed portfolio MVP and reference implementation. See
[projects/ai-catalog-consultant/README.md](projects/ai-catalog-consultant/README.md)
for the case study,
[docs/ARCHITECTURE.md](projects/ai-catalog-consultant/docs/ARCHITECTURE.md)
for the architecture and
[docs/PROJECT_CLOSURE.md](projects/ai-catalog-consultant/docs/PROJECT_CLOSURE.md)
for the closure record.
