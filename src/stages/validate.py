from __future__ import annotations

import json
import logging
from pathlib import Path

from ..config import RunConfig
from ..contracts import IngestManifest, ValidationManifest
from ..sources import resolve_source

logger = logging.getLogger(__name__)


def validate_input(
    config: RunConfig, ingest_manifest: IngestManifest
) -> ValidationManifest:
    logger.info("Starting validation...")
    source = resolve_source(ingest_manifest.source_name, config.input_zip)
    return source.validate(config, ingest_manifest)


def write_validation_manifest(config: RunConfig, manifest: ValidationManifest) -> Path:
    out_dir = config.output_root / manifest.run_id / "stages" / "02_validate"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / "manifest.json"
    with out_file.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")

    return out_file
