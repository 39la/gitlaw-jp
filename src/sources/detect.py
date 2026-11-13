"""Source auto-detection and selection."""

from __future__ import annotations

import logging
from pathlib import Path
from zipfile import BadZipFile

from .base import BulkSource
from .egov import EgovBulkSource
from .lawdb import LawdbBulkSource

logger = logging.getLogger(__name__)

SOURCES: tuple[BulkSource, ...] = (EgovBulkSource(), LawdbBulkSource())


def detect_source(zip_path: Path) -> BulkSource:
    """Pick the first source that claims to handle ``zip_path``."""

    for source in SOURCES:
        try:
            if source.detect(zip_path):
                logger.info("Auto-detected bulk source: %s", source.name)
                return source
        except (FileNotFoundError, KeyError, OSError, BadZipFile) as exc:
            logger.debug("Source %s detection failed: %s", source.name, exc)
    raise ValueError(f"could not auto-detect a known bulk source for: {zip_path}")


def resolve_source(name: str, zip_path: Path) -> BulkSource:
    """Resolve a source by explicit name, falling back to auto-detect."""

    if name in ("", "auto"):
        return detect_source(zip_path)
    for source in SOURCES:
        if source.name == name:
            return source
    raise ValueError(
        f"unknown source: {name!r} (known: {', '.join(s.name for s in SOURCES)})"
    )
