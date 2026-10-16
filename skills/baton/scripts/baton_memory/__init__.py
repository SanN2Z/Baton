"""Baton v2 project memory.

The package deliberately keeps the ledger as the source of truth.  Mem0 is an
optional, rebuildable index and is imported lazily so the stdlib-only Baton CLI
and the test suite can use the ledger without installing the venv dependencies.
"""

from .ledger import MemoryLedger, memory_append_approved
from .search import (
    fuse_results,
    keyword_search,
    render_memory_context,
    render_pinned_block,
    retrieve_memory,
)

__all__ = [
    "MemoryLedger",
    "memory_append_approved",
    "fuse_results",
    "keyword_search",
    "render_memory_context",
    "render_pinned_block",
    "retrieve_memory",
]

