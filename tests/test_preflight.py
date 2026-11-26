from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from installer import (
    InstallationConflictError,
    install,
    preflight_installation,
)
from installer.__main__ import _main as main
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import PreflightNotSupportedError
from installer.preflight import check_targets
from installer.sources import WheelFile
from installer.utils import SCHEME_NAMES

DIST = "demo"
VERSION = "1.0.0"
DIST_INFO = f"{DIST}-{VERSION}.dist-info"
DATA_DIR = f"{DIST}-{VERSION}.data"


def _hash64(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_wheel(
    directory: Path,
    files: dict[str, bytes] | None = None,
    *,
    entry_points: str | bytes | None = None,
    dist: str = DIST,
    version: str = VERSION,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    dist_info = f"{dist}-{version}.dist-info"

    archive_files: dict[str, bytes] = {
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\n"
            b"Generator: preflight-tests\n"
            b"Root-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.1\n"
            + f"Name: {dist}\nVersion: {version}\n".encode()
            + b"Summary: preflight test wheel\n"
        ),
    }
    if entry_points is not None:
        if isinstance(entry_points, str):
            entry_points = entry_points.encode()
        archive_files[f"{dist_info}/entry_points.txt"] = entry_points

    for name, contents in (files or {}).items():
        archive_files[name] = contents

    record_lines = []
    for name, contents in archive_files.items():
        record_lines.append(f"{name},sha256={_hash64(contents)},{len(contents)}")
    record_lines.append(f"{dist_info}/RECORD,,")
    archive_files[f"{dist_info}/RECORD"] = ("\n".join(record_lines) + "\n").encode()

    wheel_path = directory / f"{dist}-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, contents in archive_files.items():
            archive.writestr(name, contents)
    return wheel_path


def make_destination(
    root: Path,
    *,
    destdir: Path | None = None,
    interpreter: str = "/test/bin/python",
    overwrite_existing: bool = False,
    **scheme_overrides: str,
) -> SchemeDictionaryDestination:
    scheme_dict = {scheme: str(root / scheme) for scheme in SCHEME_NAMES}
    scheme_dict.update(scheme_overrides)
    return SchemeDictionaryDestination(
        scheme_dict=scheme_dict,
        interpreter=interpreter,
        script_kind="posix",
        bytecode_optimization_levels=(),
        destdir=str(destdir) if destdir is not None else None,
        overwrite_existing=overwrite_existing,
    )


def conflict_text(exc: pytest.ExceptionInfo[InstallationConflictError]) -> str:
    return str(exc.value)


def test_different_scheme_names_with_same_directory_conflict_before_writes(tmp_path):
    shared = tmp_path / "shared-site"
    scheme_root = tmp_path / "schemes"
    destination = make_destination(
        scheme_root,
        purelib=str(shared),
        platlib=str(shared),
    )
    wheel = make_wheel(
        tmp_path / "wheels",
        {
            "demo/mod.py": b"# ordinary root member\n",
            f"{DATA_DIR}/purelib/demo/mod.py": b"# purelib copy\n",
            f"{DATA_DIR}/platlib/demo/mod.py": b"# platlib copy\n",
        },
    )

    with WheelFile.open(wheel) as source:
        with pytest.raises(InstallationConflictError) as exc_info:
            install(source, destination, {}, preflight=True)

    error = exc_info.value
    assert len(error.conflicts) == 1
    conflict = error.conflicts[0]
    assert conflict.relation == "same-file"
    assert {target.source.kind for target in conflict.targets} == {"wheel-member"}

    message = conflict_text(exc_info)
    assert "wheel member 'demo/mod.py'" in message
    assert f"{DATA_DIR}/purelib/demo/mod.py" in message
    assert f"{DATA_DIR}/platlib/demo/mod.py" in message
    expected_target = Path(shared / "demo" / "mod.py").absolute()
    assert str(expected_target) in message

    assert not shared.exists()
    assert not scheme_root.exists()


def test_preflight_rejects_internal_conflict_even_when_overwrite_allowed(tmp_path):
    shared = tmp_path / "shared"
    destination = make_destination(
        tmp_path / "schemes",
        overwrite_existing=True,
        purelib=str(shared),
        platlib=str(shared),
    )
    wheel = make_wheel(
        tmp_path / "wheels",
        {
            f"{DATA_DIR}/purelib/demo/same.py": b"a\n",
            f"{DATA_DIR}/platlib/demo/same.py": b"b\n",
        },
    )

    with WheelFile.open(wheel) as source:
        with pytest.raises(InstallationConflictError):
            install(source, destination, {}, preflight=True)

    assert not shared.exists()


def test_destdir_is_part_of_reported_identity(tmp_path):
    shared_scheme = tmp_path / "configured" / "site-packages"
    destdir = tmp_path / "staging" / "destdir"
    destination = make_destination(
        tmp_path / "schemes",
        destdir=destdir,
        purelib=str(shared_scheme),
        platlib=str(shared_scheme),
    )
    wheel = make_wheel(
        tmp_path / "wheels",
        {
            f"{DATA_DIR}/purelib/same.py": b"a\n",
            f"{DATA_DIR}/platlib/same.py": b"b\n",
        },
    )

    with WheelFile.open(wheel) as source:
        with pytest.raises(InstallationConflictError) as exc_info:
            install(source, destination, {}, preflight=True)

    mapped = Path(destination.resolve_target("purelib", "same.py"))
    assert mapped.is_relative_to(destdir)
    assert str(mapped) in str(exc_info.value)
    assert str(destdir) in str(exc_info.value)
    assert not destdir.exists()


def test_file_beneath_another_file_is_conflict(tmp_path):
    root = tmp_path / "installation-root"
    destination = make_destination(root)
    wheel = make_wheel(
        tmp_path / "wheels",
        {
            "demo/leaf": b"I am a file\n",
            "demo/leaf/child.txt": b"I require leaf to be a directory\n",
        },
    )

    with WheelFile.open(wheel) as source:
        with pytest.raises(InstallationConflictError) as exc_info:
            install(source, destination, {}, preflight=True)

    conflict = exc_info.value.conflicts[0]
    assert conflict.relation == "file-parent"
    parent, child = conflict.targets
    assert parent.filename == Path(root / "purelib" / "demo" / "leaf").absolute()
    assert child.filename == Path(
        root / "purelib" / "demo" / "leaf" / "child.txt"
    ).absolute()

    message = str(exc_info.value)
    assert "demo/leaf" in message
    assert "demo/leaf/child.txt" in message
    assert str(parent.filename) in message
    assert str(child.filename) in message
    assert not root.exists()


def test_entry_script_and_data_script_conflict(tmp_path):
    root = tmp_path / "installation-root"
    destination = make_destination(root)
    wheel = make_wheel(
        tmp_path / "wheels",
        {
            f"{DATA_DIR}/scripts/demo-tool": b"#!/bin/sh\necho legacy\n",
        },
        entry_points=(
            "[console_scripts]\n"
            "demo-tool = demo:main\n"
        ),
    )

    with WheelFile.open(wheel) as source:
        with pytest.raises(InstallationConflictError) as exc_info:
            install(source, destination, {}, preflight=True)

    conflict = exc_info.value.conflicts[0]
    assert conflict.relation == "same-file"
    kinds = [target.source.kind for target in conflict.targets]
    assert set(kinds) == {"entry-script", "wheel-member"}

    message = str(exc_info.value)
    assert "entry point 'demo-tool'" in message
    assert f"{DATA_DIR}/scripts/demo-tool" in message
    expected = Path(root / "scripts" / "demo-tool").absolute()
    assert str(expected) in message
    assert not Path(destination.scheme_dict["scripts"]).exists()


def test_additional_metadata_and_wheel_metadata_conflict(tmp_path):
    root = tmp_path / "installation-root"
    destination = make_destination(root)
    wheel = make_wheel(
        tmp_path / "wheels",
        {f"{DIST_INFO}/extra.txt": b"from wheel\n"},
    )

    with WheelFile.open(wheel) as source:
        with pytest.raises(InstallationConflictError) as exc_info:
            install(
                source,
                destination,
                {"extra.txt": b"from caller\n"},
                preflight=True,
            )

    kinds = {target.source.kind for target in exc_info.value.conflicts[0].targets}
    assert kinds == {"wheel-member", "additional-metadata"}
    message = str(exc_info.value)
    assert "additional metadata 'extra.txt'" in message
    assert f"{DIST_INFO}/extra.txt" in message
    assert not root.exists()


def test_relocated_data_member_can_conflict_with_rewritten_record(tmp_path):
    root = tmp_path / "installation-root"
    destination = make_destination(root)
    record_lookalike = f"{DATA_DIR}/purelib/{DIST_INFO}/RECORD"
    wheel = make_wheel(
        tmp_path / "wheels",
        {record_lookalike: b"not the real final RECORD\n"},
    )

    with WheelFile.open(wheel) as source:
        with pytest.raises(InstallationConflictError) as exc_info:
            install(source, destination, {}, preflight=True)

    kinds = {target.source.kind for target in exc_info.value.conflicts[0].targets}
    assert kinds == {"wheel-member", "record"}
    assert "rewritten RECORD" in str(exc_info.value)
    assert not root.exists()


def test_cli_preflight_reports_conflict_and_creates_nothing(tmp_path, capsys):
    destdir = tmp_path / "destdir"
    wheel = make_wheel(
        tmp_path / "wheels",
        {
            f"{DATA_DIR}/purelib/demo/cli.py": b"a\n",
            f"{DATA_DIR}/platlib/demo/cli.py": b"b\n",
        },
    )

    # The in-process CLI helper leaves exception handling to the entrypoint.
    with pytest.raises(InstallationConflictError):
        main(
            [
                str(wheel),
                "--preflight",
                "--no-compile-bytecode",
                "--destdir",
                str(destdir),
            ],
            "python -m installer",
        )

    assert not destdir.exists()


def test_cli_preflight_checks_all_wheel_arguments_before_any_writes(tmp_path):
    destdir = tmp_path / "destdir"
    first = make_wheel(
        tmp_path / "wheels-a",
        {"shared/from-a.txt": b"a\n", "shared/same": b"a\n"},
        dist="alpha",
    )
    second = make_wheel(
        tmp_path / "wheels-b",
        {"shared/same": b"b\n"},
        dist="beta",
    )

    with pytest.raises(InstallationConflictError) as exc_info:
        main(
            [
                str(first),
                str(second),
                "--preflight",
                "--no-compile-bytecode",
                "--destdir",
                str(destdir),
            ],
            "python -m installer",
        )

    message = str(exc_info.value)
    assert "alpha-1.0.0-py3-none-any.whl" in message
    assert "beta-1.0.0-py3-none-any.whl" in message
    assert not destdir.exists()


def test_cli_subprocess_preflight_exits_cleanly_with_diagnostic(tmp_path):
    destdir = tmp_path / "destdir"
    wheel = make_wheel(
        tmp_path / "wheels",
        {
            "demo/a": b"file\n",
            "demo/a/b": b"child\n",
        },
    )

    src = Path(installer_file()).parents[1]
    code = f"""
import sys
sys.path.insert(0, {str(src)!r})
from installer.__main__ import _main
from installer.exceptions import InstallationConflictError

try:
    _main(sys.argv[1:])
except InstallationConflictError as exc:
    print(f"installer: error: {{exc}}", file=sys.stderr)
    raise SystemExit(1)
"""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            code,
            str(wheel),
            "--preflight",
            "--no-compile-bytecode",
            "--destdir",
            str(destdir),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "target conflict" in result.stderr
    assert "demo/a" in result.stderr
    assert str(destdir) in result.stderr
    assert not destdir.exists()


def installer_file() -> str:
    import installer

    return installer.__file__


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def mode_snapshot(root: Path) -> dict[str, int]:
    return {
        str(path.relative_to(root)): path.stat().st_mode & 0o7777
        for path in root.rglob("*")
        if path.is_file()
    }


def test_preflight_install_output_matches_normal_install(tmp_path):
    entry_points = (
        "[console_scripts]\n"
        "demo-cli = demo:main\n"
        "\n"
        "[gui_scripts]\n"
        "demo-gui = demo:main\n"
    )
    wheel = make_wheel(
        tmp_path / "wheels",
        {
            "demo/__init__.py": b"__version__ = '1.0'\n",
            "demo/main.py": b"def main():\n    return 0\n",
            f"{DATA_DIR}/data/demo/data.txt": b"payload\n",
            f"{DIST_INFO}/top_level.txt": b"demo\n",
            f"{DATA_DIR}/scripts/legacy-tool": (
                b"#!python\nprint('legacy shebang')\n"
            ),
        },
        entry_points=entry_points,
    )
    metadata = {"installer-preflight-test.txt": b"additional metadata\n"}

    normal_root = tmp_path / "normal"
    preflight_root = tmp_path / "preflight"
    normal_dest = make_destination(normal_root)
    preflight_dest = make_destination(preflight_root)

    with WheelFile.open(wheel) as source:
        install(source, normal_dest, metadata, preflight=False)
    with WheelFile.open(wheel) as source:
        install(source, preflight_dest, metadata, preflight=True)

    normal_files = snapshot(normal_root)
    preflight_files = snapshot(preflight_root)
    assert preflight_files.keys() == normal_files.keys()
    for name, contents in normal_files.items():
        assert preflight_files[name] == contents, name

    normal_modes = mode_snapshot(normal_root)
    preflight_modes = mode_snapshot(preflight_root)
    assert preflight_modes == normal_modes

    record_name = f"purelib/{DIST_INFO}/RECORD"
    assert normal_files[record_name] == preflight_files[record_name]

    legacy_name = "scripts/legacy-tool"
    assert normal_files[legacy_name].startswith(b"#!/test/bin/python\n")
    assert preflight_files[legacy_name] == normal_files[legacy_name]

    cli_name = "scripts/demo-cli"
    assert normal_files[cli_name].startswith(b"#!/test/bin/python\n")
    assert preflight_files[cli_name] == normal_files[cli_name]

    metadata_name = f"purelib/{DIST_INFO}/installer-preflight-test.txt"
    assert normal_files[metadata_name] == b"additional metadata\n"
    assert preflight_files[metadata_name] == normal_files[metadata_name]


def test_preflight_does_not_flag_distinct_targets(tmp_path):
    root = tmp_path / "root"
    destination = make_destination(root)
    wheel = make_wheel(
        tmp_path / "wheels",
        {"demo/__init__.py": b"", "demo/module.py": b"value = 1\n"},
    )

    with WheelFile.open(wheel) as source:
        install(source, destination, {}, preflight=True)

    assert (root / "purelib" / "demo" / "__init__.py").is_file()


def test_preflight_installation_standalone_returns_plan_without_writes(tmp_path):
    root = tmp_path / "root"
    destination = make_destination(root)
    wheel = make_wheel(
        tmp_path / "wheels",
        {"demo/__init__.py": b"", "demo/module.py": b"value = 1\n"},
    )

    with WheelFile.open(wheel) as source:
        plan = preflight_installation(source, destination, {"x.txt": b"x"})

    kinds = [target.source.kind for target in plan.targets]
    assert "wheel-member" in kinds
    assert "additional-metadata" in kinds
    assert kinds[-1] == "record"
    assert not root.exists()


class LegacyDestinationWithoutTargetMapping:
    """Duck-typed destination implementing the historical write interface."""

    def write_file(self, scheme, path, stream, is_executable):  # pragma: no cover
        raise AssertionError("write_file should not be called during preflight")

    def write_script(self, name, module, attr, section):  # pragma: no cover
        raise AssertionError("write_script should not be called during preflight")

    def finalize_installation(  # pragma: no cover
        self, scheme, record_file_path, records
    ):
        raise AssertionError("finalize should not be called during preflight")


def test_preflight_reports_unsupported_legacy_destination(tmp_path):
    wheel = make_wheel(tmp_path / "wheels", {"demo/__init__.py": b""})
    destination = LegacyDestinationWithoutTargetMapping()

    with WheelFile.open(wheel) as source:
        with pytest.raises(PreflightNotSupportedError):
            install(source, destination, {}, preflight=True)  # type: ignore[arg-type]


def make_large_payload_wheel(path: Path, payload_size: int) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    dist_info = f"mem-{VERSION}.dist-info"
    wheel_path = path / f"mem-{VERSION}-py3-none-any.whl"
    chunk = b"0" * (1024 * 1024)

    with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(
            f"{dist_info}/WHEEL",
            b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/METADATA",
            b"Metadata-Version: 2.1\nName: mem\nVersion: 1.0.0\n",
        )

        info = zipfile.ZipInfo("payload.bin")
        info.compress_type = zipfile.ZIP_STORED
        with archive.open(info, "w") as output:
            remaining = payload_size
            while remaining:
                size = min(remaining, len(chunk))
                output.write(chunk[:size])
                remaining -= size

        # Empty hash/size fields are valid enough for path-only planning.
        archive.writestr(f"{dist_info}/RECORD", "payload.bin,,\n" + f"{dist_info}/RECORD,,\n")

    return wheel_path


def measure_preflight_peak(wheel: Path, root: Path, src: Path) -> int:
    code = """
import os, sys, tracemalloc
sys.path.insert(0, os.environ["INSTALLER_SRC"])
from installer._core import preflight_installation
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from installer.utils import SCHEME_NAMES

wheel, root = sys.argv[1], sys.argv[2]
schemes = {name: os.path.join(root, name) for name in SCHEME_NAMES}
destination = SchemeDictionaryDestination(
    schemes, "/test/python", "posix", bytecode_optimization_levels=()
)
tracemalloc.start(25)
with WheelFile.open(wheel) as source:
    preflight_installation(source, destination, {})
_, peak = tracemalloc.get_traced_memory()
print(peak)
"""
    env = {**os.environ, "INSTALLER_SRC": str(src)}
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, str(wheel), str(root)],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return int(result.stdout.strip().splitlines()[-1])


