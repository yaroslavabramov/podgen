"""Search and read the admitted source corpus within a writing or fact-checking invocation."""

from .contracts import definitions
from .corpus import SourceCorpus
from .handoff import attach_task_passages, resolve_review_reads

__all__ = ["SourceCorpus", "attach_task_passages", "definitions", "resolve_review_reads"]
