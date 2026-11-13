from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import time
from pathlib import Path
from zipfile import ZipFile

from ..config import RunConfig
from ..contracts import IngestManifest, LawXmlParser, ParsedLawXml, SnapshotManifest
from ..xml_parser import XsdataLawXmlParser

logger = logging.getLogger(__name__)


def _extract_version_id(url: str) -> str:
    normalized = url.strip().rstrip("/")
    if not normalized:
        return ""
    return normalized.split("/")[-1]


def _read_csv_rows(zip_path: Path, csv_entry: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with ZipFile(zip_path, "r") as zf, zf.open(csv_entry, "r") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        reader = csv.DictReader(text)
        for row in reader:
            normalized_row = {
                (k or "").lstrip("\ufeff").strip(): (v or "").strip()
                for k, v in row.items()
            }
            rows.append(normalized_row)
    return rows


def _normalize_entries(entries: list[str]) -> list[str]:
    return sorted(entry.replace("\\", "/") for entry in entries)


def _resolve_xml_entry(
    entries: list[str],
    entry_lookup: set[str],
    *,
    csv_entry: str,
    law_id: str,
    version_id: str,
) -> str:
    if not law_id:
        raise ValueError("law_id is empty")
    if not version_id:
        raise ValueError(f"version_id is empty: law_id={law_id}")

    file_name = f"{law_id}_{version_id}.xml"
    csv_parent = Path(csv_entry).parent.as_posix()

    candidates: list[str] = []
    if csv_parent and csv_parent != ".":
        candidates.append(f"{csv_parent}/{law_id}_{version_id}/{file_name}")
    candidates.append(f"{law_id}_{version_id}/{file_name}")
    candidates.append(file_name)

    for candidate in candidates:
        if candidate in entry_lookup:
            return candidate

    fallback = sorted(
        name for name in entries if name == file_name or name.endswith(f"/{file_name}")
    )
    if len(fallback) == 1:
        return fallback[0]
    if len(fallback) > 1:
        raise ValueError(
            f"multiple xml entries matched for {law_id}:{version_id}: {fallback[:3]}"
        )

    raise FileNotFoundError(f"xml entry not found for {law_id}:{version_id}")


def _build_xml_mvp(parsed_xml: ParsedLawXml) -> dict[str, object]:
    first_sentence = (
        parsed_xml.structure[0].sentence_text if parsed_xml.structure else ""
    )
    return {
        "law_meta": parsed_xml.law_meta.to_dict(),
        "flags": {
            "delete": parsed_xml.has_delete,
            "hide": parsed_xml.has_hide,
            "extract": parsed_xml.has_extract,
        },
        "amend": {
            "suppl_provisions": [
                item.to_dict() for item in parsed_xml.suppl_provisions
            ],
        },
        "structure_summary": {
            "sentence_count": len(parsed_xml.structure),
            "first_sentence": first_sentence,
        },
    }


def _render_law_markdown(parsed_xml: ParsedLawXml) -> str:
    lines = [
        f"# {parsed_xml.law_meta.law_num}",
        "",
    ]
    for item in parsed_xml.structure:
        article_label = f"第{item.article_num}条" if item.article_num else "本文"
        paragraph_label = f"{item.paragraph_num}項" if item.paragraph_num else ""
        lines.append(
            f"- {article_label} {paragraph_label} {item.sentence_text}".strip()
        )
    if len(lines) == 2:
        lines.append("- （本文抽出なし）")
    return "\n".join(lines) + "\n"


def _truncate_text(value: str, max_embed_bytes: int) -> tuple[str, bool]:
    raw = value.encode("utf-8")
    if len(raw) <= max_embed_bytes:
        return value, False
    truncated = raw[:max_embed_bytes].decode("utf-8", errors="ignore")
    return truncated, True


def _to_snapshot_record(row: dict[str, str]) -> dict[str, object]:
    law_id = row.get("法令ID", "")
    body_url = row.get("本文URL", "")
    version_id = _extract_version_id(body_url)
    return {
        "law_id": law_id,
        "version_id": version_id,
        "law_type": row.get("法令種別", ""),
        "law_num": row.get("法令番号", ""),
        "law_name": row.get("法令名", ""),
        "promulgation_date": row.get("公布日", ""),
        "amendment_law_name": row.get("改正法令名", ""),
        "amendment_law_num": row.get("改正法令番号", ""),
        "amendment_promulgation_date": row.get("改正法令公布日", ""),
        "effective_date": row.get("施行日", ""),
        "effective_note": row.get("施行日備考", ""),
        "is_marked_unenforced": row.get("未施行", "") == "○",
        "source_body_url": body_url,
    }


def create_snapshot_records(
    config: RunConfig,
    ingest_manifest: IngestManifest,
    law_xml_parser: LawXmlParser | None = None,
) -> SnapshotManifest:
    logger.info(
        f"Creating snapshot records from {ingest_manifest.xml_entry_count} XML entries"
    )
    need_xml_parse = config.snapshot_parse_xml
    parser = (law_xml_parser or XsdataLawXmlParser()) if need_xml_parse else None
    logger.debug(
        "Snapshot config: parse_xml=%s, max_embed_kb=%s",
        config.snapshot_parse_xml,
        config.max_embed_kb,
    )
    rows = _read_csv_rows(config.input_zip, ingest_manifest.csv_entry)
    logger.debug("Loaded %s CSV rows from %s", len(rows), ingest_manifest.csv_entry)
    records: list[dict[str, object]] = []
    started_at = time.perf_counter()

    with ZipFile(config.input_zip, "r") as zf:
        entries = _normalize_entries(zf.namelist())
        entry_lookup = set(entries)
        logger.debug("ZIP entry count=%s", len(entries))
        for idx, row in enumerate(rows, start=1):
            record = _to_snapshot_record(row)
            law_id = str(record.get("law_id", ""))
            version_id = str(record.get("version_id", ""))
            xml_entry = _resolve_xml_entry(
                entries,
                entry_lookup,
                csv_entry=ingest_manifest.csv_entry,
                law_id=law_id,
                version_id=version_id,
            )
            with zf.open(xml_entry, "r") as raw:
                xml_bytes = raw.read()
                parsed_xml = None
                if parser is not None:
                    parsed_xml = parser.parse(
                        law_id=law_id,
                        version_id=version_id,
                        xml_bytes=xml_bytes,
                    )

            xml_sha256 = hashlib.sha256(xml_bytes).hexdigest()

            record["xml_entry"] = xml_entry
            record["xml_sha256"] = f"sha256:{xml_sha256}"
            if config.snapshot_parse_xml:
                if parsed_xml is None:
                    raise RuntimeError(
                        "parsed_xml is required when snapshot_parse_xml is enabled"
                    )
                record["xml_mvp"] = _build_xml_mvp(parsed_xml)
            records.append(record)

            if idx % 500 == 0:
                elapsed = max(time.perf_counter() - started_at, 1e-9)
                rate = idx / elapsed
                logger.debug(
                    "Snapshot progress: %s/%s records processed (%.1f rec/s)",
                    idx,
                    len(rows),
                    rate,
                )

    # Stable order for deterministic output.
    records.sort(key=lambda r: (str(r["law_id"]), str(r["version_id"])))

    out_dir = config.output_root / ingest_manifest.run_id / "stages" / "03_snapshot"
    out_dir.mkdir(parents=True, exist_ok=True)

    out_jsonl = out_dir / "law_versions.jsonl"
    with out_jsonl.open("w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            f.write("\n")

    manifest = SnapshotManifest(
        dataset_id=ingest_manifest.dataset_id,
        run_id=ingest_manifest.run_id,
        source_csv_entry=ingest_manifest.csv_entry,
        record_count=len(records),
        output_jsonl=str(out_jsonl),
        xml_parsed_count=len(records),
        xml_parse_policy="strict_fail_fast",
    )

    out_manifest = out_dir / "manifest.json"
    with out_manifest.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest.to_dict(), f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")

    elapsed = max(time.perf_counter() - started_at, 1e-9)
    logger.info(
        "Snapshot complete: records=%s, parsed=%s, elapsed=%.2fs, rate=%.1f rec/s",
        len(records),
        len(records) if need_xml_parse else 0,
        elapsed,
        len(records) / elapsed,
    )

    return manifest
