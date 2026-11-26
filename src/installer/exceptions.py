"""Errors raised from this package."""

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from installer.preflight import TargetConflict
    from installer.rollback import RollbackFailure

__all__ = [
    "ConflictError",
    "FileConflictError",
    "InstallConflictError",
    "InstallationConflictError",
    "InstallationRollbackError",
    "InstallerError",
    "InvalidWheelSource",
    "PreflightCheckError",
    "PreflightConflictError",
    "PreflightError",
    "PreflightNotSupportedError",
    "RollbackError",
    "RollbackNotSupportedError",
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


class RollbackError(InstallerError):
    """Base class for errors raised by the opt-in rollback installation mode."""


class RollbackNotSupportedError(RollbackError, NotImplementedError):
    """A destination cannot provide rollback support, rejected before writing.

    This is a :class:`NotImplementedError` (like
    :class:`PreflightNotSupportedError`) so callers can catch the conventional
    exception type when they do not care about rollback specifics.
    """


class InstallationRollbackError(RollbackError):
    """An installation failed and undoing its writes also failed.

    The original installation error is available as ``__cause__``.  The
    ``failures`` attribute holds one :class:`~installer.rollback.RollbackFailure`
    per target that could not be restored, pointing at the saved material (when
    any) needed to finish the recovery by hand.  ``journal_root`` is the kept
    journal directory (with a manifest) or ``None`` when not applicable.
    """

    def __init__(
        self,
        failures: "Sequence[RollbackFailure]",
        journal_root: "str | Path | None" = None,
        original_error: BaseException | None = None,
    ) -> None:
        """Build the error from failed undo targets and the kept journal."""
        from installer.rollback import format_rollback_failures

        self.failures = tuple(failures)
        self.journal_root: Path | None = (
            Path(journal_root) if journal_root is not None else None
        )
        self.original_error = original_error
        super().__init__(
            format_rollback_failures(
                self.failures,
                journal_root=self.journal_root,
                original_error=original_error,
            )
        )


# Compatibility aliases matching the naming of the preflight errors.
RollbackNotSupported = RollbackNotSupportedError
InstallRollbackError = InstallationRollbackError
