from __future__ import annotations

import json
from pathlib import Path

from ..config import RunConfig
from ..contracts import IngestManifest
from ..sources import resolve_source


def ingest_zip(config: RunConfig) -> IngestManifest:
    source = resolve_source(config.source, config.input_zip)
    return source.ingest(config)


def write_ingest_manifest(config: RunConfig, manifest: IngestManifest) -> Path:
    out_dir = config.output_root / manifest.run_id / "stages" / "01_ingest"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_file = out_dir / "manifest.json"
    with out_file.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")

    return out_file
