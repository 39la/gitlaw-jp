"""Shared helpers used by every :class:`BulkSource`."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..config import RunConfig, serialize_path
from ..contracts import IngestManifest, SnapshotManifest

logger = logging.getLogger(__name__)

_JST = timezone(timedelta(hours=9))

NORMALIZE_STAGE_DIR = "03_normalize_versions"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_zip_entries(entries: list[str]) -> list[str]:
    return sorted(entry.replace("\\", "/") for entry in entries)


def build_ingest_manifest(
    *,
    zip_path: Path,
    xml_entries: list[str],
    csv_entry: str = "",
    source_name: str = "egov",
) -> IngestManifest:
    if not zip_path.exists():
        raise FileNotFoundError(f"input zip not found: {zip_path}")
    if not xml_entries:
        raise ValueError("no xml files found in input zip")

    input_sha = sha256_file(zip_path)
    dataset_id = input_sha[:12]
    run_id = f"{datetime.now(_JST).strftime('%Y%m%dT%H%M%S')}_{dataset_id}"
    return IngestManifest(
        dataset_id=dataset_id,
        run_id=run_id,
        input_zip=serialize_path(zip_path),
        input_sha256=input_sha,
        csv_entry=csv_entry,
        xml_entry_count=len(xml_entries),
        xml_entries_sample=xml_entries[:10],
        source_name=source_name,
    )


def write_normalized_versions(
    *,
    config: RunConfig,
    ingest_manifest: IngestManifest,
    records: list[dict[str, object]],
    source_name: str,
    parse_xml: bool,
) -> SnapshotManifest:
    """Write the canonical ``law_versions.jsonl`` and its manifest.

    Shared by every :class:`BulkSource` so the output contract stays
    identical regardless of upstream layout.
    """

    records.sort(
        key=lambda record: (
            str(record.get("law_id", "")),
            str(record.get("revision_id", "")),
        )
    )

    out_dir = (
        config.output_root
        / ingest_manifest.run_id
        / "stages"
        / NORMALIZE_STAGE_DIR
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    out_jsonl = out_dir / "law_versions.jsonl"
    with out_jsonl.open("w", encoding="utf-8", newline="\n") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            f.write("\n")

    manifest = SnapshotManifest(
        dataset_id=ingest_manifest.dataset_id,
        run_id=ingest_manifest.run_id,
        source_csv_entry=ingest_manifest.csv_entry,
        record_count=len(records),
        output_jsonl=serialize_path(out_jsonl),
        xml_parsed_count=len(records) if parse_xml else 0,
        xml_parse_policy="strict_fail_fast",
        source_name=source_name,
    )
    out_manifest = out_dir / "manifest.json"
    with out_manifest.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(
            manifest.to_dict(),
            f,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        f.write("\n")

    return manifest
