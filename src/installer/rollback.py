"""Journal-based undo for opt-in failure-atomic installations.

The opt-in ``install(..., rollback=True)`` mode writes payload directly to the
destination, exactly like a normal installation -- there is no staged copy of
the new payload.  Before each target is first touched, this module snapshots
what the undo pass needs:

- the bytes and permission bits of a pre-existing regular file (copied to a
  temporary journal with a fixed-size buffer, so memory use stays bounded),
- the target string of a pre-existing symlink (and, for links, the bytes/mode of
  the file it resolves to),
- the absence of a target that this installation creates, and
- every ancestor directory that this installation creates on the way.

If a write fails, the reverse journal is played back: created files are removed,
overwritten files are restored byte-for-byte with their mode, original symlinks
are recreated, and only directories newly created by this installation are
removed, and only while they are empty.  Unrelated pre-existing content is left
alone.

Trade-off (chosen deliberately, see the docs): journaling the *old* bytes costs
temporary disk space proportional to the pre-existing files this install would
overwrite -- typically nothing on a fresh install -- rather than a second copy of
the wheel payload.  In exchange, the destination is only half-written during the
``install()`` call itself; on failure the undo runs before ``install()`` returns.
This covers ordinary in-process exceptions, not power loss or ``SIGKILL``.

If the undo pass itself fails, the journal directory is kept, a manifest is
written next to the backups, and
:class:`~installer.exceptions.InstallationRollbackError` reports both the
original installation error (as its cause) and every target that could not be
restored.
"""

import contextlib
import errno
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from installer.exceptions import InstallationRollbackError, RollbackNotSupportedError

if TYPE_CHECKING:
    from collections.abc import Iterable

    from installer.destinations import WheelDestination

__all__ = [
    "InstallationJournal",
    "RollbackFailure",
    "check_rollback_supported",
    "format_rollback_failures",
]

_COPY_BUFSIZE = 64 * 1024
_MANIFEST_NAME = "rollback-manifest.txt"


@dataclass
class RollbackFailure:
    """One target that the undo pass could not put back."""

    target: Path
    """Filesystem target whose restoration failed."""

    action: str
    """What the undo pass was trying to do: ``restore`` or ``remove-directory``."""

    error: BaseException
    """The error raised while undoing this target."""

    backup_path: Path | None = None
    """Saved bytes for this target inside the journal, when the target was
    overwritten.  ``None`` when the target was created by the installation (so
    undo only had to remove it) or was a directory."""


@dataclass
class _SavedTarget:
    kind: str
    mode: int | None = None
    backup_path: Path | None = None
    link_target: str | None = None


def check_rollback_supported(destination: "WheelDestination") -> None:
    """Reject destinations that cannot uphold the rollback guarantee.

    A destination opts in by exposing ``rollback_supported = True`` and, if it
    needs extra conditions, a ``check_rollback_supported()`` method.  The check
    runs before any destination directory or file is created.
    """
    if getattr(destination, "rollback_supported", False) is not True:
        raise RollbackNotSupportedError(
            "rollback=True requires a destination that declares "
            "`rollback_supported = True` and routes every write through "
            "resolve_target()/resolve_script_target()/resolve_record_target(); "
            f"{type(destination).__name__} does not. Refusing to fall back to a "
            "non-atomic install."
        )

    checker = getattr(destination, "check_rollback_supported", None)
    if callable(checker):
        checker()


def format_rollback_failures(
    failures: "Iterable[RollbackFailure]",
    journal_root: "Path | None" = None,
    original_error: "BaseException | None" = None,
) -> str:
    """Format undo failures, including the material needed for manual recovery."""
    failure_tuple = tuple(failures)
    lines = [
        (
            "Installation failed and rolling it back also failed; the destination "
            "may be partially installed."
        ),
    ]
    if original_error is not None:
        lines.append(
            "The original installation error "
            f"({type(original_error).__name__}: {original_error}) was chained as "
            "this exception's cause."
        )
    else:
        lines.append(
            "The original installation error is chained as this exception's cause."
        )
    if journal_root is not None:
        lines.append(f"The undo journal was kept at: {journal_root}")
    else:
        lines.append("The undo journal was kept for manual recovery.")
    lines.append(f"Targets that could not be restored ({len(failure_tuple)}):")
    for index, failure in enumerate(failure_tuple, start=1):
        lines.append(
            f"\n{index}. {failure.target} ({failure.action} failed: {failure.error!r})"
        )
        if failure.backup_path is not None:
            lines.append(f"   saved bytes: {failure.backup_path}")
    return "\n".join(lines)


