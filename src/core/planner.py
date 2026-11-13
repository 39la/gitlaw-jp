from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import (
    AmendmentEvent,
    CommitGraphPlan,
    EnforcementUnit,
    LawTimeline,
    LawVersion,
    law_storage_dir,
    PlannedCommit,
    PlannedFile,
    RefUpdate,
)
from .strategies import (
    FALLBACK_COMMIT_DATE,
    AmendmentGroupingStrategy,
    AmendmentLawNumGroupingStrategy,
    AsOfEnforcementProjectionStrategy,
    DateResolutionStrategy,
    DefaultDateResolutionStrategy,
    DefaultMetadataEmissionStrategy,
    DefaultRefLayoutStrategy,
    DefaultTimelineOrderingStrategy,
    EnforcementProjectionStrategy,
    MetadataEmissionStrategy,
    RefLayoutStrategy,
    TimelineOrderingStrategy,
    resolve_primary_amendment_event,
)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def law_versions_from_jsonl(path: Path) -> list[LawVersion]:
    return [LawVersion.from_dict(row) for row in _read_jsonl(path)]


def timelines_from_jsonl(path: Path) -> list[LawTimeline]:
    timelines: list[LawTimeline] = []
    for row in _read_jsonl(path):
        raw_versions = row.get("versions", [])
        if not isinstance(raw_versions, list):
            raise TypeError("timeline versions must be a list")
        versions = [
            LawVersion.from_dict(version)
            for version in raw_versions
            if isinstance(version, dict)
        ]
        timelines.append(
            LawTimeline(law_id=str(row.get("law_id", "")), versions=versions)
        )
    return timelines


def build_law_timelines(
    law_versions: Sequence[LawVersion],
    *,
    ordering: TimelineOrderingStrategy | None = None,
) -> list[LawTimeline]:
    grouped: dict[str, list[LawVersion]] = defaultdict(list)
    for version in law_versions:
        grouped[version.law_id].append(version)

    strategy = ordering or DefaultTimelineOrderingStrategy()
    timelines: list[LawTimeline] = []
    for law_id, versions in sorted(grouped.items(), key=lambda item: item[0]):
        timelines.append(
            LawTimeline(law_id=law_id, versions=strategy.sort_law_versions(versions))
        )
    return timelines


def build_amendment_events(
    law_versions: Sequence[LawVersion],
    *,
    grouping: AmendmentGroupingStrategy,
    ordering: TimelineOrderingStrategy | None = None,
) -> list[AmendmentEvent]:
    grouped: dict[str, list[LawVersion]] = defaultdict(list)
    for version in law_versions:
        grouped[grouping.group_key(version)].append(version)

    events = [
        resolve_primary_amendment_event(versions, grouping_strategy=grouping)
        for versions in grouped.values()
    ]
    return (ordering or DefaultTimelineOrderingStrategy()).sort_amendment_events(events)


def build_enforcement_units(
    timelines: Sequence[LawTimeline],
    *,
    grouping: AmendmentGroupingStrategy,
    ordering: TimelineOrderingStrategy | None = None,
    projection: EnforcementProjectionStrategy | None = None,
    as_of: str | None = None,
) -> list[EnforcementUnit]:
    units: list[EnforcementUnit] = []
    for timeline in timelines:
        grouped_versions: dict[str, list[LawVersion]] = defaultdict(list)
        for version in timeline.versions:
            grouped_versions[grouping.group_key(version)].append(version)

        for versions in grouped_versions.values():
            event = resolve_primary_amendment_event(
                versions, grouping_strategy=grouping
            )
            sorted_versions = (
                ordering or DefaultTimelineOrderingStrategy()
            ).sort_law_versions(versions)
            for phase_index, version in enumerate(sorted_versions, start=1):
                units.append(
                    EnforcementUnit(
                        unit_id=(
                            f"enforcement:{version.law_id}:{version.revision_id}:p{phase_index}"
                        ),
                        law_revision=version.revision_ref,
                        law_type=version.law_type,
                        law_name=version.law_name,
                        law_num=version.law_num,
                        amendment_event=event,
                        effective_date=version.effective_date,
                        effective_note=version.effective_note,
                        phase_index=phase_index,
                        is_marked_unenforced=version.is_marked_unenforced,
                    )
                )

    apply_strategy = projection or AsOfEnforcementProjectionStrategy()
    filtered = [
        unit for unit in units if apply_strategy.should_include(unit, as_of=as_of)
    ]
    return (ordering or DefaultTimelineOrderingStrategy()).sort_enforcement_units(
        filtered
    )


