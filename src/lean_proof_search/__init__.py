"""Model-independent proof-search interfaces with a bundled reference backend."""

from typing import Literal, TypedDict

from .search.controller import NativeSession, SearchBudget, search as run_search
from .lean.tasks import _validate as verify

__version__ = "0.1.0"


class ProofTask(TypedDict):
    """Original declaration and context; candidate code never changes these."""

    id: str
    prefix: str
    header: str
    ingest_status: Literal["parsed"]


__all__ = ["NativeSession", "ProofTask", "SearchBudget", "run_search", "verify"]
