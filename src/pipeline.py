from __future__ import annotations

import json
import logging
import shutil
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import RunConfig, serialize_path
from .contracts import ApplyResult, GraphPlanManifest, IngestManifest, PipelineResult
from .core.git_sink import execute_commit_graph_plan
from .core.models import CommitGraphPlan
from .core.planner import (
    build_commit_graph_plan,
    timelines_from_jsonl,
    write_graph_plan,
)
from .core.strategies import AmendmentLawNumGroupingStrategy, build_metadata_strategy
from .stages.ingest import ingest_zip, write_ingest_manifest
from .stages.normalize_versions import create_normalized_versions
from .stages.timelines import create_timelines
from .stages.validate import validate_input, write_validation_manifest

logger = logging.getLogger(__name__)
_JST = timezone(timedelta(hours=9))


def _write_json_manifest(path: Path, payload: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")
    return path


def _write_run_manifest_v3(
    *,
    config: RunConfig,
    dataset_id: str,
    run_id: str,
    effective_as_of: str | None,
    ingest_manifest: IngestManifest,
    ingest_manifest_path: Path,
    validate_manifest_path: Path,
    normalize_manifest_path: Path,
    timeline_manifest_path: Path,
    graph_plan_manifest_path: Path,
    git_sink_manifest_path: Path | None = None,
) -> Path:
    run_root = config.output_root / run_id
    run_root.mkdir(parents=True, exist_ok=True)

    stages: dict[str, str] = {
        "01_ingest": serialize_path(ingest_manifest_path),
        "02_validate": serialize_path(validate_manifest_path),
        "03_normalize_versions": serialize_path(normalize_manifest_path),
        "04_build_timelines": serialize_path(timeline_manifest_path),
        "05_graph_plan": serialize_path(graph_plan_manifest_path),
    }
    if git_sink_manifest_path is not None:
        stages["06_git_sink"] = serialize_path(git_sink_manifest_path)

    payload: dict[str, object] = {
        "dataset_id": dataset_id,
        "run_id": run_id,
        "schema_version": 3,
        "branch_model": config.branch_model,
        "input_zip": serialize_path(config.input_zip),
        "as_of": effective_as_of,
        "source": ingest_manifest.source_name,
        "branch_prefixes": {
            "promulgation": config.promulgation_branch_prefix,
            "enforcement": config.enforcement_branch_prefix,
        },
        "message_template": config.message_template,
        "law_types": list(config.law_types),
        "stages": stages,
    }
    return _write_json_manifest(run_root / "manifest.json", payload)


def _load_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve_run_path(path_str: str) -> Path:
    path = Path(path_str)
    if path.is_absolute():
        return path
    return Path.cwd() / path


def _load_commit_graph_plan(path: Path) -> CommitGraphPlan:
    return CommitGraphPlan.from_dict(_load_json(path))


def run_plan(config: RunConfig) -> PipelineResult:
    conf = config.normalized()
    grouping = AmendmentLawNumGroupingStrategy()
    effective_as_of = conf.as_of

    logger.info("=" * 70)
    logger.info("Starting Gitlaw-Jp plan pipeline")
    logger.info("Input ZIP: %s", conf.input_zip)
    logger.info("Branch model: %s", conf.branch_model)
    logger.info("=" * 70)

    ingest_manifest = ingest_zip(conf)
    run_root = conf.output_root / ingest_manifest.run_id
    if run_root.exists():
        if not conf.force:
            raise FileExistsError(f"run already exists: {serialize_path(run_root)}")
        shutil.rmtree(run_root)
    ingest_manifest_path = write_ingest_manifest(conf, ingest_manifest)

    validation_manifest = validate_input(conf, ingest_manifest)
    validate_manifest_path = write_validation_manifest(conf, validation_manifest)

    normalize_manifest = create_normalized_versions(conf, ingest_manifest)
    normalize_manifest_path = (
        conf.output_root
        / ingest_manifest.run_id
        / "stages"
        / "03_normalize_versions"
        / "manifest.json"
    )

    versions_jsonl = Path(normalize_manifest.output_jsonl)
    timeline_manifest = create_timelines(
        conf,
        dataset_id=ingest_manifest.dataset_id,
        run_id=ingest_manifest.run_id,
        versions_jsonl=versions_jsonl,
    )
    timeline_manifest_path = (
        conf.output_root
        / ingest_manifest.run_id
        / "stages"
        / "04_build_timelines"
        / "manifest.json"
    )

    timelines_jsonl = Path(timeline_manifest.output_jsonl)
    timelines = timelines_from_jsonl(timelines_jsonl)
    graph_plan = build_commit_graph_plan(
        run_id=ingest_manifest.run_id,
        timelines=timelines,
        target_dir=conf.git_target_dir.as_posix().strip("/"),
        promulgation_branch_prefix=conf.promulgation_branch_prefix,
        enforcement_branch_prefix=conf.enforcement_branch_prefix,
        grouping=grouping,
        metadata=build_metadata_strategy(conf.message_template),
        as_of=effective_as_of,
        max_commits=conf.git_max_commits,
    )

    graph_plan_dir = (
        conf.output_root / ingest_manifest.run_id / "stages" / "05_graph_plan"
    )
    graph_plan_path = graph_plan_dir / "graph_plan.json"
    write_graph_plan(graph_plan, graph_plan_path)
    graph_plan_manifest = GraphPlanManifest(
        dataset_id=ingest_manifest.dataset_id,
        run_id=ingest_manifest.run_id,
        source_timelines_jsonl=serialize_path(timelines_jsonl),
        planned_commit_count=len(graph_plan.planned_commits),
        ref_update_count=len(graph_plan.ref_updates),
        output_json=serialize_path(graph_plan_path),
    )
    graph_plan_manifest_path = _write_json_manifest(
        graph_plan_dir / "manifest.json",
        graph_plan_manifest.to_dict(),
    )

    run_manifest_path = _write_run_manifest_v3(
        config=conf,
        dataset_id=ingest_manifest.dataset_id,
        run_id=ingest_manifest.run_id,
        effective_as_of=effective_as_of,
        ingest_manifest=ingest_manifest,
        ingest_manifest_path=ingest_manifest_path,
        validate_manifest_path=validate_manifest_path,
        normalize_manifest_path=normalize_manifest_path,
        timeline_manifest_path=timeline_manifest_path,
        graph_plan_manifest_path=graph_plan_manifest_path,
    )

    logger.info("=" * 70)
    logger.info("Plan pipeline completed successfully")
    logger.info("Run ID: %s", ingest_manifest.run_id)
    logger.info("Manifest: %s", run_manifest_path)
    logger.info("=" * 70)

    return PipelineResult(
        run_id=ingest_manifest.run_id,
        manifest_path=run_manifest_path,
    )


def run_apply(config: RunConfig, *, run_manifest_path: Path) -> ApplyResult:
    conf = config.normalized()
    if conf.git_repo_root is None:
        raise ValueError("git_repo_root is required for apply")

    run_manifest = _load_json(run_manifest_path)
    run_id = str(run_manifest["run_id"])
    input_zip = _resolve_run_path(str(run_manifest["input_zip"]))
    graph_plan_manifest_path = _resolve_run_path(
        str(run_manifest["stages"]["05_graph_plan"])
    )
    graph_plan_manifest = _load_json(graph_plan_manifest_path)
    graph_plan_path = _resolve_run_path(str(graph_plan_manifest["output_json"]))
    graph_plan = _load_commit_graph_plan(graph_plan_path)
    source_name = str(run_manifest.get("source", conf.source))

    logger.info("=" * 70)
    logger.info("Starting apply")
    logger.info("Run ID: %s", run_id)
    logger.info("Source: %s", source_name)
    logger.info("Git repo root: %s", conf.git_repo_root)
    logger.info("Graph plan: %s", graph_plan_path)
    logger.info("=" * 70)

    sink_result = execute_commit_graph_plan(
        config=conf,
        plan=graph_plan,
        input_zip=input_zip,
        source_name=source_name,
    )
    git_sink_dir = run_manifest_path.parent / "stages" / "06_git_sink"
    git_sink_manifest_path = _write_json_manifest(
        git_sink_dir / "manifest.json",
        {
            "dataset_id": run_manifest.get("dataset_id", ""),
            "run_id": run_id,
            **sink_result.to_dict(),
        },
    )
    run_manifest["stages"]["06_git_sink"] = serialize_path(git_sink_manifest_path)
    _write_json_manifest(run_manifest_path, run_manifest)

    logger.info("=" * 70)
    logger.info("Apply completed successfully")
    logger.info("Run ID: %s", run_id)
    logger.info("Manifest: %s", run_manifest_path)
    logger.info("=" * 70)

    return ApplyResult(run_id=run_id, manifest_path=run_manifest_path)


def run_full(config: RunConfig) -> PipelineResult:
    conf = config.normalized()
    effective_as_of = conf.as_of
    if effective_as_of is None and conf.git_repo_root is not None:
        conf = replace(
            conf,
            as_of=datetime.now(_JST).strftime("%Y-%m-%d"),
        )

    plan_result = run_plan(conf)
    if conf.git_repo_root is not None:
        logger.info("Proceeding to apply for run_id=%s", plan_result.run_id)
        run_apply(conf, run_manifest_path=plan_result.manifest_path)
    return plan_result


def run_pipeline(config: RunConfig) -> PipelineResult:
    return run_full(config)
