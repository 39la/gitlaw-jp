"""Base classes and re-exports for bulk sources."""

from __future__ import annotations

from collections.abc import Iterable
from zipfile import ZipFile

from ..config import RunConfig
from ..contracts import (
    BulkSource,
    IngestManifest,
    LawXmlParser,
    SnapshotManifest,
)
from ..xml_parser import XsdataLawXmlParser
from ._common import write_normalized_versions

__all__ = ["BulkSource", "BaseBulkSource"]


class BaseBulkSource(BulkSource):
    """Template implementation shared by concrete bulk sources.

    Subclasses provide source-specific record iteration and XML entry
    candidates; the canonical JSONL/manifest output contract and the
    generic XML lookup fallback live here.
    """

    name: str

    def normalize_versions(
        self,
        config: RunConfig,
        ingest_manifest: IngestManifest,
        law_xml_parser: LawXmlParser | None = None,
    ) -> SnapshotManifest:
        need_xml_parse = config.snapshot_parse_xml
        parser = (law_xml_parser or XsdataLawXmlParser()) if need_xml_parse else None
        with ZipFile(config.input_zip, "r") as zf:
            records = list(self._iter_records(zf, config, ingest_manifest, parser))
        return write_normalized_versions(
            config=config,
            ingest_manifest=ingest_manifest,
            records=records,
            source_name=self.name,
            parse_xml=need_xml_parse,
        )

    def _iter_records(
        self,
        zf: ZipFile,
        config: RunConfig,
        ingest_manifest: IngestManifest,
        parser: LawXmlParser | None,
    ) -> Iterable[dict[str, object]]:
        raise NotImplementedError

    def read_xml(
        self,
        zf: ZipFile,
        zip_index: dict[str, str],
        *,
        law_id: str,
        revision_id: str,
        xml_entry: str | None,
    ) -> bytes:
        candidates: list[str] = []
        if xml_entry:
            candidates.append(xml_entry.replace("\\", "/"))
        candidates.extend(self._xml_candidates(law_id=law_id, revision_id=revision_id))
        for candidate in candidates:
            original = zip_index.get(candidate)
            if original is not None:
                return _read_entry(zf, original)

        suffix = self._xml_suffix(law_id=law_id, revision_id=revision_id)
        if suffix:
            for normalized, original in zip_index.items():
                if normalized == suffix or normalized.endswith(f"/{suffix}"):
                    return _read_entry(zf, original)

        raise FileNotFoundError(f"xml entry not found for {law_id}:{revision_id}")

    def _xml_candidates(self, *, law_id: str, revision_id: str) -> list[str]:
        raise NotImplementedError

    def _xml_suffix(self, *, law_id: str, revision_id: str) -> str | None:
        return None


def _read_entry(zf: ZipFile, entry: str) -> bytes:
    with zf.open(entry, "r") as raw:
        return raw.read()