def test_preflight_memory_growth_does_not_follow_payload_size(tmp_path):
    src = Path(installer_file()).parents[1]
    small_wheel = make_large_payload_wheel(tmp_path / "small.whl.d", 1024 * 1024)
    large_wheel = make_large_payload_wheel(
        tmp_path / "large.whl.d", 128 * 1024 * 1024
    )

    peak_small = measure_preflight_peak(small_wheel, tmp_path / "root-small", src)
    peak_large = measure_preflight_peak(large_wheel, tmp_path / "root-large", src)

    assert max(0, peak_large - peak_small) <= 8 * 1024 * 1024


def test_check_targets_detects_all_relations_in_one_call():
    # Direct unit check keeps the tree-prefix algorithm honest independently
    # of wheel construction.
    from installer.preflight import InstallSource, PlannedTarget

    def target(identity: str) -> PlannedTarget:
        path = Path(identity)
        return PlannedTarget.create(
            source=InstallSource(kind="test", description=identity),
            scheme="purelib",
            logical_path=identity,
            filename=path,
        )

    same_a = target("/tmp/example/a")
    same_b = target("/tmp/example/a")
    parent = target("/tmp/example/file")
    child = target("/tmp/example/file/child")

    with pytest.raises(InstallationConflictError) as exc_info:
        check_targets([same_a, same_b, parent, child])

    relations = {conflict.relation for conflict in exc_info.value.conflicts}
    assert relations == {"same-file", "file-parent"}
