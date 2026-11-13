"""e-Gov bulk XML adapter.

The e-Gov ``all_xml.zip`` ships:

* a single ``all_law_list.csv`` with the canonical 14 columns, and
* XML files keyed by ``{law_id}_{revision_id}/{law_id}_{revision_id}.xml``
  in flat directories.

This adapter preserves the existing behaviour for that layout.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import re
import time
from collections.abc import Iterable
from pathlib import Path
from zipfile import ZipFile

from ..config import RunConfig, serialize_path
from ..contracts import (
    IngestManifest,
    LawXmlParser,
    ParsedLawXml,
    ValidationManifest,
)
from ._common import build_ingest_manifest, normalize_zip_entries
from .base import BaseBulkSource

logger = logging.getLogger(__name__)

REQUIRED_CSV_COLUMNS = [
    "法令種別",
    "法令番号",
    "法令名",
    "法令名読み",
    "旧法令名",
    "公布日",
    "改正法令名",
    "改正法令番号",
    "改正法令公布日",
    "施行日",
    "施行日備考",
    "法令ID",
    "本文URL",
    "未施行",
]

_LAW_XML_RE = re.compile(r"\.xml$", re.IGNORECASE)


def _extract_revision_id(url: str) -> str:
    normalized = url.strip().rstrip("/")
    if not normalized:
        return ""
    return normalized.split("/")[-1]


def _read_csv_header_from_zip(zip_path: Path, csv_entry: str) -> list[str]:
    with ZipFile(zip_path, "r") as zf, zf.open(csv_entry, "r") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        reader = csv.reader(text)
        try:
            raw_header = next(reader)
            return [c.lstrip("\ufeff").strip() for c in raw_header]
        except StopIteration as exc:
            raise ValueError(f"csv is empty: {csv_entry}") from exc


def _read_csv_rows(zip_path: Path, csv_entry: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with ZipFile(zip_path, "r") as zf, zf.open(csv_entry, "r") as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
        reader = csv.DictReader(text)
        for row in reader:
            rows.append(
                {
                    (k or "").lstrip("\ufeff").strip(): (v or "").strip()
                    for k, v in row.items()
                }
            )
    return rows


def _resolve_xml_entry(
    entries: list[str],
    entry_lookup: set[str],
    *,
    csv_entry: str,
    law_id: str,
    revision_id: str,
) -> str:
    if not law_id:
        raise ValueError("law_id is empty")
    if not revision_id:
        raise ValueError(f"revision_id is empty: law_id={law_id}")

    file_name = f"{law_id}_{revision_id}.xml"
    csv_parent = Path(csv_entry).parent.as_posix()

    candidates: list[str] = []
    if csv_parent and csv_parent != ".":
        candidates.append(f"{csv_parent}/{law_id}_{revision_id}/{file_name}")
    candidates.append(f"{law_id}_{revision_id}/{file_name}")
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
            f"multiple xml entries matched for {law_id}:{revision_id}: {fallback[:3]}"
        )

    raise FileNotFoundError(f"xml entry not found for {law_id}:{revision_id}")


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


class EgovBulkSource(BaseBulkSource):
    """Source adapter for the e-Gov ``all_xml.zip`` bulk download."""

    name = "egov"

    def detect(self, zip_path: Path) -> bool:
        if not zip_path.exists():
            return False
        with ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
        return any(name.endswith("all_law_list.csv") for name in names)

    def ingest(self, config: RunConfig) -> IngestManifest:
        zip_path = config.input_zip
        with ZipFile(zip_path, "r") as zf:
            names = normalize_zip_entries(zf.namelist())
        csv_candidates = [n for n in names if n.endswith("all_law_list.csv")]
        if len(csv_candidates) != 1:
            raise ValueError(
                f"expected one all_law_list.csv in zip, got {len(csv_candidates)}"
            )
        xml_entries = [n for n in names if _LAW_XML_RE.search(n)]
        return build_ingest_manifest(
            zip_path=zip_path,
            xml_entries=xml_entries,
            csv_entry=csv_candidates[0],
            source_name=self.name,
        )

    def validate(
        self, config: RunConfig, ingest_manifest: IngestManifest
    ) -> ValidationManifest:
        header = _read_csv_header_from_zip(config.input_zip, ingest_manifest.csv_entry)
        missing = [col for col in REQUIRED_CSV_COLUMNS if col not in header]
        contract_ok = len(missing) == 0 and len(header) == len(REQUIRED_CSV_COLUMNS)

        xsd_exists = config.xsd_path.exists()
        if not xsd_exists:
            raise FileNotFoundError(f"xsd not found: {config.xsd_path}")
        if not contract_ok:
            raise ValueError(
                f"csv contract validation failed: columns={len(header)} "
                f"missing={missing}"
            )

        return ValidationManifest(
            dataset_id=ingest_manifest.dataset_id,
            run_id=ingest_manifest.run_id,
            xsd_path=serialize_path(config.xsd_path),
            xsd_exists=xsd_exists,
            csv_entry=ingest_manifest.csv_entry,
            csv_column_count=len(header),
            csv_header=header,
            missing_required_columns=missing,
            csv_contract_ok=contract_ok,
            source_name=self.name,
        )

    def _iter_records(
        self,
        zf: ZipFile,
        config: RunConfig,
        ingest_manifest: IngestManifest,
        parser: LawXmlParser | None,
    ) -> Iterable[dict[str, object]]:
        rows = _read_csv_rows(config.input_zip, ingest_manifest.csv_entry)
        rows = _filter_rows_by_law_id(rows, config.law_ids)
        rows = _filter_rows_by_law_type(rows, config.law_types)
        entries = normalize_zip_entries(zf.namelist())
        entry_lookup = set(entries)
        started_at = time.perf_counter()

        for idx, row in enumerate(rows, start=1):
            law_id = row.get("法令ID", "")
            body_url = row.get("本文URL", "")
            revision_id = _extract_revision_id(body_url)
            xml_entry = _resolve_xml_entry(
                entries,
                entry_lookup,
                csv_entry=ingest_manifest.csv_entry,
                law_id=law_id,
                revision_id=revision_id,
            )
            with zf.open(xml_entry, "r") as raw:
                xml_bytes = raw.read()
            parsed_xml = None
            if parser is not None:
                parsed_xml = parser.parse(
                    law_id=law_id,
                    version_id=revision_id,
                    xml_bytes=xml_bytes,
                )

            xml_sha256 = hashlib.sha256(xml_bytes).hexdigest()
            yield _to_law_version_row(
                row,
                xml_entry=xml_entry,
                xml_sha256=xml_sha256,
                parsed_xml=parsed_xml,
            )

            if idx % 500 == 0:
                elapsed = max(time.perf_counter() - started_at, 1e-9)
                logger.debug(
                    "Normalize progress: %s/%s records processed (%.1f rec/s)",
                    idx,
                    len(rows),
                    idx / elapsed,
                )

    def _xml_candidates(self, *, law_id: str, revision_id: str) -> list[str]:
        file_name = f"{law_id}_{revision_id}.xml"
        return [
            f"{law_id}_{revision_id}/{file_name}",
            file_name,
        ]

    def _xml_suffix(self, *, law_id: str, revision_id: str) -> str | None:
        return f"{law_id}_{revision_id}.xml"


def _filter_rows_by_law_id(
    rows: list[dict[str, str]], allowed_law_ids: tuple[str, ...]
) -> list[dict[str, str]]:
    if not allowed_law_ids:
        return rows
    allowed = {law_id for law_id in allowed_law_ids if law_id}
    return [row for row in rows if row.get("法令ID", "") in allowed]


def _filter_rows_by_law_type(
    rows: list[dict[str, str]], allowed_law_types: tuple[str, ...]
) -> list[dict[str, str]]:
    if not allowed_law_types:
        return rows
    from ..law_types import normalize_law_type

    allowed = {
        normalize_law_type(law_type) for law_type in allowed_law_types if law_type
    }
    return [
        row
        for row in rows
        if normalize_law_type(row.get("法令種別", ""), row.get("法令番号", ""))
        in allowed
    ]


def _to_law_version_row(
    row: dict[str, str],
    *,
    xml_entry: str,
    xml_sha256: str,
    parsed_xml: ParsedLawXml | None,
) -> dict[str, object]:
    law_id = row.get("法令ID", "")
    body_url = row.get("本文URL", "")
    revision_id = _extract_revision_id(body_url)
    record: dict[str, object] = {
        "law_id": law_id,
        "revision_id": revision_id,
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
        "xml_entry": xml_entry,
        "xml_sha256": f"sha256:{xml_sha256}",
    }
    if parsed_xml is not None:
        record["xml_mvp"] = _build_xml_mvp(parsed_xml)
    return record
