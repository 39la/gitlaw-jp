from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from src.config import RunConfig
from src.pipeline import run_apply, run_plan, run_pipeline


class PipelineV3Tests(unittest.TestCase):
    def _build_xsd(self, xsd_path: Path) -> None:
        xsd_path.parent.mkdir(parents=True, exist_ok=True)
        xsd_path.write_text(
            '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"/>\n',
            encoding="utf-8",
        )

    def _build_input_zip(self, zip_path: Path) -> None:
        csv_entry = "all_xml/all_law_list.csv"
        law_id = "123AC0000000001"
        revision_id = "20240101_505AC0000000053"
        xml_entry = f"all_xml/{law_id}_{revision_id}/{law_id}_{revision_id}.xml"

        csv_content = "\n".join(
            [
                "法令種別,法令番号,法令名,法令名読み,旧法令名,公布日,改正法令名,改正法令番号,改正法令公布日,施行日,施行日備考,法令ID,本文URL,未施行",
                (
                    "Act,法律第1号,テスト法,てすとほう,,2024-01-01,改正テスト法,令和五年法律第五十三号,2023-06-14,2024-03-01,,"
                    f"{law_id},https://laws.e-gov.go.jp/law/{revision_id},"
                ),
            ]
        )

        xml_content = """<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<Law Era=\"Reiwa\" Year=\"6\" Num=\"1\" LawType=\"Act\" Lang=\"ja\">
  <LawNum>令和六年法律第一号</LawNum>
  <LawBody>
    <MainProvision Extract=\"true\">
      <Article Num=\"1\">
        <Paragraph Num=\"1\">
          <ParagraphNum>1</ParagraphNum>
          <ParagraphSentence>
            <Sentence>テスト本文</Sentence>
          </ParagraphSentence>
        </Paragraph>
      </Article>
    </MainProvision>
  </LawBody>
</Law>
"""

        with ZipFile(zip_path, "w", compression=ZIP_DEFLATED) as zf:
            zf.writestr(csv_entry, csv_content)
            zf.writestr(xml_entry, xml_content)

    def test_run_pipeline_emits_graph_plan_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_zip = tmp_path / "input.zip"
            output_root = tmp_path / "runs"
            xsd_path = tmp_path / "schema.xsd"
            self._build_input_zip(input_zip)
            self._build_xsd(xsd_path)

            result = run_plan(
                RunConfig(
                    input_zip=input_zip,
                    output_root=output_root,
                    xsd_path=xsd_path,
                    branch_model="dual",
                )
            )

            self.assertTrue(result.manifest_path.exists())

            run_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(run_manifest["schema_version"], 3)
            self.assertIn("dataset_id", run_manifest)
            self.assertIn("03_normalize_versions", run_manifest["stages"])
            self.assertIn("04_build_timelines", run_manifest["stages"])
            self.assertIn("05_graph_plan", run_manifest["stages"])
            self.assertNotIn("04_diff", run_manifest["stages"])

            graph_plan_manifest_path = Path(run_manifest["stages"]["05_graph_plan"])
            graph_plan_manifest = json.loads(
                graph_plan_manifest_path.read_text(encoding="utf-8")
            )
            self.assertGreaterEqual(graph_plan_manifest["planned_commit_count"], 2)

            graph_plan_path = Path(graph_plan_manifest["output_json"])
            graph_plan = json.loads(graph_plan_path.read_text(encoding="utf-8"))
            projections = {
                commit["projection"] for commit in graph_plan["planned_commits"]
            }
            self.assertEqual(projections, {"promulgation", "enforcement"})
            self.assertIn("/promulgations", graph_plan["metadata"]["promulgation_ref"])
            self.assertIn("/enforcements", graph_plan["metadata"]["enforcement_ref"])

    def test_run_pipeline_replaces_existing_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_zip = tmp_path / "input.zip"
            output_root = tmp_path / "runs"
            xsd_path = tmp_path / "schema.xsd"
            self._build_input_zip(input_zip)
            self._build_xsd(xsd_path)

            result = run_plan(
                RunConfig(
                    input_zip=input_zip,
                    output_root=output_root,
                    xsd_path=xsd_path,
                    branch_model="dual",
                )
            )
            stale_file = output_root / result.run_id / "stale.txt"
            stale_file.write_text("stale\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                run_plan(
                    RunConfig(
                        input_zip=input_zip,
                        output_root=output_root,
                        xsd_path=xsd_path,
                        branch_model="dual",
                    )
                )

            self.assertTrue(stale_file.exists())

    def test_run_pipeline_force_replaces_existing_run_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_zip = tmp_path / "input.zip"
            output_root = tmp_path / "runs"
            xsd_path = tmp_path / "schema.xsd"
            self._build_input_zip(input_zip)
            self._build_xsd(xsd_path)

            fake_now = datetime(
                2026, 4, 2, 12, 0, 0, tzinfo=timezone(timedelta(hours=9))
            )
            with patch("src.sources._common.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = fake_now
                mocked_datetime.strftime = datetime.strftime
                result = run_plan(
                    RunConfig(
                        input_zip=input_zip, output_root=output_root, xsd_path=xsd_path
                    )
                )
            stale_file = output_root / result.run_id / "stale.txt"
            stale_file.write_text("stale\n", encoding="utf-8")

            with patch("src.sources._common.datetime") as mocked_datetime:
                mocked_datetime.now.return_value = fake_now
                mocked_datetime.strftime = datetime.strftime
                rerun = run_plan(
                    RunConfig(
                        input_zip=input_zip,
                        output_root=output_root,
                        xsd_path=xsd_path,
                        force=True,
                    )
                )

            self.assertFalse(stale_file.exists())
            self.assertEqual(result.run_id, rerun.run_id)

    def test_run_pipeline_defaults_as_of_for_git_projection(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_zip = tmp_path / "input.zip"
            output_root = tmp_path / "runs"
            repo_path = tmp_path / "repo"
            xsd_path = tmp_path / "schema.xsd"
            self._build_input_zip(input_zip)
            self._build_xsd(xsd_path)
            repo_path.mkdir()

            with patch("src.pipeline.execute_commit_graph_plan") as sink:
                sink.return_value.to_dict.return_value = {
                    "commit_count": 0,
                    "promulgation_commit_count": 0,
                    "enforcement_commit_count": 0,
                    "updated_refs": [],
                }
                result = run_pipeline(
                    RunConfig(
                        input_zip=input_zip,
                        output_root=output_root,
                        xsd_path=xsd_path,
                        branch_model="dual",
                        git_repo_root=repo_path,
                    )
                )

            run_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            expected = datetime.now(timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
            self.assertEqual(run_manifest["as_of"], expected)

    def test_run_pipeline_defaults_to_laws_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_zip = tmp_path / "input.zip"
            output_root = tmp_path / "runs"
            xsd_path = tmp_path / "schema.xsd"
            self._build_input_zip(input_zip)
            self._build_xsd(xsd_path)

            result = run_plan(
                RunConfig(
                    input_zip=input_zip, output_root=output_root, xsd_path=xsd_path
                )
            )
            run_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(run_manifest["law_types"], ["法律"])

    def test_run_pipeline_filters_by_law_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_zip = tmp_path / "input.zip"
            output_root = tmp_path / "runs"
            xsd_path = tmp_path / "schema.xsd"
            self._build_input_zip(input_zip)
            self._build_xsd(xsd_path)

            result = run_plan(
                RunConfig(
                    input_zip=input_zip,
                    output_root=output_root,
                    xsd_path=xsd_path,
                    branch_model="dual",
                    law_ids=("999AC0000000999",),
                )
            )

            run_manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            normalize_manifest = json.loads(
                Path(run_manifest["stages"]["03_normalize_versions"]).read_text(
                    encoding="utf-8"
                )
            )
            graph_plan_manifest = json.loads(
                Path(run_manifest["stages"]["05_graph_plan"]).read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(normalize_manifest["record_count"], 0)
            self.assertEqual(graph_plan_manifest["planned_commit_count"], 0)

    def test_run_apply_updates_existing_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            input_zip = tmp_path / "input.zip"
            output_root = tmp_path / "runs"
            repo_path = tmp_path / "repo"
            xsd_path = tmp_path / "schema.xsd"
            self._build_input_zip(input_zip)
            self._build_xsd(xsd_path)
            repo_path.mkdir()

            plan_result = run_plan(
                RunConfig(
                    input_zip=input_zip,
                    output_root=output_root,
                    xsd_path=xsd_path,
                )
            )

            with patch("src.pipeline.execute_commit_graph_plan") as sink:
                sink.return_value.to_dict.return_value = {
                    "commit_count": 0,
                    "promulgation_commit_count": 0,
                    "enforcement_commit_count": 0,
                    "updated_refs": [],
                }
                apply_result = run_apply(
                    RunConfig(
                        git_repo_root=repo_path,
                        git_target_dir=Path("laws"),
                    ),
                    run_manifest_path=plan_result.manifest_path,
                )

            run_manifest = json.loads(
                apply_result.manifest_path.read_text(encoding="utf-8")
            )
            self.assertIn("06_git_sink", run_manifest["stages"])


if __name__ == "__main__":
    unittest.main()
