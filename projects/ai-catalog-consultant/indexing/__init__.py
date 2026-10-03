"""Samsung RAG document/chunk indexing pipeline (Phase 3A -- no embeddings).

Builds `documents` / `chunks` rows deterministically from `products` /
`product_specs`. Never calls an embeddings API and never writes to the
`embedding` column -- see `repository.py` module docstring. See
projects/samsung-ai-consultant/indexing/README.md for design and usage.
"""
