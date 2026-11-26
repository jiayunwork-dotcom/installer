"""Target identity and conflict detection for opt-in installation preflight.

The objects in this module deliberately describe *planned* writes without
reading or spooling payload streams.  Path mapping remains the destination's
responsibility; this module only compares the resulting identities.
"""

import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from installer.exceptions import InstallationConflictError
from installer.utils import Scheme

__all__ = [
    "InstallSource",
    "InstallationPlan",
    "PlannedTarget",
    "TargetConflict",
    "check_targets",
    "format_conflicts",
]


@dataclass(frozen=True)
class InstallSource:
    """A source that can contribute a path to an installation."""

    kind: str
    """Machine-readable source kind."""

    description: str
    """Human-readable description, sufficient to locate the origin."""

    name: str | None = None
    """Kind-specific name, when available."""

    wheel_path: str | None = None
    """Original path in the wheel, when this source is wheel-backed."""

    scheme: Scheme | None = None
    """Logical scheme used for mapping, if known."""

    logical_path: str | None = None
    """Path within :attr:`scheme`, if known."""

    location: str | None = None
    """Best-effort originating wheel/source location."""

    def __str__(self) -> str:
        return self.description


@dataclass(frozen=True)
class PlannedTarget:
    """One source's fully resolved filesystem target."""

    source: InstallSource
    scheme: Scheme
    logical_path: str
    filename: Path
    """Absolute lexical path used by the destination."""

    identity: str
    """Canonical identity used for conflict comparison."""

    @classmethod
    def create(
        cls,
        *,
        source: InstallSource,
        scheme: Scheme,
        logical_path: str,
        filename: "os.PathLike[str] | str",
    ) -> "PlannedTarget":
        """Create a target, normalizing its lexical and physical identities."""
        absolute = Path(os.path.abspath(os.fspath(filename)))  # noqa: PTH100
        # realpath() also catches aliases through pre-existing symlinked scheme
        # directories.  It does not create any files or directories.
        identity = os.path.realpath(absolute)
        return cls(
            source=source,
            scheme=scheme,
            logical_path=logical_path,
            filename=absolute,
            identity=identity,
        )


ConflictRelation = Literal["same-file", "file-parent"]


@dataclass(frozen=True)
class InstallationPlan:
    """All target identities for one prospective installation."""

    root_scheme: Scheme
    record_file_path: str
    targets: tuple[PlannedTarget, ...]


@dataclass(frozen=True)
class TargetConflict:
    """A pair or group of planned targets that cannot both be written."""

    relation: ConflictRelation
    targets: tuple[PlannedTarget, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        """Lexical absolute paths involved in the conflict."""
        return tuple(target.filename for target in self.targets)

    @property
    def sources(self) -> tuple[InstallSource, ...]:
        """Sources involved in the conflict."""
        return tuple(target.source for target in self.targets)


def check_targets(targets: Iterable[PlannedTarget]) -> tuple[TargetConflict, ...]:
    """Reject duplicate paths and file/directory path prefix conflicts."""
    target_tuple = tuple(targets)

    by_identity: dict[str, list[PlannedTarget]] = {}
    for target in target_tuple:
        by_identity.setdefault(target.identity, []).append(target)

    conflicts: list[TargetConflict] = []

    for group in by_identity.values():
        if len(group) > 1:
            conflicts.append(TargetConflict("same-file", tuple(group)))

    # A file at ".../a" prevents another file being written at ".../a/b",
    # because the second write needs ".../a" to be a directory.
    seen_parent_pairs: set[tuple[str, str]] = set()
    for child in target_tuple:
        parent = Path(child.identity).parent
        while True:
            parent_group = by_identity.get(os.fspath(parent))
            if parent_group is not None:
                for parent_target in parent_group:
                    pair = (parent_target.identity, child.identity)
                    if pair in seen_parent_pairs:
                        continue
                    seen_parent_pairs.add(pair)
                    conflicts.append(
                        TargetConflict("file-parent", (parent_target, child))
                    )

            if parent == parent.parent:
                break
            parent = parent.parent

    if conflicts:
        raise InstallationConflictError(conflicts)

    return tuple(conflicts)


def format_conflicts(conflicts: Iterable[TargetConflict]) -> str:
    """Format conflicts with their sources and resolved target paths."""
    conflict_tuple = tuple(conflicts)
    lines = [
        f"Installation preflight detected {len(conflict_tuple)} target "
        "conflict(s); no files or directories were created:"
    ]

    for index, conflict in enumerate(conflict_tuple, start=1):
        if conflict.relation == "same-file":
            lines.append(
                f"\n{index}. {len(conflict.targets)} sources claim the same file:"
            )
        else:
            lines.append(
                f"\n{index}. A file target is also an ancestor directory of "
                "another target:"
            )

        for target in conflict.targets:
            lines.append(f"   - {target.source}")
            if target.source.location:
                lines.append(f"     source wheel: {target.source.location}")
            lines.append(f"     target: {target.filename}")

    return "\n".join(lines)
