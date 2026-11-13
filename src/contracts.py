from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from zipfile import ZipFile

if TYPE_CHECKING:
    from .config import RunConfig


@dataclass(frozen=True)
class IngestManifest:
    dataset_id: str
    run_id: str
    input_zip: str
    input_sha256: str
    csv_entry: str
    xml_entry_count: int
    xml_entries_sample: list[str]
    source_name: str = "egov"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ValidationManifest:
    dataset_id: str
    run_id: str
    xsd_path: str
    xsd_exists: bool
    csv_entry: str
    csv_column_count: int
    csv_header: list[str]
    missing_required_columns: list[str]
    csv_contract_ok: bool
    source_name: str = "egov"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SnapshotManifest:
    dataset_id: str
    run_id: str
    source_csv_entry: str
    record_count: int
    output_jsonl: str
    xml_parsed_count: int | None = None
    xml_parse_policy: str | None = None
    source_name: str = "egov"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TimelineManifest:
    dataset_id: str
    run_id: str
    source_versions_jsonl: str
    timeline_count: int
    output_jsonl: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class GraphPlanManifest:
    dataset_id: str
    run_id: str
    source_timelines_jsonl: str
    planned_commit_count: int
    ref_update_count: int
    output_json: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class DiffManifest:
    dataset_id: str
    run_id: str
    source_snapshot_jsonl: str
    diff_record_count: int
    output_jsonl: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PipelineResult:
    run_id: str
    manifest_path: Path


@dataclass(frozen=True)
class ApplyResult:
    run_id: str
    manifest_path: Path


@dataclass(frozen=True)
class XmlLawMeta:
    law_num: str
    era: str
    year: str
    num: str
    law_type: str
    lang: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class XmlStructureItem:
    article_num: str
    paragraph_num: str
    sentence_text: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class XmlSupplProvision:
    suppl_type: str
    amend_law_num: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ParsedLawXml:
    law_meta: XmlLawMeta
    structure: list[XmlStructureItem]
    has_delete: bool
    has_hide: bool
    has_extract: bool
    suppl_provisions: list[XmlSupplProvision]

    def to_dict(self) -> dict:
        return {
            "law_meta": self.law_meta.to_dict(),
            "structure": [item.to_dict() for item in self.structure],
            "has_delete": self.has_delete,
            "has_hide": self.has_hide,
            "has_extract": self.has_extract,
            "suppl_provisions": [item.to_dict() for item in self.suppl_provisions],
        }


class LawXmlParser(Protocol):
    """Parses a law XML payload into a typed intermediate model."""

    def parse(
        self, *, law_id: str, version_id: str, xml_bytes: bytes
    ) -> ParsedLawXml: ...


class BulkSource(Protocol):
    """Adapter that owns source-specific I/O for the pipeline.

    A source encapsulates everything that varies by upstream dataset:
    how to read the input zip, how to resolve an XML entry, how to map a
    raw XML payload into the ``LawVersion`` domain model. Downstream
    stages consume the resulting manifest and JSONL only, so the rest of
    the pipeline stays source-agnostic.
    """

    name: str

    def detect(self, zip_path: Path) -> bool:
        """Return True when this source can handle ``zip_path``."""

    def ingest(self, config: RunConfig) -> IngestManifest:
        """Identify the dataset and emit an ``IngestManifest``."""

    def validate(
        self, config: RunConfig, ingest_manifest: IngestManifest
    ) -> ValidationManifest:
        """Validate source-specific contracts (CSV columns, XSD, etc.)."""

    def normalize_versions(
        self,
        config: RunConfig,
        ingest_manifest: IngestManifest,
        law_xml_parser: LawXmlParser | None = None,
    ) -> SnapshotManifest:
        """Produce the canonical ``law_versions.jsonl`` for the run."""

    def read_xml(
        self,
        zf: ZipFile,
        zip_index: dict[str, str],
        *,
        law_id: str,
        revision_id: str,
        xml_entry: str | None,
    ) -> bytes:
        """Read a single law XML payload from the source zip."""
