from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..law_types import law_category_from_num, normalize_law_type
from .models import (
    AmendmentEvent,
    EnforcementUnit,
    law_storage_dir,
    LawVersion,
    PlannedFile,
    revision_to_effective_date_hint,
)

FALLBACK_SORT_DATE = "9999-12-31"
FALLBACK_COMMIT_DATE = "1970-01-01"

ERA_BASE_YEAR = {
    "明治": 1868,
    "大正": 1912,
    "昭和": 1926,
    "平成": 1989,
    "令和": 2019,
}
KANJI_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "元": 1,
}
KANJI_UNITS = {
    "十": 10,
    "百": 100,
    "千": 1000,
}
WAREKI_DATE_RE = re.compile(
    r"^(明治|大正|昭和|平成|令和)"
    r"([元〇零一二三四五六七八九十百千\d]+)年"
    r"([元〇零一二三四五六七八九十百千\d]+)月"
    r"([元〇零一二三四五六七八九十百千\d]+)日$"
)


class DateResolutionStrategy(Protocol):
    def normalize_date(self, date_text: str | None) -> str | None:
        """Normalize a date string into YYYY-MM-DD."""

    def resolve_commit_date(
        self,
        *,
        amendment_promulgation_date: str,
        effective_date: str,
        revision_id: str,
    ) -> tuple[str, str]:
        """Resolve a stable commit date and source label."""


class AmendmentGroupingStrategy(Protocol):
    def group_key(self, law_version: LawVersion) -> str:
        """Build a key used to group versions into one amendment event."""


class TimelineOrderingStrategy(Protocol):
    def sort_law_versions(self, versions: Sequence[LawVersion]) -> list[LawVersion]:
        """Sort versions within a law timeline."""

    def sort_amendment_events(
        self, events: Sequence[AmendmentEvent]
    ) -> list[AmendmentEvent]:
        """Sort amendment events deterministically."""

    def sort_enforcement_units(
        self, units: Sequence[EnforcementUnit]
    ) -> list[EnforcementUnit]:
        """Sort enforcement units deterministically."""


class EnforcementProjectionStrategy(Protocol):
    def should_include(self, unit: EnforcementUnit, *, as_of: str | None) -> bool:
        """Return whether the unit should be projected into enforcement branch."""


class MetadataEmissionStrategy(Protocol):
    def build_enforcement_message(self, unit: EnforcementUnit) -> str:
        """Render enforcement commit message."""

    def build_promulgation_side_message(self, unit: EnforcementUnit) -> str:
        """Render promulgation side-lineage commit message."""

    def build_promulgation_merge_message(
        self, amendment_event: AmendmentEvent, unit_count: int
    ) -> str:
        """Render promulgation merge commit message."""

    def enforcement_trailers(self, unit: EnforcementUnit) -> dict[str, str]:
        """Emit enforcement trailers."""

    def promulgation_side_trailers(self, unit: EnforcementUnit) -> dict[str, str]:
        """Emit promulgation side-lineage trailers."""

    def promulgation_merge_trailers(
        self, amendment_event: AmendmentEvent
    ) -> dict[str, str]:
        """Emit promulgation merge trailers."""

    def build_sidecar_file(
        self, *, projection: str, unit: EnforcementUnit
    ) -> PlannedFile:
        """Emit metadata sidecar."""


class RefLayoutStrategy(Protocol):
    def promulgation_ref(self, *, run_id: str, branch_prefix: str) -> str:
        """Return promulgation branch ref."""

    def enforcement_ref(self, *, run_id: str, branch_prefix: str) -> str:
        """Return enforcement branch ref."""


def _parse_japanese_number(text: str) -> int | None:
    normalized = text.strip()
    if not normalized:
        return None
    if normalized.isdigit():
        return int(normalized)

    total = 0
    current = 0
    has_any = False
    for ch in normalized:
        if ch in KANJI_DIGITS:
            current = KANJI_DIGITS[ch]
            has_any = True
            continue
        if ch in KANJI_UNITS:
            unit = KANJI_UNITS[ch]
            if current == 0:
                current = 1
            total += current * unit
            current = 0
            has_any = True
            continue
        return None

    if not has_any:
        return None
    return total + current


