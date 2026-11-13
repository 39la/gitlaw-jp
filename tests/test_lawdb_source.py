"""Tests for the JAHIS ``law_data.zip`` bulk source adapter."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pygit2

from src.config import RunConfig
from src.pipeline import run_apply, run_plan
from src.sources import (
    BulkSource,
    EgovBulkSource,
    LawdbBulkSource,
    detect_source,
    resolve_source,
)
from src.sources.lawdb import (
    _derive_law_id_from_path,
    _derive_law_type_from_path,
    _law_num_for,
    _parse_law_meta,
    _promulgation_date_for,
    _revision_id_for,
)


class _DetectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _build_egov_zip(self, path: Path) -> None:
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as zf:
            zf.writestr("all_law_list.csv", "法令種別,法令番号\n")
            zf.writestr(
                "326AC1000000285_20230401_504AC0000000024/x.xml",
                "<Law/>",
            )

    def _build_lawdb_zip(self, path: Path) -> None:
        xml = (
            '<Law Era="Meiji" Year="19" LawType="Act" Num="1" '
            'PromulgateMonth="8" PromulgateDay="13" Lang="ja">'
            "<LawBody><LawTitle>登記法</LawTitle></LawBody></Law>"
        )
        with ZipFile(path, "w", compression=ZIP_DEFLATED) as zf:
            zf.writestr("Act/119/119a0001.xml", xml)
            zf.writestr(
                "Constitution/122constitution.xml",
                xml.replace('LawType="Act"', 'LawType="Constitution"')
                .replace('Year="19"', 'Year="22"')
                .replace('Num="1"', ""),
            )
            zf.writestr("XMLSchemaForJapaneseLaw_v3-lawdb.xsd", "<xs:schema/>")

    def test_detect_lawdb(self) -> None:
        path = self.tmpdir / "law_data.zip"
        self._build_lawdb_zip(path)
        source = detect_source(path)
        self.assertIsInstance(source, LawdbBulkSource)

    def test_detect_egov(self) -> None:
        path = self.tmpdir / "all_xml.zip"
        self._build_egov_zip(path)
        source = detect_source(path)
        self.assertIsInstance(source, EgovBulkSource)

    def test_resolve_explicit_lawdb(self) -> None:
        path = self.tmpdir / "law_data.zip"
        self._build_lawdb_zip(path)
        source = resolve_source("lawdb", path)
        self.assertIsInstance(source, LawdbBulkSource)

    def test_resolve_unknown_source_raises(self) -> None:
        path = self.tmpdir / "law_data.zip"
        self._build_lawdb_zip(path)
        with self.assertRaises(ValueError):
            resolve_source("not-a-source", path)

    def test_detect_corrupt_zip_raises_value_error(self) -> None:
        path = self.tmpdir / "broken.zip"
        path.write_bytes(b"not a zip archive")
        with self.assertRaises(ValueError):
            detect_source(path)


class _PathDerivationTests(unittest.TestCase):
    def test_derive_law_id_act(self) -> None:
        self.assertEqual(_derive_law_id_from_path("Act/119/119a0001.xml"), "119")

    def test_derive_law_id_imperial_order(self) -> None:
        self.assertEqual(
            _derive_law_id_from_path("ImperialOrder/201/201i0001.xml"),
            "201",
        )

    def test_derive_law_id_constitution(self) -> None:
        self.assertEqual(
            _derive_law_id_from_path("Constitution/122constitution.xml"),
            "122",
        )

    def test_derive_law_id_military_with_subtype(self) -> None:
        self.assertEqual(
            _derive_law_id_from_path("MilitaryOrdinance/140/140ima001.xml"),
            "140",
        )

    def test_derive_law_id_dot_suffix(self) -> None:
        self.assertEqual(
            _derive_law_id_from_path("Act/322.89/322.89a0001.xml"),
            "322.89",
        )

    def test_derive_law_type(self) -> None:
        self.assertEqual(
            _derive_law_type_from_path("ImperialOrder/119/119i0001.xml"),
            "ImperialOrder",
        )
        self.assertEqual(
            _derive_law_type_from_path("Constitution/122constitution.xml"),
            "Constitution",
        )


class _MetadataParseTests(unittest.TestCase):
    def test_parse_law_meta(self) -> None:
        xml = (
            '<?xml version="1.0"?>'
            '<Law Era="Meiji" Lang="ja" LawType="Act" Num="1" '
            'PromulgateDay="13" PromulgateMonth="8" Year="19">'
            "<LawBody><LawTitle>登記法</LawTitle></LawBody></Law>"
        ).encode("utf-8")
        meta = _parse_law_meta(xml)
        self.assertEqual(meta["era"], "Meiji")
        self.assertEqual(meta["year"], "19")
        self.assertEqual(meta["num"], "1")
        self.assertEqual(meta["law_type"], "Act")
        self.assertEqual(meta["promulgate_month"], "8")
        self.assertEqual(meta["promulgate_day"], "13")
        self.assertEqual(meta["law_title"], "登記法")

    def test_revision_id_uses_gregorian_date(self) -> None:
        meta = {
            "era": "Meiji",
            "year": "19",
            "num": "1",
            "law_type": "Act",
            "law_subtype": "",
            "promulgate_month": "8",
            "promulgate_day": "13",
            "law_title": "登記法",
        }
        self.assertEqual(_revision_id_for(meta, "119a0001"), "18860813_119a0001")

    def test_revision_id_falls_back_when_undated(self) -> None:
        meta = {
            "era": "",
            "year": "",
            "num": "",
            "law_type": "",
            "law_subtype": "",
            "promulgate_month": "",
            "promulgate_day": "",
            "law_title": "",
        }
        self.assertTrue(_revision_id_for(meta, "x").startswith("undated_"))

    def test_revision_id_with_dotted_law_id_stays_unique(self) -> None:
        meta = {
            "era": "",
            "year": "",
            "num": "",
            "law_type": "Act",
            "law_subtype": "",
            "promulgate_month": "",
            "promulgate_day": "",
            "law_title": "",
        }
        revision_ids = {
            _revision_id_for(meta, basename)
            for basename in (
                "322.89a0001.xml",
                "322.89a0002.xml",
                "322.89i0001.xml",
            )
        }
        self.assertEqual(
            revision_ids,
            {
                "undated_322.89a0001",
                "undated_322.89a0002",
                "undated_322.89i0001",
            },
        )

    def test_promulgation_date_meiji_to_gregorian(self) -> None:
        meta = {
            "era": "Meiji",
            "year": "19",
            "promulgate_month": "8",
            "promulgate_day": "13",
        }
        self.assertEqual(_promulgation_date_for(meta), "1886-08-13")

    def test_law_num_japanese_format(self) -> None:
        meta = {
            "era": "Meiji",
            "year": "19",
            "num": "1",
        }
        self.assertEqual(_law_num_for(meta), "明治19年法律第1号")


class _LawdbBulkSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.zip_path = self.tmpdir / "law_data.zip"
        self.xsd_path = self.tmpdir / "schema.xsd"
        self.xsd_path.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
        )

        xml_act1 = (
            '<Law Era="Meiji" Year="19" LawType="Act" Num="1" '
            'PromulgateMonth="8" PromulgateDay="13" Lang="ja">'
            "<LawBody><LawTitle>登記法</LawTitle></LawBody></Law>"
        )
        xml_act2 = (
            '<Law Era="Meiji" Year="19" LawType="Act" Num="2" '
            'PromulgateMonth="8" PromulgateDay="13" Lang="ja">'
            "<LawBody><LawTitle>公證人規則</LawTitle></LawBody></Law>"
        )
        xml_io1 = (
            '<Law Era="Meiji" Year="19" LawType="ImperialOrder" Num="1" '
            'PromulgateMonth="2" PromulgateDay="26" Lang="ja">'
            "<LawBody><LawTitle>公文式</LawTitle></LawBody></Law>"
        )
        with ZipFile(self.zip_path, "w", compression=ZIP_DEFLATED) as zf:
            zf.writestr("Act/119/119a0001.xml", xml_act1)
            zf.writestr("Act/119/119a0002.xml", xml_act2)
            zf.writestr("ImperialOrder/119/119i0001.xml", xml_io1)
            zf.writestr("XMLSchemaForJapaneseLaw_v3-lawdb.xsd", "<xs:schema/>")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ingest_manifest_counts_xml_files(self) -> None:
        source = LawdbBulkSource()
        config = RunConfig(input_zip=self.zip_path, xsd_path=self.xsd_path)
        manifest = source.ingest(config)
        self.assertEqual(manifest.xml_entry_count, 3)
        self.assertEqual(manifest.source_name, "lawdb")
        self.assertEqual(manifest.csv_entry, "")

    def test_validate_succeeds_without_csv(self) -> None:
        source = LawdbBulkSource()
        config = RunConfig(input_zip=self.zip_path, xsd_path=self.xsd_path)
        ingest = source.ingest(config)
        manifest = source.validate(config, ingest)
        self.assertTrue(manifest.csv_contract_ok)
        self.assertEqual(manifest.source_name, "lawdb")

    def test_normalize_filters_by_law_type(self) -> None:
        source = LawdbBulkSource()
        output_root = self.tmpdir / "runs"
        config = RunConfig(
            input_zip=self.zip_path,
            xsd_path=self.xsd_path,
            output_root=output_root,
            law_types=("法律",),
        )
        ingest = source.ingest(config)
        snapshot = source.normalize_versions(config, ingest)
        self.assertEqual(snapshot.source_name, "lawdb")
        self.assertEqual(snapshot.record_count, 2)

        versions = [
            json.loads(line)
            for line in Path(snapshot.output_jsonl)
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        self.assertEqual(
            sorted(v["law_num"] for v in versions),
            ["明治19年法律第1号", "明治19年法律第2号"],
        )
        for version in versions:
            self.assertEqual(version["law_id"], "119")
            self.assertEqual(version["law_type"], "Act")
            self.assertTrue(version["promulgation_date"])
            self.assertTrue(version["revision_id"].startswith("1886"))

    def test_read_xml_round_trip(self) -> None:
        source = LawdbBulkSource()
        with ZipFile(self.zip_path, "r") as zf:
            index = {n: n for n in zf.namelist()}
            payload = source.read_xml(
                zf,
                index,
                law_id="119",
                revision_id="18860813_119a0001",
                xml_entry="Act/119/119a0001.xml",
            )
        self.assertIn("登記法".encode("utf-8"), payload)

    def test_normalize_keeps_dotted_law_ids_distinct(self) -> None:
        source = LawdbBulkSource()
        dotted_zip = self.tmpdir / "dotted.zip"
        xml_v1 = (
            '<Law Era="Meiji" Year="19" LawType="Act" Num="1" '
            'PromulgateMonth="8" PromulgateDay="13">'
            "<LawBody><LawTitle>旧法</LawTitle></LawBody></Law>"
        )
        xml_v2 = (
            '<Law Era="Meiji" Year="19" LawType="Act" Num="2" '
            'PromulgateMonth="8" PromulgateDay="14">'
            "<LawBody><LawTitle>新法</LawTitle></LawBody></Law>"
        )
        with ZipFile(dotted_zip, "w", compression=ZIP_DEFLATED) as zf:
            zf.writestr("Act/322.89/322.89a0001.xml", xml_v1)
            zf.writestr("Act/322.89/322.89a0002.xml", xml_v2)

        config = RunConfig(
            input_zip=dotted_zip,
            xsd_path=self.xsd_path,
            output_root=self.tmpdir / "runs",
        )
        ingest = source.ingest(config)
        snapshot = source.normalize_versions(config, ingest)
        versions = self._read_versions(snapshot)

        self.assertEqual(snapshot.record_count, 2)
        self.assertEqual({v["law_id"] for v in versions}, {"322.89"})
        self.assertEqual(
            sorted(v["revision_id"] for v in versions),
            ["18860813_322.89a0001", "18860814_322.89a0002"],
        )

        with ZipFile(dotted_zip, "r") as zf:
            index = {n: n for n in zf.namelist()}
            payload = source.read_xml(
                zf,
                index,
                law_id="322.89",
                revision_id="18860813_322.89a0001",
                xml_entry=None,
            )
        self.assertIn("旧法".encode("utf-8"), payload)

    def test_normalize_filters_by_law_id(self) -> None:
        source = LawdbBulkSource()
        config = RunConfig(
            input_zip=self.zip_path,
            xsd_path=self.xsd_path,
            output_root=self.tmpdir / "runs-filtered",
            law_types=("法律",),
            law_ids=("does-not-exist",),
        )
        ingest = source.ingest(config)
        snapshot = source.normalize_versions(config, ingest)
        self.assertEqual(snapshot.record_count, 0)

        kept_config = RunConfig(
            input_zip=self.zip_path,
            xsd_path=self.xsd_path,
            output_root=self.tmpdir / "runs-kept",
            law_types=("法律",),
            law_ids=("119",),
        )
        kept_ingest = source.ingest(kept_config)
        kept_snapshot = source.normalize_versions(kept_config, kept_ingest)
        self.assertEqual(kept_snapshot.record_count, 2)

    @staticmethod
    def _read_versions(snapshot: object) -> list[dict[str, object]]:
        output_jsonl = Path(getattr(snapshot, "output_jsonl"))
        return [
            json.loads(line)
            for line in output_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


class _LawdbPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self.xsd_path = self.tmpdir / "schema.xsd"
        self.xsd_path.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>'
        )
        self.zip_path = self.tmpdir / "law_data.zip"
        xml_v1 = (
            '<Law Era="Meiji" Year="19" LawType="Act" Num="1" '
            'PromulgateMonth="8" PromulgateDay="13">'
            "<LawBody><LawTitle>登記法</LawTitle></LawBody></Law>"
        )
        xml_v2 = (
            '<Law Era="Meiji" Year="19" LawType="Act" Num="2" '
            'PromulgateMonth="9" PromulgateDay="1">'
            "<LawBody><LawTitle>公證人規則</LawTitle></LawBody></Law>"
        )
        with ZipFile(self.zip_path, "w", compression=ZIP_DEFLATED) as zf:
            zf.writestr("Act/119/119a0001.xml", xml_v1)
            zf.writestr("Act/119/119a0002.xml", xml_v2)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lawdb_plan_apply_round_trip(self) -> None:
        plan_result = run_plan(
            RunConfig(
                input_zip=self.zip_path,
                output_root=self.tmpdir / "runs",
                xsd_path=self.xsd_path,
                source="lawdb",
            )
        )

        repo_path = self.tmpdir / "repo"
        pygit2.init_repository(str(repo_path), bare=True)
        apply_result = run_apply(
            RunConfig(git_repo_root=repo_path, git_target_dir=Path("laws")),
            run_manifest_path=plan_result.manifest_path,
        )

        manifest = json.loads(apply_result.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["source"], "lawdb")
        self.assertIn("06_git_sink", manifest["stages"])

        repo = pygit2.Repository(str(repo_path))
        run_id = manifest["run_id"]
        repo.lookup_reference(f"refs/heads/{run_id}/promulgations")
        repo.lookup_reference(f"refs/heads/{run_id}/enforcements")


class _BulkSourceProtocolTests(unittest.TestCase):
    def test_lawdb_satisfies_protocol(self) -> None:
        source: BulkSource = LawdbBulkSource()
        self.assertEqual(source.name, "lawdb")

    def test_egov_satisfies_protocol(self) -> None:
        source: BulkSource = EgovBulkSource()
        self.assertEqual(source.name, "egov")


if __name__ == "__main__":
    unittest.main()
