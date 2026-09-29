"""Samsung AI Consultant -- deterministic domain core and Agent tools.

Phase 4B: plan -> route -> typed catalog retrieval -> feature evaluation -> ranking -> shortlist.
Phase 4C: evidence bundle, semantic passages, gaps, relaxation, confidence.
Phase 4D: five closed Agent tools over that core (``agent_tools``), the Agent-facing result
contract (``agent_payload``) and an MCP adapter (``mcp_server``) for the n8n AI Agent.
The package never calls an LLM or an embedding API. See docs/PHASE_4A_AI_CONSULTANT_ARCHITECTURE.md
and docs/adr/004-agent-runtime-and-tool-boundary.md.
"""