def _resolve_commit_date(
    unit: EnforcementUnit,
    *,
    date_resolution: DateResolutionStrategy,
) -> str:
    date, _ = date_resolution.resolve_commit_date(
        amendment_promulgation_date=unit.amendment_event.amendment_promulgation_date,
        effective_date=unit.effective_date,
        revision_id=unit.law_revision.revision_id,
    )
    return date


def _build_unit_files(
    unit: EnforcementUnit,
    *,
    target_dir: str,
    metadata: MetadataEmissionStrategy,
    projection: str,
) -> list[PlannedFile]:
    sidecar = metadata.build_sidecar_file(projection=projection, unit=unit)
    law_dir = law_storage_dir(unit.law_revision.law_id)
    return [
        PlannedFile(
            path=f"{target_dir}/{law_dir}/current.xml",
            kind="zip_xml",
            law_id=unit.law_revision.law_id,
            revision_id=unit.law_revision.revision_id,
            xml_entry=unit.law_revision.xml_entry,
        ),
        PlannedFile(
            path=f"{target_dir}/{sidecar.path}",
            kind="inline_text",
            content=sidecar.content,
        ),
    ]


def build_commit_graph_plan(
    *,
    run_id: str,
    timelines: Sequence[LawTimeline],
    target_dir: str,
    promulgation_branch_prefix: str,
    enforcement_branch_prefix: str,
    grouping: AmendmentGroupingStrategy | None = None,
    ordering: TimelineOrderingStrategy | None = None,
    projection: EnforcementProjectionStrategy | None = None,
    metadata: MetadataEmissionStrategy | None = None,
    ref_layout: RefLayoutStrategy | None = None,
    date_resolution: DateResolutionStrategy | None = None,
    as_of: str | None = None,
    max_commits: int | None = None,
) -> CommitGraphPlan:
    grouping_strategy = grouping or AmendmentLawNumGroupingStrategy()
    ordering_strategy = ordering or DefaultTimelineOrderingStrategy()
    projection_strategy = projection or AsOfEnforcementProjectionStrategy()
    metadata_strategy = metadata or DefaultMetadataEmissionStrategy()
    ref_layout_strategy = ref_layout or DefaultRefLayoutStrategy()
    date_strategy = date_resolution or DefaultDateResolutionStrategy()

    all_versions = [version for timeline in timelines for version in timeline.versions]
    amendment_events = build_amendment_events(
        all_versions,
        grouping=grouping_strategy,
        ordering=ordering_strategy,
    )
    promulgation_units = build_enforcement_units(
        timelines,
        grouping=grouping_strategy,
        ordering=ordering_strategy,
        projection=None,
        as_of=None,
    )
    enforcement_units = build_enforcement_units(
        timelines,
        grouping=grouping_strategy,
        ordering=ordering_strategy,
        projection=projection_strategy,
        as_of=as_of,
    )

    units_by_amendment: dict[str, list[EnforcementUnit]] = defaultdict(list)
    for unit in promulgation_units:
        units_by_amendment[unit.amendment_event.amendment_id].append(unit)

    prom_ref = ref_layout_strategy.promulgation_ref(
        run_id=run_id,
        branch_prefix=promulgation_branch_prefix,
    )
    enf_ref = ref_layout_strategy.enforcement_ref(
        run_id=run_id,
        branch_prefix=enforcement_branch_prefix,
    )

    planned_commits: list[PlannedCommit] = []
    promulgation_tip_commit_id: str | None = None
    promulgation_merge_commit_id: str | None = None
    promulgation_commit_count = 0

    for event in amendment_events:
        if max_commits is not None and promulgation_commit_count >= max_commits:
            break
        event_units = ordering_strategy.sort_enforcement_units(
            units_by_amendment.get(event.amendment_id, [])
        )
        if not event_units:
            continue

        branch_base_commit_id = promulgation_tip_commit_id
        side_tip_commit_id = branch_base_commit_id

        for unit in event_units:
            commit_id = f"promulgation-side:{unit.unit_id}"
            parents = [side_tip_commit_id] if side_tip_commit_id else []
            planned_commits.append(
                PlannedCommit(
                    commit_id=commit_id,
                    projection="promulgation",
                    parent_commit_ids=parents,
                    files=_build_unit_files(
                        unit,
                        target_dir=target_dir,
                        metadata=metadata_strategy,
                        projection="promulgation",
                    ),
                    message=metadata_strategy.build_promulgation_side_message(unit),
                    trailers=metadata_strategy.promulgation_side_trailers(unit),
                    author_date=_resolve_commit_date(
                        unit, date_resolution=date_strategy
                    ),
                    target_ref=prom_ref,
                )
            )
            side_tip_commit_id = commit_id
            promulgation_commit_count += 1
            if max_commits is not None and promulgation_commit_count >= max_commits:
                break

        if side_tip_commit_id is None:
            continue

        merge_commit_id = f"promulgation-merge:{event.amendment_id}"
        parents = []
        if branch_base_commit_id is not None:
            parents.append(branch_base_commit_id)
        parents.append(side_tip_commit_id)
        planned_commits.append(
            PlannedCommit(
                commit_id=merge_commit_id,
                projection="promulgation",
                parent_commit_ids=parents,
                files=[],
                message=metadata_strategy.build_promulgation_merge_message(
                    event,
                    len(event_units),
                ),
                trailers=metadata_strategy.promulgation_merge_trailers(event),
                author_date=(
                    date_strategy.normalize_date(event.amendment_promulgation_date)
                    or FALLBACK_COMMIT_DATE
                ),
                target_ref=prom_ref,
                tree_source_commit_id=side_tip_commit_id,
            )
        )
        promulgation_commit_count += 1
        promulgation_tip_commit_id = merge_commit_id
        promulgation_merge_commit_id = merge_commit_id
        if max_commits is not None and promulgation_commit_count >= max_commits:
            break

    enforcement_tip_commit_id: str | None = None
    enforcement_commit_count = 0
    for unit in enforcement_units:
        if max_commits is not None and enforcement_commit_count >= max_commits:
            break
        commit_id = unit.unit_id
        planned_commits.append(
            PlannedCommit(
                commit_id=commit_id,
                projection="enforcement",
                parent_commit_ids=(
                    [enforcement_tip_commit_id] if enforcement_tip_commit_id else []
                ),
                files=_build_unit_files(
                    unit,
                    target_dir=target_dir,
                    metadata=metadata_strategy,
                    projection="enforcement",
                ),
                message=metadata_strategy.build_enforcement_message(unit),
                trailers=metadata_strategy.enforcement_trailers(unit),
                author_date=_resolve_commit_date(unit, date_resolution=date_strategy),
                target_ref=enf_ref,
            )
        )
        enforcement_tip_commit_id = commit_id
        enforcement_commit_count += 1

    ref_updates: list[RefUpdate] = []
    if promulgation_merge_commit_id is not None:
        ref_updates.append(
            RefUpdate(ref_name=prom_ref, commit_id=promulgation_merge_commit_id)
        )
    if enforcement_tip_commit_id is not None:
        ref_updates.append(
            RefUpdate(ref_name=enf_ref, commit_id=enforcement_tip_commit_id)
        )

    metadata_payload = {
        "run_id": run_id,
        "promulgation_ref": prom_ref,
        "enforcement_ref": enf_ref,
        "amendment_event_count": len(amendment_events),
        "enforcement_unit_count": len(enforcement_units),
        "promulgation_commit_count": promulgation_commit_count,
        "enforcement_commit_count": enforcement_commit_count,
    }
    return CommitGraphPlan(
        planned_commits=planned_commits,
        ref_updates=ref_updates,
        metadata=metadata_payload,
    )


def write_graph_plan(plan: CommitGraphPlan, out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8", newline="\n") as f:
        json.dump(plan.to_dict(), f, ensure_ascii=False, sort_keys=True, indent=2)
        f.write("\n")


def write_units_jsonl(units: Iterable[object], out_file: Path) -> None:
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with out_file.open("w", encoding="utf-8", newline="\n") as f:
        for unit in units:
            to_dict = getattr(unit, "to_dict", None)
            if to_dict is None:
                raise TypeError("unit must provide to_dict()")
            f.write(json.dumps(to_dict(), ensure_ascii=False, sort_keys=True))
            f.write("\n")


@dataclass(frozen=True)
class GraphPlanManifestV3:
    run_id: str
    source_timelines_jsonl: str
    planned_commit_count: int
    ref_update_count: int
    output_json: str

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "source_timelines_jsonl": self.source_timelines_jsonl,
            "planned_commit_count": self.planned_commit_count,
            "ref_update_count": self.ref_update_count,
            "output_json": self.output_json,
        }
