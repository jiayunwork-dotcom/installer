"""Errors raised from this package."""

from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from installer.preflight import TargetConflict

__all__ = [
    "InstallerError",
    "InvalidWheelSource",
    "PreflightError",
    "PreflightCheckError",
    "PreflightNotSupportedError",
    "InstallationConflictError",
    "ConflictError",
    "PreflightConflictError",
    "InstallConflictError",
    "FileConflictError",
    "TargetConflictError",
]


class InstallerError(Exception):
    """All exceptions raised from this package's code."""


class InvalidWheelSource(InstallerError):
    """When a wheel source violates a contract, or is not supported."""


class PreflightError(InstallerError):
    """Base class for errors detected before writing to the destination."""


PreflightCheckError = PreflightError


class PreflightNotSupportedError(PreflightError, NotImplementedError):
    """A destination cannot provide the target identities needed for preflight."""


class InstallationConflictError(PreflightError, FileExistsError):
    """Two or more sources in one installation would claim the same location."""

    def __init__(self, conflicts: "Sequence[TargetConflict]") -> None:
        from installer.preflight import format_conflicts

        self.conflicts = tuple(conflicts)
        super().__init__(format_conflicts(self.conflicts))


# Shorter/compatibility aliases for callers that prefer other terminology.
ConflictError = InstallationConflictError
PreflightConflictError = InstallationConflictError
InstallConflictError = InstallationConflictError
FileConflictError = InstallationConflictError
TargetConflictError = InstallationConflictError
