"""Nagoya University JAHIS ``law_data.zip`` adapter.

The JAHIS archive ships historical Japanese laws (Meiji 19 - Heisei 29)
without a CSV index. Files are laid out as::

    {LawType}/{law_id}/{law_id}{type_prefix}{seq}[a-z].xml
    Constitution/{era}{N}constitution.xml

where ``law_id`` encodes the wareki year (e.g. ``119`` = Meiji 19).
This adapter walks the archive directly, extracting metadata from each
XML payload on the fly.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path
from zipfile import ZipFile

from ..config import RunConfig, serialize_path
from ..contracts import (
    IngestManifest,
    LawXmlParser,
    ValidationManifest,
)
from ..law_types import normalize_law_type
from ._common import build_ingest_manifest, normalize_zip_entries
from .base import BaseBulkSource

logger = logging.getLogger(__name__)


ERA_TO_WAREKI = {
    "Meiji": "明治",
    "Taisho": "大正",
    "Showa": "昭和",
    "Heisei": "平成",
    "Reiwa": "令和",
}

KNOWN_TOP_DIRS = {
    "Act",
    "Constitution",
    "ImperialHouseholdOrdinance",
    "ImperialOrder",
    "MilitaryOrdinance",
}

_LAWDB_XML_RE = re.compile(r"\.xml$", re.IGNORECASE)


def _era_to_gregorian_year(era: str, era_year: int) -> int | None:
    base = {
        "Meiji": 1868,
        "Taisho": 1912,
        "Showa": 1926,
        "Heisei": 1989,
        "Reiwa": 2019,
    }.get(era)
    if base is None:
        return None
    return base + era_year - 1


def _filter_xml_entries(names: Iterable[str]) -> list[str]:
    return [n for n in names if _LAWDB_XML_RE.search(n)]


def _derive_law_id_from_path(entry: str) -> str:
    """Extract the law_id component from a JAHIS zip entry path.

    Examples:
        ``Act/119/119a0001.xml``           -> ``119``
        ``ImperialOrder/201/201i0001.xml``  -> ``201``
        ``Constitution/122constitution.xml``-> ``122``
        ``MilitaryOrdinance/140/140ima001.xml`` -> ``140``
    """
    parts = entry.split("/")
    if len(parts) < 2:
        return ""
    base = parts[-1].rsplit(".", 1)[0]
    top = parts[0]
    if top == "Constitution":
        m = re.match(r"^(\d+)", base)
        return m.group(1) if m else ""
    if len(parts) >= 3:
        directory = parts[1]
        if directory and base.startswith(directory):
            return directory
    m = re.match(r"^(\d+(?:\.\d+)?)", base)
    return m.group(1) if m else ""


def _derive_law_type_from_path(entry: str) -> str:
    parts = entry.split("/")
    if not parts:
        return ""
    return parts[0]


def _parse_law_meta(xml_bytes: bytes) -> dict[str, str]:
    """Pull the small set of attributes the lawdb source needs.

    The lawdb XSD has a slightly different ``LawType`` enumeration than
    the e-Gov XSD the xsdata dataclasses were generated from; we only
    need attribute values and the law title, so stdlib ElementTree is
    sufficient and stays order-independent of the attribute list.
    """
    meta: dict[str, str] = {
        "era": "",
        "year": "",
        "num": "",
        "law_type": "",
        "law_subtype": "",
        "promulgate_month": "",
        "promulgate_day": "",
        "law_title": "",
    }
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.debug("lawdb XML parse error: %s", exc)
        return meta
    for key, target in (
        ("Era", "era"),
        ("Year", "year"),
        ("Num", "num"),
        ("LawType", "law_type"),
        ("LawSubtype", "law_subtype"),
        ("PromulgateMonth", "promulgate_month"),
        ("PromulgateDay", "promulgate_day"),
    ):
        value = root.attrib.get(key)
        if value:
            meta[target] = value
    title_el = root.find(".//LawTitle")
    if title_el is not None and title_el.text:
        meta["law_title"] = title_el.text.strip()
    return meta


def _revision_id_for(meta: dict[str, str], basename: str) -> str:
    """Synthesize a stable ``revision_id`` for a JAHIS XML.

    The naming combines a western date prefix (so the existing date
    resolution strategy can extract an effective-date hint) with the
    file sequence so each version stays uniquely addressable.
    """
    era = meta["era"]
    year = meta["year"]
    month = meta["promulgate_month"]
    day = meta["promulgate_day"]
    seq = basename.rsplit(".", 1)[0]
    if era and year and month and day:
        try:
            gregorian = _era_to_gregorian_year(era, int(year))
            if gregorian is not None:
                return f"{gregorian:04d}{int(month):02d}{int(day):02d}_{seq}"
        except ValueError:
            pass
    return f"undated_{seq}"


def _promulgation_date_for(meta: dict[str, str]) -> str:
    era = meta["era"]
    year = meta["year"]
    month = meta["promulgate_month"]
    day = meta["promulgate_day"]
    if not (era and year and month and day):
        return ""
    try:
        gregorian = _era_to_gregorian_year(era, int(year))
        if gregorian is None:
            return ""
        return f"{gregorian:04d}-{int(month):02d}-{int(day):02d}"
    except ValueError:
        return ""


def _law_num_for(meta: dict[str, str]) -> str:
    wareki = ERA_TO_WAREKI.get(meta["era"], "")
    num = meta["num"]
    if not num:
        return ""
    if not wareki:
        return f"第{num}号"
    try:
        return f"{wareki}{int(meta['year'])}年法律第{num}号"
    except ValueError:
        return f"{wareki}法律第{num}号"


class LawdbBulkSource(BaseBulkSource):
    """Source adapter for the Nagoya University JAHIS ``law_data.zip``."""

    name = "lawdb"

    def detect(self, zip_path: Path) -> bool:
        if not zip_path.exists():
            return False
        with ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
        if any(name.endswith("all_law_list.csv") for name in names):
            return False
        top_dirs = {name.split("/", 1)[0] for name in names if "/" in name}
        return bool(top_dirs & KNOWN_TOP_DIRS)

    def ingest(self, config: RunConfig) -> IngestManifest:
        with ZipFile(config.input_zip, "r") as zf:
            names = normalize_zip_entries(zf.namelist())
        xml_entries = _filter_xml_entries(names)
        return build_ingest_manifest(
            zip_path=config.input_zip,
            xml_entries=xml_entries,
            csv_entry="",
            source_name=self.name,
        )

    def validate(
        self, config: RunConfig, ingest_manifest: IngestManifest
    ) -> ValidationManifest:
        xsd_exists = config.xsd_path.exists()
        if not xsd_exists:
            raise FileNotFoundError(f"xsd not found: {config.xsd_path}")
        return ValidationManifest(
            dataset_id=ingest_manifest.dataset_id,
            run_id=ingest_manifest.run_id,
            xsd_path=serialize_path(config.xsd_path),
            xsd_exists=xsd_exists,
            csv_entry="",
            csv_column_count=0,
            csv_header=[],
            missing_required_columns=[],
            csv_contract_ok=True,
            source_name=self.name,
        )

    def _iter_records(
        self,
        zf: ZipFile,
        config: RunConfig,
        ingest_manifest: IngestManifest,
        parser: LawXmlParser | None,
    ) -> Iterable[dict[str, object]]:
        if config.snapshot_parse_xml:
            logger.warning(
                "lawdb source does not support --snapshot-parse-xml; "
                "emitting placeholder xml_mvp values instead"
            )
        allowed_law_ids = {law_id for law_id in config.law_ids if law_id}
        entries = sorted(
            entry
            for entry in normalize_zip_entries(zf.namelist())
            if _LAWDB_XML_RE.search(entry)
        )
        started_at = time.perf_counter()
        record_count = 0
        skipped_no_law_id = 0
        skipped_law_type = 0
        skipped_law_id = 0

        for entry in entries:
            law_id = _derive_law_id_from_path(entry)
            if not law_id:
                logger.debug("Skipping entry without derivable law_id: %s", entry)
                skipped_no_law_id += 1
                continue
            path_law_type = _derive_law_type_from_path(entry)
            if not self._matches_law_type_filter(path_law_type, config.law_types):
                skipped_law_type += 1
                continue
            if allowed_law_ids and law_id not in allowed_law_ids:
                skipped_law_id += 1
                continue

            yield self._build_record(
                zf,
                entry,
                law_id=law_id,
                path_law_type=path_law_type,
                parse_xml=config.snapshot_parse_xml,
            )
            record_count += 1

        logger.info(
            "lawdb normalize complete: records=%s "
            "skipped(no_law_id=%s, law_type=%s, law_id=%s) elapsed=%.2fs",
            record_count,
            skipped_no_law_id,
            skipped_law_type,
            skipped_law_id,
            time.perf_counter() - started_at,
        )

    def _xml_candidates(self, *, law_id: str, revision_id: str) -> list[str]:
        basename = revision_id.split("_", 1)[-1] if "_" in revision_id else revision_id
        candidates: list[str] = []
        for top in sorted(KNOWN_TOP_DIRS):
            if top == "Constitution":
                candidates.append(f"Constitution/{basename}.xml")
            else:
                candidates.append(f"{top}/{law_id}/{basename}.xml")
                candidates.append(f"{top}/{law_id}/{basename}")
        return candidates

    def _xml_suffix(self, *, law_id: str, revision_id: str) -> str | None:
        del law_id
        basename = revision_id.split("_", 1)[-1] if "_" in revision_id else revision_id
        return f"{basename}.xml"

    def _build_record(
        self,
        zf: ZipFile,
        entry: str,
        *,
        law_id: str,
        path_law_type: str,
        parse_xml: bool,
    ) -> dict[str, object]:
        with zf.open(entry, "r") as raw:
            xml_bytes = raw.read()
        meta = _parse_law_meta(xml_bytes)
        basename = entry.rsplit("/", 1)[-1]
        revision_id = _revision_id_for(meta, basename)
        record: dict[str, object] = {
            "law_id": law_id,
            "revision_id": revision_id,
            "law_type": path_law_type,
            "law_num": _law_num_for(meta),
            "law_name": meta["law_title"],
            "promulgation_date": _promulgation_date_for(meta),
            "amendment_law_name": "",
            "amendment_law_num": "",
            "amendment_promulgation_date": "",
            "effective_date": _promulgation_date_for(meta),
            "effective_note": meta["law_subtype"],
            "is_marked_unenforced": False,
            "source_body_url": "",
            "xml_entry": entry,
            "xml_sha256": f"sha256:{hashlib.sha256(xml_bytes).hexdigest()}",
        }
        if parse_xml:
            record["xml_mvp"] = {
                "law_meta": {
                    "law_num": record["law_num"],
                    "era": meta["era"],
                    "year": meta["year"],
                    "num": meta["num"],
                    "law_type": path_law_type,
                    "lang": "ja",
                },
                "flags": {"delete": False, "hide": False, "extract": False},
                "amend": {"suppl_provisions": []},
                "structure_summary": {"sentence_count": 0, "first_sentence": ""},
            }
        return record

    @staticmethod
    def _matches_law_type_filter(
        path_law_type: str,
        allowed_law_types: tuple[str, ...],
    ) -> bool:
        if not allowed_law_types:
            return True
        normalized_allowed = {normalize_law_type(t) for t in allowed_law_types if t}
        return normalize_law_type(path_law_type) in normalized_allowed
