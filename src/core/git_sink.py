"""Commit graph sink: materialize planned commits and refs with pygit2."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from time import perf_counter
from zipfile import ZipFile

import pygit2

from ..config import RunConfig
from ..contracts import BulkSource
from ..sources import resolve_source
from .models import CommitGraphPlan, GraphSinkResult, PlannedCommit, PlannedFile
from .strategies import DefaultDateResolutionStrategy

logger = logging.getLogger(__name__)

_JST_OFFSET_MINUTES = 9 * 60
_AUTHOR_NAME = "gitlaw-jp"
_AUTHOR_EMAIL = "gitlaw-jp@example.local"
_FALLBACK_UNIX_TS = 0
_DEFAULT_BASE_REF = "refs/heads/dev"
_PROGRESS_INTERVAL = 500


def _resolve_unix_ts(date_str: str | None) -> int:
    if not date_str:
        return _FALLBACK_UNIX_TS
    strategy = DefaultDateResolutionStrategy()
    normalized = strategy.normalize_date(date_str)
    if normalized is None:
        return _FALLBACK_UNIX_TS
    try:
        from datetime import datetime, timedelta, timezone

        dt = datetime.strptime(normalized, "%Y-%m-%d").replace(
            hour=12,
            tzinfo=timezone(timedelta(hours=9)),
        )
        # libgit2/pygit2 does not reliably preserve negative commit timestamps
        # on this environment, so clamp pre-1970 dates to the Unix epoch and
        # keep the original domain date in commit metadata.
        return max(int(dt.timestamp()), _FALLBACK_UNIX_TS)
    except ValueError:
        return _FALLBACK_UNIX_TS


def _make_signature(unix_ts: int) -> pygit2.Signature:
    return pygit2.Signature(_AUTHOR_NAME, _AUTHOR_EMAIL, unix_ts, _JST_OFFSET_MINUTES)


def _update_files_in_tree(
    repo: pygit2.Repository,
    parent_tree_oid: pygit2.Oid | None,
    files: dict[str, str | bytes],
    *,
    blob_cache: dict[str, pygit2.Oid],
    tree_cache: dict[tuple[str | None, tuple[tuple[str, str], ...]], pygit2.Oid],
) -> pygit2.Oid:
    direct_blobs: dict[str, str | bytes] = {}
    by_dir: dict[str, dict[str, str | bytes]] = {}
    for path, content in files.items():
        head, _, tail = path.partition("/")
        if tail:
            by_dir.setdefault(head, {})[tail] = content
        else:
            direct_blobs[head] = content

    tree_builder = (
        repo.TreeBuilder(repo[parent_tree_oid])
        if parent_tree_oid is not None
        else repo.TreeBuilder()
    )

    direct_blob_oids: list[tuple[str, pygit2.Oid]] = []
    for name, content in direct_blobs.items():
        payload = content.encode("utf-8") if isinstance(content, str) else content
        blob_key = hashlib.sha256(payload).hexdigest()
        blob_oid = blob_cache.get(blob_key)
        if blob_oid is None:
            blob_oid = repo.create_blob(payload)
            blob_cache[blob_key] = blob_oid
        tree_builder.insert(name, blob_oid, pygit2.GIT_FILEMODE_BLOB)
        direct_blob_oids.append((name, str(blob_oid)))

    parent_tree_obj = repo[parent_tree_oid] if parent_tree_oid is not None else None
    child_tree_oids: list[tuple[str, str]] = []
    for dir_name, sub_files in by_dir.items():
        child_oid: pygit2.Oid | None = None
        if parent_tree_obj is not None:
            try:
                child_oid = parent_tree_obj[dir_name].id
            except KeyError:
                pass
        new_child_oid = _update_files_in_tree(
            repo,
            child_oid,
            sub_files,
            blob_cache=blob_cache,
            tree_cache=tree_cache,
        )
        tree_builder.insert(dir_name, new_child_oid, pygit2.GIT_FILEMODE_TREE)
        child_tree_oids.append((dir_name, str(new_child_oid)))

    cache_key = (
        str(parent_tree_oid) if parent_tree_oid is not None else None,
        tuple(sorted(direct_blob_oids + child_tree_oids)),
    )
    cached_tree_oid = tree_cache.get(cache_key)
    if cached_tree_oid is not None:
        return cached_tree_oid

    tree_oid = tree_builder.write()
    tree_cache[cache_key] = tree_oid
    return tree_oid


def _build_zip_index(zip_path: Path) -> dict[str, str]:
    with ZipFile(zip_path, "r") as zf:
        return {entry.replace("\\", "/"): entry for entry in zf.namelist()}


def _materialize_files(
    planned_files: list[PlannedFile],
    *,
    zf: ZipFile,
    zip_index: dict[str, str],
    source: BulkSource,
) -> dict[str, str]:
    materialized: dict[str, str] = {}
    for planned_file in planned_files:
        if planned_file.kind == "inline_text":
            materialized[planned_file.path] = planned_file.content or ""
            continue
        if planned_file.kind != "zip_xml":
            raise ValueError(f"unsupported planned file kind: {planned_file.kind}")
        if not planned_file.law_id or not planned_file.revision_id:
            raise ValueError("zip_xml planned file requires law_id and revision_id")
        materialized[planned_file.path] = source.read_xml(
            zf,
            zip_index,
            law_id=planned_file.law_id,
            revision_id=planned_file.revision_id,
            xml_entry=planned_file.xml_entry,
        ).decode("utf-8")
    return materialized


def _create_commit(
    repo: pygit2.Repository,
    *,
    planned_commit: PlannedCommit,
    parents: list[pygit2.Commit],
    tree_source_commit: pygit2.Commit | None,
    files: dict[str, str],
    author: pygit2.Signature,
    committer: pygit2.Signature,
    blob_cache: dict[str, pygit2.Oid],
    tree_cache: dict[tuple[str | None, tuple[tuple[str, str], ...]], pygit2.Oid],
) -> pygit2.Commit:
    base_tree_oid = None
    if tree_source_commit is not None:
        base_tree_oid = tree_source_commit.tree.id
    elif parents:
        base_tree_oid = parents[0].tree.id

    tree_oid = _update_files_in_tree(
        repo,
        base_tree_oid,
        files,
        blob_cache=blob_cache,
        tree_cache=tree_cache,
    )
    commit_oid = repo.create_commit(
        None,
        author,
        committer,
        planned_commit.message,
        tree_oid,
        [parent.id for parent in parents],
    )
    return repo[commit_oid]


def _resolve_base_commit(repo: pygit2.Repository) -> pygit2.Commit | None:
    try:
        base_ref = repo.lookup_reference(_DEFAULT_BASE_REF)
    except KeyError:
        return None
    return repo[base_ref.target]


def execute_commit_graph_plan(
    *,
    config: RunConfig,
    plan: CommitGraphPlan,
    input_zip: Path,
    source_name: str | None = None,
) -> GraphSinkResult:
    if not config.git_repo_root:
        raise ValueError("git_repo_root required for commit graph sink")

    source = resolve_source(source_name or config.source, input_zip)
    repo = pygit2.Repository(str(config.git_repo_root))
    zip_index = _build_zip_index(input_zip)
    commits_by_id: dict[str, pygit2.Commit] = {}
    base_commit = _resolve_base_commit(repo)
    promulgation_commit_count = len(
        [
            commit
            for commit in plan.planned_commits
            if commit.projection == "promulgation"
        ]
    )
    enforcement_commit_count = len(
        [
            commit
            for commit in plan.planned_commits
            if commit.projection == "enforcement"
        ]
    )

    logger.info(
        "Git sink starting: commits=%s (promulgation=%s, enforcement=%s), refs=%s, base_ref=%s",
        len(plan.planned_commits),
        promulgation_commit_count,
        enforcement_commit_count,
        len(plan.ref_updates),
        _DEFAULT_BASE_REF if base_commit is not None else "<none>",
    )

    total_materialize_seconds = 0.0
    total_commit_seconds = 0.0
    blob_cache: dict[str, pygit2.Oid] = {}
    tree_cache: dict[tuple[str | None, tuple[tuple[str, str], ...]], pygit2.Oid] = {}
    window_materialize_seconds = 0.0
    window_commit_seconds = 0.0
    window_commits = 0

    with ZipFile(input_zip, "r") as zf:
        for index, planned_commit in enumerate(plan.planned_commits, start=1):
            parents = [
                commits_by_id[parent_id]
                for parent_id in planned_commit.parent_commit_ids
            ]
            if not parents and base_commit is not None:
                parents = [base_commit]
            tree_source_commit = (
                commits_by_id[planned_commit.tree_source_commit_id]
                if planned_commit.tree_source_commit_id is not None
                else None
            )
            if tree_source_commit is None and not planned_commit.parent_commit_ids:
                tree_source_commit = base_commit

            materialize_started_at = perf_counter()
            files = _materialize_files(
                planned_commit.files,
                zf=zf,
                zip_index=zip_index,
                source=source,
            )
            materialize_elapsed = perf_counter() - materialize_started_at

            signature = _make_signature(_resolve_unix_ts(planned_commit.author_date))

            commit_started_at = perf_counter()
            commits_by_id[planned_commit.commit_id] = _create_commit(
                repo,
                planned_commit=planned_commit,
                parents=parents,
                tree_source_commit=tree_source_commit,
                files=files,
                author=signature,
                committer=signature,
                blob_cache=blob_cache,
                tree_cache=tree_cache,
            )
            commit_elapsed = perf_counter() - commit_started_at

            total_materialize_seconds += materialize_elapsed
            total_commit_seconds += commit_elapsed
            window_materialize_seconds += materialize_elapsed
            window_commit_seconds += commit_elapsed
            window_commits += 1

            if (
                index == 1
                or index % _PROGRESS_INTERVAL == 0
                or index == len(plan.planned_commits)
            ):
                logger.info(
                    (
                        "Git sink progress: %s/%s commits written (%s, %s) "
                        "[window avg: materialize=%.4fs commit=%.4fs, "
                        "totals: materialize=%.2fs commit=%.2fs]"
                    ),
                    index,
                    len(plan.planned_commits),
                    planned_commit.projection,
                    planned_commit.commit_id,
                    window_materialize_seconds / window_commits,
                    window_commit_seconds / window_commits,
                    total_materialize_seconds,
                    total_commit_seconds,
                )
                window_materialize_seconds = 0.0
                window_commit_seconds = 0.0
                window_commits = 0

    updated_refs: list[str] = []
    logger.info("Git sink updating %s refs", len(plan.ref_updates))
    for ref_update in plan.ref_updates:
        commit = commits_by_id[ref_update.commit_id]
        repo.references.create(ref_update.ref_name, commit.id, force=config.force_refs)
        updated_refs.append(ref_update.ref_name)
    logger.info("Git sink completed: refs updated=%s", len(updated_refs))
    return GraphSinkResult(
        commit_count=len(plan.planned_commits),
        promulgation_commit_count=promulgation_commit_count,
        enforcement_commit_count=enforcement_commit_count,
        updated_refs=updated_refs,
    )