def _render_message(title: str, body_lines: list[str], trailers: dict[str, str]) -> str:
    lines = [title, ""]
    lines.extend(body_lines)
    lines.append("")
    for key, value in trailers.items():
        lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def _law_category_from_type(law_type: str) -> str:
    return normalize_law_type(law_type)


def _law_category_from_num(law_num: str) -> str:
    return law_category_from_num(law_num) or "法令"


def _display_title(name: str, num: str) -> str:
    if name and num:
        return f"{name}（{num}）"
    return name or num or "名称不明"


@dataclass(frozen=True)
class DefaultDateResolutionStrategy:
    def normalize_date(self, date_text: str | None) -> str | None:
        if not date_text:
            return None

        normalized_input = date_text.strip()
        if not normalized_input:
            return None

        if re.match(r"^\d{4}/\d{1,2}/\d{1,2}$", normalized_input):
            normalized_input = normalized_input.replace("/", "-")

        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(normalized_input, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass

        wareki_match = WAREKI_DATE_RE.match(normalized_input)
        if wareki_match is None:
            return None

        era_name = wareki_match.group(1)
        year_text = wareki_match.group(2)
        month_text = wareki_match.group(3)
        day_text = wareki_match.group(4)

        era_base = ERA_BASE_YEAR.get(era_name)
        if era_base is None:
            return None

        era_year = _parse_japanese_number(year_text)
        month = _parse_japanese_number(month_text)
        day = _parse_japanese_number(day_text)
        if era_year is None or month is None or day is None:
            return None

        western_year = era_base + era_year - 1
        try:
            return datetime(western_year, month, day).strftime("%Y-%m-%d")
        except ValueError:
            return None

    def resolve_commit_date(
        self,
        *,
        amendment_promulgation_date: str,
        effective_date: str,
        revision_id: str,
    ) -> tuple[str, str]:
        normalized_amendment = self.normalize_date(amendment_promulgation_date)
        if normalized_amendment is not None:
            return normalized_amendment, "amendment_promulgation_date"

        normalized_effective = self.normalize_date(effective_date)
        if normalized_effective is not None:
            return normalized_effective, "effective_date"

        revision_hint = revision_to_effective_date_hint(revision_id)
        if revision_hint is not None:
            return revision_hint, "revision_id"

        return FALLBACK_COMMIT_DATE, "sentinel"


@dataclass(frozen=True)
class AmendmentLawNumGroupingStrategy:
    def group_key(self, law_version: LawVersion) -> str:
        num = law_version.amendment_law_num.strip()
        date = law_version.amendment_promulgation_date.strip()
        if num:
            return f"{num}:{date}" if date else num
        return f"ungrouped:{law_version.law_id}:{law_version.revision_id}"


@dataclass(frozen=True)
class DefaultTimelineOrderingStrategy:
    date_resolution: DateResolutionStrategy = DefaultDateResolutionStrategy()

    def sort_law_versions(self, versions: Sequence[LawVersion]) -> list[LawVersion]:
        def key(version: LawVersion) -> tuple[str, str]:
            effective = (
                version.effective_date_hint
                or self.date_resolution.normalize_date(version.effective_date)
                or FALLBACK_SORT_DATE
            )
            return effective, version.revision_id

        return sorted(versions, key=key)

    def sort_amendment_events(
        self, events: Sequence[AmendmentEvent]
    ) -> list[AmendmentEvent]:
        def key(event: AmendmentEvent) -> tuple[str, str, str]:
            date = (
                self.date_resolution.normalize_date(event.amendment_promulgation_date)
                or FALLBACK_SORT_DATE
            )
            return date, event.amendment_law_num, event.group_key

        return sorted(events, key=key)

    def sort_enforcement_units(
        self, units: Sequence[EnforcementUnit]
    ) -> list[EnforcementUnit]:
        def key(unit: EnforcementUnit) -> tuple[str, str, str, int]:
            date = (
                self.date_resolution.normalize_date(unit.effective_date)
                or self.date_resolution.normalize_date(
                    unit.amendment_event.amendment_promulgation_date
                )
                or unit.law_revision.effective_date_hint
                or FALLBACK_SORT_DATE
            )
            return (
                date,
                unit.law_revision.law_id,
                unit.law_revision.revision_id,
                unit.phase_index,
            )

        return sorted(units, key=key)


@dataclass(frozen=True)
class AsOfEnforcementProjectionStrategy:
    date_resolution: DateResolutionStrategy = DefaultDateResolutionStrategy()

    def should_include(self, unit: EnforcementUnit, *, as_of: str | None) -> bool:
        if as_of is None:
            return True
        if unit.is_marked_unenforced:
            return False

        normalized_as_of = self.date_resolution.normalize_date(as_of)
        normalized_effective = self.date_resolution.normalize_date(unit.effective_date)
        if normalized_as_of is None or normalized_effective is None:
            return False
        return normalized_effective <= normalized_as_of


@dataclass(frozen=True)
class DefaultMetadataEmissionStrategy:
    def build_enforcement_message(self, unit: EnforcementUnit) -> str:
        trailers = self.enforcement_trailers(unit)
        return _render_message(
            (
                f"[gitlaw][施行][{normalize_law_type(unit.law_type, unit.law_num)}] "
                f"{_display_title(unit.law_name, unit.law_num)}"
            ),
            [
                f"法令種別: {normalize_law_type(unit.law_type, unit.law_num)}",
                f"法令名: {unit.law_name}",
                f"法令番号: {unit.law_num}",
                f"法令ID: {unit.law_revision.law_id}",
                f"改正版ID: {unit.law_revision.revision_id}",
                f"改正法令名: {unit.amendment_event.amendment_law_name}",
                f"改正法令番号: {unit.amendment_event.amendment_law_num}",
                f"施行日: {unit.effective_date}",
                f"施行備考: {unit.effective_note}",
                f"フェーズ: {unit.phase_index}",
                f"公布日: {unit.amendment_event.amendment_promulgation_date}",
            ],
            trailers,
        )

    def build_promulgation_side_message(self, unit: EnforcementUnit) -> str:
        trailers = self.promulgation_side_trailers(unit)
        return _render_message(
            (
                f"[gitlaw][公布準備][{normalize_law_type(unit.law_type, unit.law_num)}] "
                f"{_display_title(unit.law_name, unit.law_num)}"
            ),
            [
                f"法令種別: {normalize_law_type(unit.law_type, unit.law_num)}",
                f"法令名: {unit.law_name}",
                f"法令番号: {unit.law_num}",
                f"法令ID: {unit.law_revision.law_id}",
                f"改正版ID: {unit.law_revision.revision_id}",
                f"改正法令名: {unit.amendment_event.amendment_law_name}",
                f"改正法令番号: {unit.amendment_event.amendment_law_num}",
                f"公布日: {unit.amendment_event.amendment_promulgation_date}",
                f"施行予定日: {unit.effective_date}",
                f"フェーズ: {unit.phase_index}",
            ],
            trailers,
        )

    def build_promulgation_merge_message(
        self, amendment_event: AmendmentEvent, unit_count: int
    ) -> str:
        trailers = self.promulgation_merge_trailers(amendment_event)
        return _render_message(
            (
                f"[gitlaw][公布][{_law_category_from_num(amendment_event.amendment_law_num)}] "
                f"{_display_title(amendment_event.amendment_law_name, amendment_event.amendment_law_num)}"
            ),
            [
                f"改正法令主体: {_display_title(amendment_event.amendment_law_name, amendment_event.amendment_law_num)}",
                f"改正法令名: {amendment_event.amendment_law_name}",
                f"改正法令番号: {amendment_event.amendment_law_num}",
                f"公布日: {amendment_event.amendment_promulgation_date}",
                f"対象ユニット数: {unit_count}",
                f"対象法令数: {len(amendment_event.affected_law_ids)}",
            ],
            trailers,
        )

    def enforcement_trailers(self, unit: EnforcementUnit) -> dict[str, str]:
        return {
            "Projection": "enforcement",
            "Amendment-Id": unit.amendment_event.amendment_id,
            "Unit-Id": unit.unit_id,
            "Law-Id": unit.law_revision.law_id,
            "Revision-Id": unit.law_revision.revision_id,
            "Phase-Index": str(unit.phase_index),
        }

    def promulgation_side_trailers(self, unit: EnforcementUnit) -> dict[str, str]:
        return {
            "Projection": "promulgation",
            "Amendment-Id": unit.amendment_event.amendment_id,
            "Unit-Id": unit.unit_id,
            "Law-Id": unit.law_revision.law_id,
            "Revision-Id": unit.law_revision.revision_id,
            "Phase-Index": str(unit.phase_index),
        }

    def promulgation_merge_trailers(
        self, amendment_event: AmendmentEvent
    ) -> dict[str, str]:
        return {
            "Projection": "promulgation",
            "Amendment-Id": amendment_event.amendment_id,
        }

    def build_sidecar_file(
        self, *, projection: str, unit: EnforcementUnit
    ) -> PlannedFile:
        payload = {
            "projection": projection,
            "amendment_id": unit.amendment_event.amendment_id,
            "unit_id": unit.unit_id,
            "law_id": unit.law_revision.law_id,
            "revision_id": unit.law_revision.revision_id,
            "law_type": unit.law_type,
            "law_name": unit.law_name,
            "law_num": unit.law_num,
            "phase_index": unit.phase_index,
            "effective_date": unit.effective_date,
            "effective_note": unit.effective_note,
            "amendment_event": unit.amendment_event.to_dict(),
        }
        return PlannedFile(
            path=f"{law_storage_dir(unit.law_revision.law_id)}/current.json",
            kind="inline_text",
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
        )


@dataclass(frozen=True)
class CompactMetadataEmissionStrategy(DefaultMetadataEmissionStrategy):
    def build_enforcement_message(self, unit: EnforcementUnit) -> str:
        trailers = self.enforcement_trailers(unit)
        return _render_message(
            (
                f"[gitlaw][施行][{normalize_law_type(unit.law_type, unit.law_num)}] "
                f"{_display_title(unit.law_name, unit.law_num)}"
            ),
            [
                f"改正法令: {_display_title(unit.amendment_event.amendment_law_name, unit.amendment_event.amendment_law_num)}",
                f"施行日: {unit.effective_date}",
                f"改正版ID: {unit.law_revision.revision_id}",
            ],
            trailers,
        )

    def build_promulgation_side_message(self, unit: EnforcementUnit) -> str:
        trailers = self.promulgation_side_trailers(unit)
        return _render_message(
            (
                f"[gitlaw][公布準備][{normalize_law_type(unit.law_type, unit.law_num)}] "
                f"{_display_title(unit.law_name, unit.law_num)}"
            ),
            [
                f"改正法令: {_display_title(unit.amendment_event.amendment_law_name, unit.amendment_event.amendment_law_num)}",
                f"施行予定日: {unit.effective_date}",
                f"改正版ID: {unit.law_revision.revision_id}",
            ],
            trailers,
        )


@dataclass(frozen=True)
class DefaultRefLayoutStrategy:
    def promulgation_ref(self, *, run_id: str, branch_prefix: str) -> str:
        return f"refs/heads/{run_id}/{branch_prefix}"

    def enforcement_ref(self, *, run_id: str, branch_prefix: str) -> str:
        return f"refs/heads/{run_id}/{branch_prefix}"


def build_metadata_strategy(template: str) -> MetadataEmissionStrategy:
    if template == "compact":
        return CompactMetadataEmissionStrategy()
    if template == "default":
        return DefaultMetadataEmissionStrategy()
    raise ValueError(f"unsupported message template: {template}")


def resolve_primary_amendment_event(
    versions: Sequence[LawVersion],
    *,
    grouping_strategy: AmendmentGroupingStrategy,
) -> AmendmentEvent:
    amendment_law_num = ""
    amendment_law_name = ""
    amendment_promulgation_date = ""
    affected_law_ids: set[str] = set()

    for version in versions:
        if not amendment_law_num and version.amendment_law_num.strip():
            amendment_law_num = version.amendment_law_num.strip()
        if not amendment_law_name and version.amendment_law_name.strip():
            amendment_law_name = version.amendment_law_name.strip()
        if (
            not amendment_promulgation_date
            and version.amendment_promulgation_date.strip()
        ):
            amendment_promulgation_date = version.amendment_promulgation_date.strip()
        affected_law_ids.add(version.law_id)

    group_key = grouping_strategy.group_key(versions[0]) if versions else "ungrouped"
    amendment_id = f"promulgation:{group_key}"
    return AmendmentEvent(
        amendment_id=amendment_id,
        group_key=group_key,
        amendment_law_num=amendment_law_num,
        amendment_law_name=amendment_law_name,
        amendment_promulgation_date=amendment_promulgation_date,
        affected_law_ids=tuple(sorted(affected_law_ids)),
    )
