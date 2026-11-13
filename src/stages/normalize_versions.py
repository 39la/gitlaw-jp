from __future__ import annotations

from ..config import RunConfig
from ..contracts import IngestManifest, LawXmlParser, SnapshotManifest
from ..sources import resolve_source


def create_normalized_versions(
    config: RunConfig,
    ingest_manifest: IngestManifest,
    law_xml_parser: LawXmlParser | None = None,
) -> SnapshotManifest:
    source = resolve_source(ingest_manifest.source_name, config.input_zip)
    return source.normalize_versions(config, ingest_manifest, law_xml_parser)
