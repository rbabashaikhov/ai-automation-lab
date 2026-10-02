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
    └── samsung-ai-consultant/ # Samsung TV AI Consultant (RAG + agent)
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

### [Samsung AI Consultant](projects/samsung-ai-consultant/)

Production-like AI product consultant for Samsung TVs on a real catalog:
deterministic ingestion, PostgreSQL + pgvector, a tool-using LLM agent
orchestrated in n8n, and a formal evaluation trail.

Status: backend MVP complete and frozen. Ready for a portfolio or
controlled client demo with one documented limitation; strict production
acceptance is on hold. See
[projects/samsung-ai-consultant/README.md](projects/samsung-ai-consultant/README.md)
for the case study,
[docs/ARCHITECTURE.md](projects/samsung-ai-consultant/docs/ARCHITECTURE.md)
for the architecture and
[docs/PROJECT_CLOSURE.md](projects/samsung-ai-consultant/docs/PROJECT_CLOSURE.md)
for the closure record.
