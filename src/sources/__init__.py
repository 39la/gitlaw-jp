"""Source adapters for upstream law datasets.

Each adapter in this package owns the source-specific I/O and parsing
concerns so the rest of the pipeline can stay source-agnostic. The
public entry points are :func:`resolve_source` (auto-detect or named
selection) and the concrete :class:`EgovBulkSource` /
:class:`LawdbBulkSource` classes.
"""

from .base import BaseBulkSource, BulkSource
from .detect import detect_source, resolve_source
from .egov import EgovBulkSource
from .lawdb import LawdbBulkSource

__all__ = [
    "BaseBulkSource",
    "BulkSource",
    "EgovBulkSource",
    "LawdbBulkSource",
    "detect_source",
    "resolve_source",
]