class InstallationJournal:
    """Track the pre-installation state of every target an install touches."""

    def __init__(self) -> None:
        """Create a journal backed by a fresh temporary directory."""
        self._root = Path(tempfile.mkdtemp(prefix="installer-rollback-"))
        self._saved: dict[str, _SavedTarget] = {}
        self._created_dirs: list[Path] = []
        self._created_dirs_seen: set[str] = set()
        self._backup_count = 0
        self._finished = False

    @property
    def root(self) -> Path:
        """Directory holding backups and, after a failed undo, a manifest."""
        return self._root

    def prepare_target(self, target: "str | os.PathLike[str]") -> Path:
        """Snapshot ``target`` before it is first written by this install.

        Returns the absolute lexical path that was snapshotted.
        """
        path = Path(os.path.abspath(os.fspath(target)))  # noqa: PTH100
        key = os.fspath(path)
        if key in self._saved:
            return path

        self._record_missing_parents(path.parent)

        if path.is_symlink():
            link_target = path.readlink().as_posix()
            self._saved[key] = _SavedTarget("symlink", link_target=link_target)
            # A write through the link modifies the file it resolves to.
            resolved = Path(os.path.realpath(path))
            if resolved != path:
                self._record_missing_parents(resolved.parent)
                if resolved.is_file():
                    self._save_file(resolved)
                elif not resolved.exists():
                    self._saved[os.fspath(resolved)] = _SavedTarget("absent")
        elif path.is_file():
            self._save_file(path)
        elif path.exists():
            # A directory cannot be written over by ``open("wb")``.
            self._saved[key] = _SavedTarget("directory")
        else:
            self._saved[key] = _SavedTarget("absent")

        return path

    def _save_file(self, path: Path) -> None:
        key = os.fspath(path)
        if key in self._saved:
            return

        self._backup_count += 1
        backup_path = self._root / f"backup-{self._backup_count}"
        with path.open("rb") as source, backup_path.open("wb") as destination:
            shutil.copyfileobj(source, destination, _COPY_BUFSIZE)
        mode = stat.S_IMODE(path.stat().st_mode)
        self._saved[key] = _SavedTarget("file", mode=mode, backup_path=backup_path)

    def _record_missing_parents(self, directory: Path) -> None:
        missing: list[Path] = []
        current = directory
        # A dangling symlink is "missing" to exists() but is not a directory
        # this install may remove, so stop before it.
        while not current.exists() and not current.is_symlink():
            missing.append(current)
            current = current.parent
        # Shallowest first, so undo removes them deepest first.
        for path in reversed(missing):
            key = os.fspath(path)
            if key not in self._created_dirs_seen:
                self._created_dirs_seen.add(key)
                self._created_dirs.append(path)

    def rollback(self, original_error: "BaseException | None" = None) -> None:
        """Undo this transaction, or raise with the recovery material kept."""
        if self._finished:
            return

        failures: list[RollbackFailure] = []
        for key, saved in reversed(list(self._saved.items())):
            path = Path(key)
            try:
                if saved.kind == "absent":
                    _remove_created_file(path)
                elif saved.kind == "file":
                    assert saved.backup_path is not None
                    assert saved.mode is not None
                    _restore_file(saved.backup_path, path, saved.mode)
                elif saved.kind == "symlink":
                    assert saved.link_target is not None
                    _restore_symlink(path, saved.link_target)
            except Exception as exc:  # noqa: BLE001 - report any undo failure
                failures.append(
                    RollbackFailure(
                        target=path,
                        action="restore",
                        error=exc,
                        backup_path=saved.backup_path,
                    )
                )

        for directory in reversed(self._created_dirs):
            try:
                # Only directories created by this install are listed, and only
                # empty ones can be removed.  Non-empty directories may hold
                # unrelated content and are intentionally left in place.
                if directory.is_dir():
                    directory.rmdir()
            except OSError as exc:
                if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                    failures.append(
                        RollbackFailure(
                            target=directory,
                            action="remove-directory",
                            error=exc,
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - report any undo failure
                failures.append(
                    RollbackFailure(
                        target=directory,
                        action="remove-directory",
                        error=exc,
                    )
                )

        if failures:
            self._finished = True
            self._write_manifest(failures)
            error = InstallationRollbackError(
                failures,
                journal_root=self._root,
                original_error=original_error,
            )
            if original_error is not None:
                error.__cause__ = original_error
            raise error

        self.commit()

    def commit(self) -> None:
        """Discard the journal after a successful installation."""
        if self._finished:
            return
        self._finished = True
        shutil.rmtree(self._root, ignore_errors=True)

    def _write_manifest(self, failures: "Iterable[RollbackFailure]") -> None:
        """Leave a self-describing index of the recovery material."""
        lines = [
            "installer rollback manifest",
            f"journal: {self._root}",
            "",
            "The installation failed and undo did not fully succeed.",
            "Backups below can be copied back to their target paths.",
            "",
        ]
        for key, saved in self._saved.items():
            if saved.kind == "file":
                lines.append(f"saved file {saved.backup_path} -> {key}")
            elif saved.kind == "symlink":
                lines.append(f"saved symlink {key} -> {saved.link_target}")
        lines.append("")
        lines.append("failed targets:")
        for failure in failures:
            lines.append(f"- {failure.target}: {failure.error!r}")
        with contextlib.suppress(OSError):
            # The backups themselves are still on disk and the error message
            # lists them; failing to write the manifest must not mask the undo
            # error.
            (self._root / _MANIFEST_NAME).write_text("\n".join(lines) + "\n")


def _remove_created_file(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    # A missing target needs no work; a directory is not something this install
    # wrote and is left alone.


def _same_contents(backup_path: Path, target: Path) -> bool:
    """Whether ``target`` already holds exactly the saved bytes."""
    try:
        if not target.is_file() or target.stat().st_size != backup_path.stat().st_size:
            return False
        with backup_path.open("rb") as saved, target.open("rb") as current:
            while True:
                saved_chunk = saved.read(_COPY_BUFSIZE)
                current_chunk = current.read(_COPY_BUFSIZE)
                if saved_chunk != current_chunk:
                    return False
                if not saved_chunk:
                    return True
    except OSError:
        return False


def _restore_file(backup_path: Path, target: Path, mode: int) -> None:
    if target.is_symlink():
        target.unlink()
    if not _same_contents(backup_path, target):
        with backup_path.open("rb") as source, target.open("wb") as destination:
            shutil.copyfileobj(source, destination, _COPY_BUFSIZE)
    target.chmod(mode)


def _restore_symlink(target: Path, link_target: str) -> None:
    if target.is_symlink():
        if target.readlink().as_posix() == link_target:
            return
        target.unlink()
    elif target.is_file():
        target.unlink()
    target.symlink_to(link_target)
