from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pygit2

from src.config import RunConfig
from src.core.git_sink import execute_commit_graph_plan
from src.core.models import law_storage_dir
from src.core.planner import build_commit_graph_plan, timelines_from_jsonl
from src.stages.ingest import ingest_zip
from src.stages.normalize_versions import create_normalized_versions
from src.stages.timelines import create_timelines


class CommitGraphSinkTests(unittest.TestCase):
    REAL_LAW_ID = "129AC0000000089"
    REAL_LAW_NAME = "民法"
    REAL_LAW_NUM = "明治二十九年法律第八十九号"
    REAL_LAST_REVISION_ID = "20280613_505AC0000000053"

    def _law_tree_prefix(self) -> str:
        return f"laws/{law_storage_dir(self.REAL_LAW_ID)}"

    def _repo_root(self) -> Path:
        return Path(__file__).resolve().parents[1]

    def _real_payload_zip(self) -> Path:
        return self._repo_root() / "payload" / "all_xml.zip"

    def _init_bare_repo(self, repo_path: Path) -> pygit2.Repository:
        return pygit2.init_repository(str(repo_path), bare=True)

    def _seed_dev_branch(self, repo: pygit2.Repository) -> pygit2.Commit:
        author = pygit2.Signature("tester", "tester@example.local", 0, 9 * 60)
        readme_oid = repo.create_blob("# Gitlaw-Jp\n")
        tree = repo.TreeBuilder()
        tree.insert("README.md", readme_oid, pygit2.GIT_FILEMODE_BLOB)
        commit_oid = repo.create_commit(
            "refs/heads/dev",
            author,
            author,
            "seed dev",
            tree.write(),
            [],
        )
        return repo[commit_oid]

    def _build_real_payload_plan(
        self, tmp: Path
    ) -> tuple[RunConfig, object, list[dict[str, object]], Path]:
        input_zip = self._real_payload_zip()
        if not input_zip.exists():
            self.skipTest(f"real payload not found: {input_zip}")

        config = RunConfig(
            input_zip=input_zip,
            output_root=tmp / "runs",
            law_ids=(self.REAL_LAW_ID,),
        )
        ingest_manifest = ingest_zip(config)
        normalize_manifest = create_normalized_versions(config, ingest_manifest)
        timeline_manifest = create_timelines(
            config,
            dataset_id=ingest_manifest.dataset_id,
            run_id=ingest_manifest.run_id,
            versions_jsonl=Path(normalize_manifest.output_jsonl),
        )
        versions = [
            json.loads(line)
            for line in Path(normalize_manifest.output_jsonl)
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        timelines = timelines_from_jsonl(Path(timeline_manifest.output_jsonl))
        plan = build_commit_graph_plan(
            run_id=ingest_manifest.run_id,
            timelines=timelines,
            target_dir="laws",
            promulgation_branch_prefix="promulgations",
            enforcement_branch_prefix="enforcements",
        )
        return config, plan, versions, input_zip

    def test_real_payload_creates_expected_refs_and_commit_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo_path = tmp / "repo"
            repo = self._init_bare_repo(repo_path)
            dev_tip = self._seed_dev_branch(repo)

            config, plan, versions, input_zip = self._build_real_payload_plan(tmp)
            result = execute_commit_graph_plan(
                config=RunConfig(
                    input_zip=config.input_zip,
                    output_root=config.output_root,
                    git_repo_root=repo_path,
                    git_target_dir=Path("laws"),
                    law_ids=config.law_ids,
                ),
                plan=plan,
                input_zip=input_zip,
            )

            self.assertEqual(len(versions), 4)
            self.assertEqual(result.promulgation_commit_count, 7)
            self.assertEqual(result.enforcement_commit_count, 4)
            self.assertIn(
                f"refs/heads/{plan.metadata['run_id']}/promulgations",
                result.updated_refs,
            )
            self.assertIn(
                f"refs/heads/{plan.metadata['run_id']}/enforcements",
                result.updated_refs,
            )
            self.assertNotIn("refs/heads/main", result.updated_refs)
            prom_ref = repo.lookup_reference(
                f"refs/heads/{plan.metadata['run_id']}/promulgations"
            )
            enf_ref = repo.lookup_reference(
                f"refs/heads/{plan.metadata['run_id']}/enforcements"
            )
            self.assertTrue(repo.descendant_of(prom_ref.target, dev_tip.id))
            self.assertTrue(repo.descendant_of(enf_ref.target, dev_tip.id))

    def test_real_payload_promulgation_tip_is_merge_commit_with_expected_message(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo_path = tmp / "repo"
            repo = self._init_bare_repo(repo_path)
            self._seed_dev_branch(repo)

            config, plan, _, input_zip = self._build_real_payload_plan(tmp)
            execute_commit_graph_plan(
                config=RunConfig(
                    input_zip=config.input_zip,
                    output_root=config.output_root,
                    git_repo_root=repo_path,
                    git_target_dir=Path("laws"),
                    law_ids=config.law_ids,
                ),
                plan=plan,
                input_zip=input_zip,
            )

            repo = pygit2.Repository(str(repo_path))
            prom_ref = repo.lookup_reference(
                f"refs/heads/{plan.metadata['run_id']}/promulgations"
            )
            prom_tip = repo[prom_ref.target]

            self.assertEqual(len(prom_tip.parents), 2)
            self.assertTrue(prom_tip.message.startswith("[gitlaw][公布][法律] "))
            self.assertIn("譲渡担保契約及び所有権留保契約", prom_tip.message)
            self.assertIn(
                "Amendment-Id: promulgation:令和七年法律第五十七号", prom_tip.message
            )

    def test_real_payload_promulgation_history_updates_current_xml_as_expected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo_path = tmp / "repo"
            repo = self._init_bare_repo(repo_path)
            self._seed_dev_branch(repo)

            config, plan, versions, input_zip = self._build_real_payload_plan(tmp)
            execute_commit_graph_plan(
                config=RunConfig(
                    input_zip=config.input_zip,
                    output_root=config.output_root,
                    git_repo_root=repo_path,
                    git_target_dir=Path("laws"),
                    law_ids=config.law_ids,
                ),
                plan=plan,
                input_zip=input_zip,
            )

            repo = pygit2.Repository(str(repo_path))
            prom_ref = repo.lookup_reference(
                f"refs/heads/{plan.metadata['run_id']}/promulgations"
            )
            prom_tip = repo[prom_ref.target]

            xml_path = f"{self._law_tree_prefix()}/current.xml"

            first_parent_history: list[pygit2.Commit] = []
            commit = prom_tip
            while True:
                first_parent_history.append(commit)
                if not commit.parents:
                    break
                commit = commit.parents[0]

            self.assertEqual(len(first_parent_history), 6)

            merge_commits = [
                item
                for item in first_parent_history
                if item.message.startswith("[gitlaw][公布][法律] ")
            ]
            self.assertEqual(len(merge_commits), 3)

            first_parent_xml_change_count = 0
            for commit in first_parent_history:
                try:
                    entry = commit.tree[xml_path]
                except KeyError:
                    continue
                parent_entry_id = None
                if commit.parents:
                    try:
                        parent_entry_id = commit.parents[0].tree[xml_path].id
                    except KeyError:
                        parent_entry_id = None
                if parent_entry_id != entry.id:
                    first_parent_xml_change_count += 1
            self.assertEqual(first_parent_xml_change_count, 4)

            reachable_ids: set[pygit2.Oid] = set()
            stack = [prom_tip]
            reachable_commits: list[pygit2.Commit] = []
            while stack:
                commit = stack.pop()
                if commit.id in reachable_ids:
                    continue
                reachable_ids.add(commit.id)
                reachable_commits.append(commit)
                stack.extend(commit.parents)

            side_commits = [
                item
                for item in reachable_commits
                if item.message.startswith(
                    f"[gitlaw][公布準備][法律] {self.REAL_LAW_NAME}（{self.REAL_LAW_NUM}）"
                )
            ]
            self.assertEqual(len(side_commits), 4)

            side_xml_blob_ids = {
                side_commit.tree[xml_path].id for side_commit in side_commits
            }
            self.assertEqual(len(side_xml_blob_ids), 4)

            tip_xml_blob = repo[prom_tip.tree[xml_path].id]
            tip_xml_sha = hashlib.sha256(tip_xml_blob.read_raw()).hexdigest()
            self.assertEqual(
                tip_xml_sha,
                versions[2]["xml_sha256"].split(":", 1)[1],
            )

    def test_real_payload_enforcement_history_updates_current_xml_four_times(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            repo_path = tmp / "repo"
            self._init_bare_repo(repo_path)

            config, plan, versions, input_zip = self._build_real_payload_plan(tmp)
            execute_commit_graph_plan(
                config=RunConfig(
                    input_zip=config.input_zip,
                    output_root=config.output_root,
                    git_repo_root=repo_path,
                    git_target_dir=Path("laws"),
                    law_ids=config.law_ids,
                ),
                plan=plan,
                input_zip=input_zip,
            )

            repo = pygit2.Repository(str(repo_path))
            enf_ref = repo.lookup_reference(
                f"refs/heads/{plan.metadata['run_id']}/enforcements"
            )
            enf_tip = repo[enf_ref.target]

            self.assertEqual(len(enf_tip.parents), 1)
            self.assertTrue(
                enf_tip.message.startswith(
                    f"[gitlaw][施行][法律] {self.REAL_LAW_NAME}（{self.REAL_LAW_NUM}）"
                )
            )
            self.assertIn(
                f"Revision-Id: {self.REAL_LAST_REVISION_ID}",
                enf_tip.message,
            )

            current_json_blob = repo[
                enf_tip.tree[f"{self._law_tree_prefix()}/current.json"].id
            ]
            current_json = json.loads(current_json_blob.read_raw().decode("utf-8"))
            self.assertEqual(current_json["law_id"], self.REAL_LAW_ID)
            self.assertEqual(current_json["revision_id"], self.REAL_LAST_REVISION_ID)
            self.assertEqual(current_json["law_type"], "法律")

            current_xml_blob = repo[
                enf_tip.tree[f"{self._law_tree_prefix()}/current.xml"].id
            ]
            current_xml_sha = hashlib.sha256(current_xml_blob.read_raw()).hexdigest()
            self.assertEqual(
                current_xml_sha, versions[-1]["xml_sha256"].split(":", 1)[1]
            )

            history: list[pygit2.Commit] = []
            commit = enf_tip
            while True:
                history.append(commit)
                if not commit.parents:
                    break
                commit = commit.parents[0]

            self.assertEqual(len(history), 4)
            self.assertTrue(
                all(
                    item.message.startswith(
                        f"[gitlaw][施行][法律] {self.REAL_LAW_NAME}（{self.REAL_LAW_NUM}）"
                    )
                    for item in history
                )
            )

            xml_path = f"{self._law_tree_prefix()}/current.xml"
            xml_change_count = 0
            for commit in history:
                entry = commit.tree[xml_path]
                parent_entry_id = None
                if commit.parents:
                    try:
                        parent_entry_id = commit.parents[0].tree[xml_path].id
                    except KeyError:
                        parent_entry_id = None
                if parent_entry_id != entry.id:
                    xml_change_count += 1

            self.assertEqual(xml_change_count, 4)


if __name__ == "__main__":
    unittest.main()
