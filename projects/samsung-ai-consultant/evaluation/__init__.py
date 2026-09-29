"""Retrieval evaluation harness (Phase 3D.1).

Pure, deterministic and independent of the production indexing/retrieval code:
it loads ``retrieval_cases.json``, validates it, and scores *supplied*
retrieval results. It never calls a database or an embeddings API.
"""
