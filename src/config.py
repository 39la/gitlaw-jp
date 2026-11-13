from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


def serialize_path(path: Path | str) -> str:
    path_obj = path if isinstance(path, Path) else Path(path)
    try:
        return path_obj.relative_to(Path.cwd()).as_posix()
    except ValueError:
        return path_obj.as_posix()


@dataclass(frozen=True)
class RunConfig:
    """Runtime configuration for deterministic pipeline execution."""

    input_zip: Path = Path("payload/all_xml.zip")
    output_root: Path = Path("runs")
    xsd_path: Path = Path("payload/XMLSchemaForJapaneseLaw_v3.xsd")
    as_of: str | None = None
    git_repo_root: Path | None = None
    git_target_dir: Path = Path("laws")
    git_max_commits: int | None = None
    snapshot_parse_xml: bool = False
    max_embed_kb: int = 512
    branch_model: str = "dual"
    promulgation_branch_prefix: str = "promulgations"
    enforcement_branch_prefix: str = "enforcements"
    message_template: str = "default"
    law_types: tuple[str, ...] = ("法律",)
    law_ids: tuple[str, ...] = ()
    force: bool = False
    force_refs: bool = False
    source: str = "auto"

    def normalized(self) -> RunConfig:
        git_repo_root = (
            self.git_repo_root.expanduser() if self.git_repo_root is not None else None
        )
        return RunConfig(
            input_zip=self.input_zip.expanduser(),
            output_root=self.output_root.expanduser(),
            xsd_path=self.xsd_path.expanduser(),
            as_of=self.as_of,
            git_repo_root=git_repo_root,
            git_target_dir=self.git_target_dir,
            git_max_commits=self.git_max_commits,
            snapshot_parse_xml=self.snapshot_parse_xml,
            max_embed_kb=self.max_embed_kb,
            branch_model=self.branch_model,
            promulgation_branch_prefix=self.promulgation_branch_prefix,
            enforcement_branch_prefix=self.enforcement_branch_prefix,
            message_template=self.message_template,
            law_types=tuple(self.law_types),
            law_ids=tuple(self.law_ids),
            force=self.force,
            force_refs=self.force_refs,
            source=self.source,
        )
