"""Tests for opt-in failure rollback (``install(..., rollback=True)``)."""

from __future__ import annotations

import base64
import hashlib
import io
import os
import stat
import tracemalloc
import zipfile
from pathlib import Path

import pytest

import installer.rollback as rollback_module
from installer import (
    InstallationRollbackError,
    RollbackError,
    RollbackNotSupportedError,
    install,
)
from installer.__main__ import _main as main
from installer.destinations import SchemeDictionaryDestination, WheelDestination
from installer.rollback import InstallationJournal
from installer.sources import WheelFile, WheelSource
from installer.utils import SCHEME_NAMES

DIST = "demo"
VERSION = "1.0.0"
DIST_INFO = f"{DIST}-{VERSION}.dist-info"
DATA_DIR = f"{DIST}-{VERSION}.data"


@pytest.fixture(autouse=True)
def _cleanup_kept_journals():
    """Remove journals that failed-undo tests intentionally keep for recovery."""
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("installer-rollback-*"))
    yield
    for path in set(Path(tempfile.gettempdir()).glob("installer-rollback-*")):
        if path not in before:
            import shutil

            shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
def _hash64(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_wheel(
    directory: Path,
    files: dict[str, bytes] | None = None,
    *,
    entry_points: bytes | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    archive_files: dict[str, bytes] = {
        f"{DIST_INFO}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{DIST_INFO}/METADATA": (
            f"Metadata-Version: 2.1\nName: {DIST}\nVersion: {VERSION}\n".encode()
        ),
    }
    if entry_points is not None:
        archive_files[f"{DIST_INFO}/entry_points.txt"] = entry_points
    archive_files.update(files or {})

    record_lines = [
        f"{name},sha256={_hash64(contents)},{len(contents)}"
        for name, contents in archive_files.items()
    ]
    record_lines.append(f"{DIST_INFO}/RECORD,,")
    archive_files[f"{DIST_INFO}/RECORD"] = ("\n".join(record_lines) + "\n").encode()

    wheel_path = directory / f"{DIST}-{VERSION}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w", compression=compression) as archive:
        for name, contents in archive_files.items():
            archive.writestr(name, contents)
    return wheel_path


class ExplodingStream(io.BytesIO):
    """A stream that fails part-way through its payload."""

    def read(self, size: int = -1) -> bytes:
        raise OSError("source stream failed while reading payload")


class _ControllableDestination(SchemeDictionaryDestination):
    """SchemeDictionaryDestination whose writes can be made to fail on demand."""

    fail_write_suffix: str | None = None
    fail_record: bool = False

    def write_file(self, scheme, path, stream, is_executable):
        if self.fail_write_suffix and str(path).endswith(self.fail_write_suffix):
            # Consume nothing; the failure happens before this file appears.
            raise OSError(f"destination write failed for {path}")
        return super().write_file(scheme, path, stream, is_executable)

    def finalize_installation(self, scheme, record_file_path, records):
        if self.fail_record:
            raise OSError("RECORD write failed")
        return super().finalize_installation(scheme, record_file_path, records)


def _dest(root: Path, *, overwrite_existing: bool = False, **kwargs):
    return _ControllableDestination(
        scheme_dict={scheme: str(root / scheme) for scheme in SCHEME_NAMES},
        interpreter="/test/bin/python",
        script_kind="posix",
        bytecode_optimization_levels=(),
        overwrite_existing=overwrite_existing,
        **kwargs,
    )


def _snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    """Map relative path -> (contents, mode) for every file under ``root``."""
    result: dict[str, tuple[bytes, int]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result[str(path.relative_to(root))] = (
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
        elif path.is_symlink():
            result[str(path.relative_to(root))] = (
                path.readlink().as_posix().encode(),
                stat.S_IMODE(path.lstat().st_mode),
            )
    return result


def _dir_snapshot(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {str(p.relative_to(root)) for p in root.rglob("*") if p.is_dir()}


class _StreamSource(WheelSource):
    """A tiny in-memory source whose last payload stream can be made to fail."""

    def __init__(self, members, *, fail_member: str | None = None) -> None:
        super().__init__("demo", "1.0.0")
        self._members = members
        self._fail_member = fail_member

    @property
    def dist_info_filenames(self):
        return ["WHEEL", "METADATA"]

    def read_dist_info(self, filename):
        if filename == "WHEEL":
            return "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        if filename == "METADATA":
            return "Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n"
        raise KeyError(filename)

    def get_contents(self):
        for name, data in self._members:
            if name == self._fail_member:
                yield ((name, "", str(len(data))), ExplodingStream(data), False)
            else:
                yield ((name, "", str(len(data))), io.BytesIO(data), False)
        for name in self.dist_info_filenames:
            data = self.read_dist_info(name).encode()
            yield (
                (f"{self.dist_info_dir}/{name}", "", str(len(data))),
                io.BytesIO(data),
                False,
            )


# --------------------------------------------------------------------------------------
# 1. Opt-in, library and CLI
# --------------------------------------------------------------------------------------
def test_rollback_is_opt_in(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    dest = _dest(tmp_path / "inst")
    # A destination without rollback support is rejected only when requested.
    with WheelFile.open(wheel) as source:
        install(source, dest, {})  # normal mode keeps working


def test_rollback_rejects_unsupported_destination_before_writing(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    root = tmp_path / "inst"

    class PlainDestination(WheelDestination):
        def write_file(self, *a, **k):
            raise AssertionError("must not be called")

    with WheelFile.open(wheel) as source, pytest.raises(RollbackNotSupportedError):
        install(source, PlainDestination(), {}, rollback=True)
    assert not root.exists()


def test_rollback_rejects_bytecode_compilation_before_writing(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    root = tmp_path / "inst"
    dest = SchemeDictionaryDestination(
        scheme_dict={s: str(root / s) for s in SCHEME_NAMES},
        interpreter="/test/bin/python",
        script_kind="posix",
        bytecode_optimization_levels=[0],
    )
    with WheelFile.open(wheel) as source, pytest.raises(RollbackNotSupportedError):
        install(source, dest, {}, rollback=True)
    assert not root.exists()


def test_cli_rollback(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    destdir = tmp_path / "dest"
    main([str(wheel), "-d", str(destdir), "--rollback"], "python -m installer")
    installed = list(destdir.rglob("demo/mod.py"))
    assert len(installed) == 1
    assert list(destdir.rglob("*.pyc")) == []


def test_cli_rejects_rollback_with_compile_bytecode(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    args = [
        str(wheel),
        "-d",
        str(tmp_path / "d"),
        "--rollback",
        "--compile-bytecode",
        "0",
    ]
    with pytest.raises(SystemExit):
        main(args, "python -m installer")
    assert not (tmp_path / "d").exists()


def test_cli_rollback_with_preflight(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    destdir = tmp_path / "dest"
    main(
        [str(wheel), "-d", str(destdir), "--rollback", "--preflight"],
        "python -m installer",
    )
    assert len(list(destdir.rglob("demo/mod.py"))) == 1


def test_cli_multi_wheel_rolls_back_only_the_failing_wheel(tmp_path, monkeypatch):
    first = make_wheel(tmp_path / "w1", {"demo/one.py": b"1"})
    # A distinct distribution so the two wheels do not collide on paths.
    second_dir = tmp_path / "w2"
    second_dir.mkdir()
    import zipfile

    dist_info = "other-1.0.0.dist-info"
    second = second_dir / "other-1.0.0-py3-none-any.whl"
    members = {
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/METADATA": b"Metadata-Version: 2.1\nName: other\nVersion: 1.0.0\n",
        "other/mod.py": b"2",
    }
    rows = [
        f"{name},sha256={_hash64(blob)},{len(blob)}" for name, blob in members.items()
    ]
    rows.append(f"{dist_info}/RECORD,,")
    members[f"{dist_info}/RECORD"] = ("\n".join(rows) + "\n").encode()
    with zipfile.ZipFile(second, "w") as archive:
        for name, blob in members.items():
            archive.writestr(name, blob)

    destdir = tmp_path / "dest"
    real_write_file = SchemeDictionaryDestination.write_file

    def fail_for_other(self, scheme, path, stream, is_executable):
        if "other/mod.py" in str(path):
            raise OSError("second wheel failed")
        return real_write_file(self, scheme, path, stream, is_executable)

    monkeypatch.setattr(SchemeDictionaryDestination, "write_file", fail_for_other)
    with pytest.raises(OSError, match="second wheel failed"):
        main(
            [str(first), str(second), "-d", str(destdir), "--rollback"],
            "python -m installer",
        )

    # The first wheel's installation succeeded and is retained; only the
    # second wheel's writes were undone.
    assert len(list(destdir.rglob("demo/one.py"))) == 1
    assert not list(destdir.rglob("other/mod.py"))


def test_cli_rollback_undoes_on_failure(tmp_path, monkeypatch):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    destdir = tmp_path / "dest"

    def failing_write_file(self, scheme, path, stream, is_executable):
        raise OSError("forced failure")

    monkeypatch.setattr(SchemeDictionaryDestination, "write_file", failing_write_file)
    with pytest.raises(OSError, match="forced failure"):
        main([str(wheel), "-d", str(destdir), "--rollback"], "python -m installer")

    # The created scheme tree was removed again.
    assert not any(destdir.rglob("demo"))


def test_cli_entrypoint_accepts_rollback(tmp_path):
    import subprocess
    import sys
    import textwrap

    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    src = Path(__file__).resolve().parent.parent / "src"
    runner = textwrap.dedent(
        """
        import sys
        sys.path.insert(0, sys.argv[1])
        from installer.__main__ import _main
        _main(sys.argv[2:], "python -m installer")
        """
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            runner,
            str(src),
            str(wheel),
            "-d",
            str(tmp_path / "d"),
            "--rollback",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(list((tmp_path / "d").rglob("demo/mod.py"))) == 1


# --------------------------------------------------------------------------------------
# 2. Success matches the normal flow; no mode keeps existing behaviour
# --------------------------------------------------------------------------------------
def test_successful_rollback_matches_normal_install(tmp_path):
    files = {
        "demo/__init__.py": b"def main():\n    print('hi')\n",
        f"{DATA_DIR}/data/demo/msg.txt": b"hello\n",
        f"{DATA_DIR}/scripts/demo-legacy": b"#!python\nprint('x')\n",
    }
    entry_points = b"[console_scripts]\ndemo-cli = demo:main\n"

    normal_wheel = make_wheel(tmp_path / "w1", files, entry_points=entry_points)
    rollback_wheel = make_wheel(tmp_path / "w2", files, entry_points=entry_points)

    normal_root = tmp_path / "normal"
    rollback_root = tmp_path / "rollback"
    with WheelFile.open(normal_wheel) as source:
        install(source, _dest(normal_root), {})
    with WheelFile.open(rollback_wheel) as source:
        install(source, _dest(rollback_root), {}, rollback=True)

    assert _snapshot(normal_root) == _snapshot(rollback_root)
    assert _dir_snapshot(normal_root) == _dir_snapshot(rollback_root)


def test_rollback_success_leaves_no_journal(tmp_path, monkeypatch):
    created = []
    real_mkdtemp = rollback_module.tempfile.mkdtemp

    def tracking_mkdtemp(*a, **k):
        path = real_mkdtemp(*a, **k)
        created.append(Path(path))
        return path

    monkeypatch.setattr(rollback_module.tempfile, "mkdtemp", tracking_mkdtemp)

    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    with WheelFile.open(wheel) as source:
        install(source, _dest(tmp_path / "inst"), {}, rollback=True)

    assert created and all(not path.exists() for path in created)


def test_non_rollback_failure_keeps_partial_install(tmp_path):
    """Without rollback the pre-existing (partial) behaviour is unchanged."""
    root = tmp_path / "inst"
    source = _StreamSource(
        [("demo/a.py", b"a"), ("demo/b.py", b"b")], fail_member="demo/b.py"
    )
    with pytest.raises(OSError):
        install(source, _dest(root), {})

    # demo/a.py was written before the failure and is left behind.
    assert (root / "purelib" / "demo" / "a.py").exists()


def test_rollback_with_destdir(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    destdir = tmp_path / "staging"
    dest = SchemeDictionaryDestination(
        scheme_dict={s: f"/usr/lib/{s}" for s in SCHEME_NAMES},
        interpreter="/test/bin/python",
        script_kind="posix",
        bytecode_optimization_levels=(),
        destdir=str(destdir),
    )
    with WheelFile.open(wheel) as source:
        install(source, dest, {}, rollback=True)
    assert len(list(destdir.rglob("demo/mod.py"))) == 1


def test_failed_script_write_restores_existing_script(tmp_path):
    wheel = make_wheel(
        tmp_path / "w",
        {"demo/__init__.py": b"def main():\n    pass\n", "demo/zzz.py": b"z"},
        entry_points=b"[console_scripts]\ndemo-cli = demo:main\n",
    )
    root = tmp_path / "inst"
    existing_script = root / "scripts" / "demo-cli"
    existing_script.parent.mkdir(parents=True)
    existing_script.write_bytes(b"OLD SCRIPT")
    existing_script.chmod(0o755)

    dest = _dest(root, overwrite_existing=True)
    dest.fail_write_suffix = "zzz.py"

    with WheelFile.open(wheel) as source, pytest.raises(OSError):
        install(source, dest, {}, rollback=True)

    assert existing_script.read_bytes() == b"OLD SCRIPT"
    assert stat.S_IMODE(existing_script.stat().st_mode) == 0o755


def test_unexpected_undo_error_is_still_reported(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    source = _StreamSource([("demo/mod.py", b"x"), ("demo/boom.py", b"b")])
    dest = _dest(root)
    dest.fail_write_suffix = "boom.py"

    def exploding_rollback(self, original_error=None):
        raise RuntimeError("undo machinery broke")

    monkeypatch.setattr(InstallationJournal, "rollback", exploding_rollback)
    with pytest.raises(InstallationRollbackError) as info:
        install(source, dest, {}, rollback=True)

    assert isinstance(info.value.__cause__, OSError)
    assert "destination write failed" in str(info.value.__cause__)
    assert "undo machinery broke" in str(info.value)
    assert any(f.action == "undo" for f in info.value.failures)


def test_file_exists_failure_mid_install_rolls_back_earlier_files(tmp_path):
    root = tmp_path / "inst"
    # No overwrite_existing: this pre-existing file makes the write of
    # "demo/zzz.py" fail once earlier files are already on disk.
    blocker = root / "purelib" / "demo" / "zzz.py"
    blocker.parent.mkdir(parents=True)
    blocker.write_bytes(b"PRE-EXISTING")
    before = _snapshot(root)

    source = _StreamSource([("demo/aaa.py", b"a"), ("demo/zzz.py", b"z")])
    with pytest.raises(FileExistsError):
        install(source, _dest(root), {}, rollback=True)

    # Earlier new file is gone; the pre-existing blocker is untouched.
    assert _snapshot(root) == before
    assert not (root / "purelib" / "demo" / "aaa.py").exists()


# --------------------------------------------------------------------------------------
# 3. Ordinary exceptions during stream read / target write restore everything
# --------------------------------------------------------------------------------------
def test_stream_read_failure_restores_created_files_and_directories(tmp_path):
    members = [
        ("demo/a.py", b"a = 1\n"),
        ("demo/b.py", b"b = 2\n"),
        ("demo/c.py", b"c = 3\n"),
    ]
    source = _StreamSource(members, fail_member="demo/c.py")
    root = tmp_path / "inst"

    with pytest.raises(OSError, match="source stream failed"):
        install(source, _dest(root), {}, rollback=True)

    # Not even the top-level scheme directory survives: it was created this run.
    assert not root.exists()


def test_stream_read_failure_preserves_unrelated_tree(tmp_path):
    root = tmp_path / "inst"
    pre_existing = root / "purelib" / "demo" / "keep.txt"
    pre_existing.parent.mkdir(parents=True)
    pre_existing.write_bytes(b"unrelated")
    before = _snapshot(root)
    dirs_before = _dir_snapshot(root)

    source = _StreamSource(
        [("demo/a.py", b"a = 1\n"), ("demo/b.py", b"b = 2\n")],
        fail_member="demo/b.py",
    )
    with pytest.raises(OSError):
        install(source, _dest(root), {}, rollback=True)

    assert _snapshot(root) == before
    assert _dir_snapshot(root) == dirs_before


def test_write_failure_restores_overwritten_file_bytes_and_mode(tmp_path):
    root = tmp_path / "inst"
    existing = root / "purelib" / "demo" / "mod.py"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"ORIGINAL CONTENT")
    existing.chmod(0o640)

    source = _StreamSource(
        [("demo/mod.py", b"replacement" * 100), ("demo/zzz.py", b"z")],
    )
    dest = _dest(root, overwrite_existing=True)
    dest.fail_write_suffix = "zzz.py"

    with pytest.raises(OSError, match="destination write failed"):
        install(source, dest, {}, rollback=True)

    assert existing.read_bytes() == b"ORIGINAL CONTENT"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o640
    assert not (root / "purelib" / "demo" / "zzz.py").exists()


def test_executable_bit_restored_after_failure(tmp_path):
    root = tmp_path / "inst"
    existing = root / "scripts" / "demo-tool"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"#!/bin/sh\necho old\n")
    existing.chmod(0o755)

    source = _StreamSource(
        [
            ("demo-1.0.0.data/scripts/demo-tool", b"#!python\nprint('new')\n"),
            ("demo/boom.py", b"b"),
        ],
    )
    dest = _dest(root, overwrite_existing=True)
    dest.fail_write_suffix = "boom.py"

    with pytest.raises(OSError):
        install(source, dest, {}, rollback=True)

    assert existing.read_bytes() == b"#!/bin/sh\necho old\n"
    assert stat.S_IMODE(existing.stat().st_mode) == 0o755
    assert os.access(existing, os.X_OK)


def test_script_generation_failure_leaves_nothing_behind(tmp_path):
    files = {"demo/__init__.py": b"def main():\n    pass\n"}
    wheel = make_wheel(
        tmp_path / "w", files, entry_points=b"[console_scripts]\ndemo-cli = demo:main\n"
    )
    root = tmp_path / "inst"

    class ScriptFails(_ControllableDestination):
        def write_script(self, name, module, attr, section):
            raise OSError("script generation failed")

    dest = ScriptFails(
        scheme_dict={s: str(root / s) for s in SCHEME_NAMES},
        interpreter="/test/bin/python",
        script_kind="posix",
        bytecode_optimization_levels=(),
    )
    with WheelFile.open(wheel) as source, pytest.raises(OSError):
        install(source, dest, {}, rollback=True)
    assert not root.exists()


def test_record_write_failure_rolls_back_whole_install(tmp_path):
    files = {"demo/__init__.py": b"def main():\n    pass\n"}
    wheel = make_wheel(tmp_path / "w", files)
    root = tmp_path / "inst"
    dest = _dest(root)
    dest.fail_record = True

    with WheelFile.open(wheel) as source, pytest.raises(OSError, match="RECORD"):
        install(source, dest, {}, rollback=True)

    assert not root.exists()


def test_record_failure_with_overwrite_restores_previous_files(tmp_path):
    root = tmp_path / "inst"
    old_record = root / "purelib" / f"{DIST_INFO}" / "RECORD"
    old_record.parent.mkdir(parents=True)
    old_record.write_bytes(b"old,record,here\n")
    old_mod = root / "purelib" / "demo" / "mod.py"
    old_mod.parent.mkdir(parents=True)
    old_mod.write_bytes(b"OLD MODULE")

    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"NEW MODULE"})
    dest = _dest(root, overwrite_existing=True)
    dest.fail_record = True

    with WheelFile.open(wheel) as source, pytest.raises(OSError):
        install(source, dest, {}, rollback=True)

    assert old_record.read_bytes() == b"old,record,here\n"
    assert old_mod.read_bytes() == b"OLD MODULE"


def test_only_created_empty_directories_are_removed(tmp_path):
    root = tmp_path / "inst"
    # This pre-existing directory has unrelated content; a nested directory
    # created by the install inside it becomes non-empty and must survive.
    unrelated = root / "purelib" / "other" / "file.txt"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"keep")

    source = _StreamSource(
        [("demo/a.py", b"a"), ("demo/sub/deep/b.py", b"b")],
        fail_member="demo/a.py",
    )
    with pytest.raises(OSError):
        install(source, _dest(root), {}, rollback=True)

    assert unrelated.read_bytes() == b"keep"
    assert (root / "purelib" / "other").is_dir()


# --------------------------------------------------------------------------------------
# 4. Unsupported custom destinations are refused before any write
# --------------------------------------------------------------------------------------
def test_custom_destination_without_support_is_refused(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})

    class Custom(WheelDestination):
        rollback_supported = False

        def write_file(self, *a, **k):  # pragma: no cover - must not be reached
            raise AssertionError("write_file called")

        def write_script(self, *a, **k):  # pragma: no cover
            raise AssertionError("write_script called")

        def finalize_installation(self, *a, **k):  # pragma: no cover
            raise AssertionError("finalize called")

    with WheelFile.open(wheel) as source, pytest.raises(RollbackError) as info:
        install(source, Custom(), {}, rollback=True)
    assert isinstance(info.value, NotImplementedError)


def test_destination_declaring_support_without_hooks_is_refused(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})

    class HalfDone(WheelDestination):
        rollback_supported = True
        # resolve_target is inherited and raises NotImplementedError.

    with WheelFile.open(wheel) as source, pytest.raises(RollbackError):
        install(source, HalfDone(), {}, rollback=True)


def test_destination_check_hook_can_reject_before_writes(tmp_path):
    wheel = make_wheel(tmp_path / "w", {"demo/mod.py": b"x = 1\n"})
    root = tmp_path / "inst"

    class Refusing(_ControllableDestination):
        def check_rollback_supported(self):
            raise RollbackNotSupportedError("nope")

    dest = Refusing(
        scheme_dict={s: str(root / s) for s in SCHEME_NAMES},
        interpreter="/test/bin/python",
        script_kind="posix",
        bytecode_optimization_levels=(),
    )
    with WheelFile.open(wheel) as source, pytest.raises(RollbackNotSupportedError):
        install(source, dest, {}, rollback=True)
    assert not root.exists()


# --------------------------------------------------------------------------------------
# 5. A failed undo keeps both errors and the recovery material
# --------------------------------------------------------------------------------------
def test_failed_undo_reports_both_errors_and_keeps_journal(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    existing = root / "purelib" / "demo" / "mod.py"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"ORIGINAL")

    source = _StreamSource([("demo/mod.py", b"replacement"), ("demo/boom.py", b"b")])
    dest = _dest(root, overwrite_existing=True)
    dest.fail_write_suffix = "boom.py"

    real_restore = rollback_module._restore_file

    def broken_restore(*args, **kwargs):
        raise OSError("undo storage unavailable")

    monkeypatch.setattr(rollback_module, "_restore_file", broken_restore)

    try:
        with pytest.raises(InstallationRollbackError) as info:
            install(source, dest, {}, rollback=True)
    finally:
        monkeypatch.setattr(rollback_module, "_restore_file", real_restore)

    error = info.value
    # Both the original install error and the undo failure are preserved.
    assert isinstance(error.__cause__, OSError)
    assert "destination write failed" in str(error.__cause__)
    assert "undo storage unavailable" in str(error)
    assert error.original_error is error.__cause__
    assert len(error.failures) == 1
    failure = error.failures[0]
    assert failure.target == existing
    assert failure.action == "restore"
    # Material needed to finish the recovery by hand is still on disk.
    assert error.journal_root is not None and error.journal_root.is_dir()
    assert failure.backup_path is not None and failure.backup_path.exists()
    assert failure.backup_path.read_bytes() == b"ORIGINAL"
    manifest = error.journal_root / "rollback-manifest.txt"
    assert manifest.exists()
    assert str(existing) in manifest.read_text()


def test_failed_undo_does_not_claim_success(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    source = _StreamSource([("demo/mod.py", b"content"), ("demo/boom.py", b"b")])
    dest = _dest(root)
    dest.fail_write_suffix = "boom.py"

    def broken_remove(*args, **kwargs):
        raise OSError("cannot unlink")

    monkeypatch.setattr(rollback_module, "_remove_created_file", broken_remove)
    with pytest.raises(InstallationRollbackError) as info:
        install(source, dest, {}, rollback=True)

    assert info.value.failures
    assert all(f.action == "restore" for f in info.value.failures)
    # The created file is still there because undo failed; the exception says so.
    assert (root / "purelib" / "demo" / "mod.py").exists()


# --------------------------------------------------------------------------------------
# 6. Bounded memory: 1 MiB vs 128 MiB payload peaks within 8 MiB
# --------------------------------------------------------------------------------------
def _install_peak(wheel: Path, dest_root: Path) -> int:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        with WheelFile.open(wheel) as source:
            install(source, _dest(dest_root), {}, rollback=True)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak


def _payload_wheel(tmp_path: Path, name: str, size: int) -> Path:
    # A mostly-incompressible payload so the stored member really spans ~size.
    chunk = b"".join(f"{i:08d}".encode() for i in range(1024))
    payload = (chunk * (size // len(chunk) + 1))[:size]
    files = {"demo/blob.bin": payload, "demo/mod.py": b"x = 1\n"}
    # Stored (not deflated) keeps decompression buffers out of the measurement.
    return make_wheel(tmp_path / name, files, compression=zipfile.ZIP_STORED)


def test_rollback_memory_peak_is_independent_of_payload_size(tmp_path):
    small = _payload_wheel(tmp_path, "small", 1 * 1024 * 1024)
    large = _payload_wheel(tmp_path, "large", 128 * 1024 * 1024)

    small_peak = _install_peak(small, tmp_path / "a")
    large_peak = _install_peak(large, tmp_path / "b")

    assert abs(large_peak - small_peak) <= 8 * 1024 * 1024, (
        f"1 MiB peak={small_peak}, 128 MiB peak={large_peak}"
    )


# --------------------------------------------------------------------------------------
# Journal unit tests
# --------------------------------------------------------------------------------------
def test_journal_restores_symlink_target(tmp_path):
    root = tmp_path / "inst"
    real_file = root / "real.txt"
    real_file.parent.mkdir(parents=True)
    real_file.write_bytes(b"real")
    link = root / "link.txt"
    link.symlink_to(real_file)

    journal = InstallationJournal()
    try:
        journal.prepare_target(link)
        # Simulate a write that follows the link and clobbers the real file.
        real_file.write_bytes(b"clobbered")
        journal.rollback()
    finally:
        journal.commit()

    assert link.is_symlink()
    assert link.readlink() == real_file
    assert real_file.read_bytes() == b"real"


def test_journal_preserves_original_symlink_target(tmp_path):
    root = tmp_path / "inst"
    root.mkdir()
    link = root / "link.txt"
    link.symlink_to("somewhere-else")

    journal = InstallationJournal()
    try:
        journal.prepare_target(link)
        link.unlink()
        link.write_bytes(b"replaced a dangling link")
        journal.rollback()
    finally:
        journal.commit()

    assert link.is_symlink()
    assert link.readlink().as_posix() == "somewhere-else"


def test_journal_records_are_idempotent(tmp_path):
    root = tmp_path / "inst"
    file = root / "purelib" / "f.txt"
    file.parent.mkdir(parents=True)
    file.write_bytes(b"one")

    journal = InstallationJournal()
    try:
        first = journal.prepare_target(file)
        second = journal.prepare_target(file)
        assert first == second
        file.write_bytes(b"two")
        journal.rollback()
    finally:
        journal.commit()

    assert file.read_bytes() == b"one"


def test_journal_leaves_directory_target_alone(tmp_path):
    root = tmp_path / "inst"
    directory = root / "purelib" / "pkg"
    directory.mkdir(parents=True)

    journal = InstallationJournal()
    try:
        journal.prepare_target(directory)
        journal.rollback()
    finally:
        journal.commit()

    assert directory.is_dir()


def test_journal_rollback_before_finish_is_idempotent(tmp_path):
    journal = InstallationJournal()
    journal.commit()
    # A second call is a no-op rather than an error.
    journal.rollback()
    journal.commit()


def test_failed_undo_keeps_symlink_material_in_manifest(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    root.mkdir()
    link = root / "link.txt"
    link.symlink_to("original-target")
    existing = root / "file.txt"
    existing.write_bytes(b"original")

    journal = InstallationJournal()
    journal.prepare_target(link)
    journal.prepare_target(existing)
    existing.write_bytes(b"changed")

    def broken_restore(*args, **kwargs):
        raise OSError("cannot restore")

    monkeypatch.setattr(rollback_module, "_restore_file", broken_restore)
    try:
        with pytest.raises(InstallationRollbackError) as info:
            journal.rollback()
    finally:
        journal.commit()

    manifest = (info.value.journal_root / "rollback-manifest.txt").read_text()
    assert "saved symlink" in manifest
    assert "original-target" in manifest
    assert str(existing) in manifest


def test_undo_removal_failure_is_reported(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    source = _StreamSource([("demo/mod.py", b"content"), ("demo/boom.py", b"b")])
    dest = _dest(root)
    dest.fail_write_suffix = "boom.py"

    real_rmdir = Path.rmdir

    def failing_rmdir(path):
        raise OSError(5, "I/O error", str(path))

    monkeypatch.setattr(Path, "rmdir", failing_rmdir)
    try:
        with pytest.raises(InstallationRollbackError) as info:
            install(source, dest, {}, rollback=True)
    finally:
        monkeypatch.setattr(Path, "rmdir", real_rmdir)

    actions = {failure.action for failure in info.value.failures}
    assert "remove-directory" in actions


def test_undo_removal_non_oserror_is_reported(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    source = _StreamSource([("demo/mod.py", b"content"), ("demo/boom.py", b"b")])
    dest = _dest(root)
    dest.fail_write_suffix = "boom.py"

    real_rmdir = Path.rmdir

    def failing_rmdir(path):
        raise RuntimeError("unexpected rmdir failure")

    monkeypatch.setattr(Path, "rmdir", failing_rmdir)
    try:
        with pytest.raises(InstallationRollbackError) as info:
            install(source, dest, {}, rollback=True)
    finally:
        monkeypatch.setattr(Path, "rmdir", real_rmdir)

    action_errors = {f.action: f.error for f in info.value.failures}
    assert "remove-directory" in action_errors
    assert isinstance(action_errors["remove-directory"], RuntimeError)


def test_manifest_write_failure_does_not_mask_undo_error(tmp_path, monkeypatch):
    root = tmp_path / "inst"
    existing = root / "purelib" / "demo" / "mod.py"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"ORIGINAL")

    source = _StreamSource([("demo/mod.py", b"new"), ("demo/boom.py", b"b")])
    dest = _dest(root, overwrite_existing=True)
    dest.fail_write_suffix = "boom.py"

    monkeypatch.setattr(
        rollback_module,
        "_restore_file",
        lambda *a, **k: (_ for _ in ()).throw(OSError("undo failed")),
    )
    real_write_text = Path.write_text
    monkeypatch.setattr(
        Path,
        "write_text",
        lambda *a, **k: (_ for _ in ()).throw(OSError("manifest failed")),
    )
    try:
        with pytest.raises(InstallationRollbackError) as info:
            install(source, dest, {}, rollback=True)
    finally:
        monkeypatch.setattr(Path, "write_text", real_write_text)

    assert isinstance(info.value.__cause__, OSError)
    assert "undo failed" in str(info.value)


def test_rollback_with_preflight_uses_plan_targets(tmp_path):
    root = tmp_path / "inst"
    existing = root / "purelib" / "demo" / "mod.py"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"ORIGINAL")

    source = _StreamSource([("demo/mod.py", b"new contents"), ("demo/boom.py", b"b")])
    dest = _dest(root, overwrite_existing=True)
    dest.fail_write_suffix = "boom.py"

    with pytest.raises(OSError, match="destination write failed"):
        install(source, dest, {}, preflight=True, rollback=True)

    assert existing.read_bytes() == b"ORIGINAL"


# --------------------------------------------------------------------------------------
# Journal unit-level coverage
# --------------------------------------------------------------------------------------
def test_journal_root_is_reported(tmp_path):
    journal = InstallationJournal()
    try:
        assert journal.root.is_dir()
    finally:
        journal.commit()


def test_format_rollback_failures_without_optional_context():
    target = Path("/some/target")
    failure = rollback_module.RollbackFailure(
        target=target, action="restore", error=OSError("boom")
    )
    text = rollback_module.format_rollback_failures([failure])
    assert "OriginalError" not in text
    assert "chained as this exception's cause" in text
    assert "manual recovery" in text
    assert str(target) in text


def test_journal_restore_file_replaces_symlink_at_target(tmp_path):
    root = tmp_path / "inst"
    root.mkdir()
    backup = tmp_path / "backup"
    backup.write_bytes(b"original")
    target = root / "file.txt"
    target.symlink_to(tmp_path / "elsewhere")

    rollback_module._restore_file(backup, target, 0o600)

    assert not target.is_symlink()
    assert target.read_bytes() == b"original"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_journal_save_file_is_idempotent(tmp_path):
    root = tmp_path / "inst"
    root.mkdir()
    file = root / "file.txt"
    file.write_bytes(b"original")

    journal = InstallationJournal()
    try:
        journal.prepare_target(file)
        # Saving the same path twice keeps the first (pre-install) snapshot.
        file.write_bytes(b"changed")
        journal._save_file(file)
        journal.rollback()
    finally:
        journal.commit()

    assert file.read_bytes() == b"original"


def test_additional_metadata_is_rolled_back(tmp_path):
    root = tmp_path / "inst"
    dest = _dest(root)
    source = _StreamSource([("demo/mod.py", b"x = 1\n")])
    # Force the metadata write (written later) to fail, after files are written.
    dest.fail_write_suffix = "extra.txt"

    with pytest.raises(OSError, match="destination write failed"):
        install(source, dest, {"extra.txt": b"generated"}, rollback=True)

    assert not (root / "purelib" / f"{DIST_INFO}" / "extra.txt").exists()
    assert not (root / "purelib" / "demo" / "mod.py").exists()


def test_rollback_skips_pycache_members_when_resolving(tmp_path):
    root = tmp_path / "inst"
    dest = _dest(root)
    source = _StreamSource(
        [("demo/ok.py", b"x = 1\n"), ("demo/__pycache__/skip.pyc", b"junk")]
    )
    with pytest.warns(RuntimeWarning):
        install(source, dest, {}, rollback=True)

    assert (root / "purelib" / "demo" / "ok.py").exists()
    assert not (root / "purelib" / "demo" / "__pycache__").exists()


def test_destination_without_resolve_target_is_rejected(tmp_path):
    root = tmp_path / "inst"

    class NoResolve(_ControllableDestination):
        rollback_supported = True

        def resolve_target(self, scheme, path):
            raise NotImplementedError

    dest = NoResolve(
        scheme_dict={s: str(root / s) for s in SCHEME_NAMES},
        interpreter="/test/bin/python",
        script_kind="posix",
        bytecode_optimization_levels=(),
    )
    source = _StreamSource([("demo/mod.py", b"x = 1\n")])
    with pytest.raises(RollbackNotSupportedError):
        install(source, dest, {}, rollback=True)
    assert not root.exists()


def test_undo_does_not_rewrite_unchanged_file(tmp_path, monkeypatch):
    backup = tmp_path / "backup"
    backup.write_bytes(b"SAME")
    target = tmp_path / "target"
    target.write_bytes(b"SAME")
    target.chmod(0o644)

    opened = []
    real_open = Path.open

    def tracking_open(self, *args, **kwargs):
        opened.append((self, args[0] if args else kwargs.get("mode")))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", tracking_open)
    rollback_module._restore_file(backup, target, 0o640)
    monkeypatch.setattr(Path, "open", real_open)

    # No "wb" open for an identical target; only the mode is applied.
    assert [path for path, mode in opened if path == target and mode == "wb"] == []
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_undo_recreates_replaced_symlink(tmp_path):
    root = tmp_path / "inst"
    root.mkdir()
    real = tmp_path / "real.txt"
    real.write_bytes(b"real")
    link = root / "link.txt"
    link.symlink_to(real)

    journal = InstallationJournal()
    try:
        journal.prepare_target(link)
        link.unlink()
        link.symlink_to(tmp_path / "different")
        journal.rollback()
    finally:
        journal.commit()

    assert link.is_symlink()
    assert link.readlink() == real


def test_same_contents_handles_unreadable_target(tmp_path):
    saved = tmp_path / "saved"
    saved.write_bytes(b"abc")
    missing = tmp_path / "missing"
    assert rollback_module._same_contents(saved, missing) is False

    unreadable = tmp_path / "unreadable"
    unreadable.write_bytes(b"abc")
    unreadable.chmod(0o000)
    try:
        assert rollback_module._same_contents(saved, unreadable) is False
    finally:
        unreadable.chmod(0o644)
