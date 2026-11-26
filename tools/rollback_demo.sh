#!/usr/bin/env bash
# Reproducible, offline terminal demonstration for opt-in rollback installs.
# All wheels and destination trees are created under a fresh mktemp directory.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d /tmp/installer-rollback-demo.XXXXXX)"
echo "== Temporary workspace: $WORK"

python3 -I -B - "$REPO_ROOT/src" "$WORK" <<'PY'
"""Build demo wheels using only the standard library."""
import base64
import hashlib
import pathlib
import sys
import zipfile

src, work = sys.argv[1], pathlib.Path(sys.argv[2])
dist, version = "demo", "1.0.0"
di = f"{dist}-{version}.dist-info"
data = f"{dist}-{version}.data"


def digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def make_wheel(path: pathlib.Path, files: dict[str, bytes], *, stored: bool = False) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    members: dict[str, bytes] = {
        f"{di}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{di}/METADATA": b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n",
        f"{di}/entry_points.txt": b"[console_scripts]\ndemo-cli = demo:main\n",
    }
    members.update(files)
    rows = [f"{name},sha256={digest(blob)},{len(blob)}" for name, blob in members.items()]
    rows.append(f"{di}/RECORD,,")
    members[f"{di}/RECORD"] = ("\n".join(rows) + "\n").encode()

    compression = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    with zipfile.ZipFile(path, "w", compression=compression) as wheel:
        for name, blob in members.items():
            wheel.writestr(name, blob)
    return path


def payload(size: int) -> bytes:
    chunk = b"".join(f"{i:08d}".encode() for i in range(1024))
    return (chunk * (size // len(chunk) + 1))[:size]


# 1. A normal, valid wheel.
make_wheel(
    work / "good" / "demo-1.0.0-py3-none-any.whl",
    {
        "demo/__init__.py": b"def main():\n    print('hello')\n",
        f"{data}/data/demo/message.txt": b"from .data/data\n",
        f"{data}/scripts/demo-legacy": b"#!python\nprint('legacy')\n",
    },
)

# 2. A wheel whose stored member bytes are corrupted: the installer writes the
#    earlier members first and then fails while reading the corrupted one.
corrupt = make_wheel(
    work / "corrupt" / "demo-1.0.0-py3-none-any.whl",
    {
        "demo/aaa.py": b"a = 1\n",
        "demo/zzz_big.bin": b"\x00" * 4096,
        "demo/other.py": b"o = 1\n",
    },
    stored=True,
)
raw = bytearray(corrupt.read_bytes())
marker = b"\x00" * 64
index = raw.find(b"demo/zzz_big.bin")
# Flip bytes inside the stored payload that follows the member header.
pos = raw.find(marker, index)
raw[pos : pos + 64] = b"\xff" * 64
corrupt.write_bytes(bytes(raw))

# 3. Small and large payload wheels for the tracemalloc comparison.
make_wheel(work / "small" / "demo-1.0.0-py3-none-any.whl",
           {"demo/blob.bin": payload(1 * 1024 * 1024), "demo/__init__.py": b"x = 1\n"},
           stored=True)
make_wheel(work / "large" / "demo-1.0.0-py3-none-any.whl",
           {"demo/blob.bin": payload(128 * 1024 * 1024), "demo/__init__.py": b"x = 1\n"},
           stored=True)

print("built demo wheels under", work)
PY

run_installer() {
  python3 -I -B - "$REPO_ROOT/src" "$@" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from installer.__main__ import _main
try:
    _main(sys.argv[2:], "python -m installer")
except Exception as exc:
    print(f"installer: error: {type(exc).__name__}: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

echo
echo "== 1. CLI --rollback success is identical to a normal install"
run_installer "$WORK/good/demo-1.0.0-py3-none-any.whl" --no-compile-bytecode -d "$WORK/normal"
run_installer "$WORK/good/demo-1.0.0-py3-none-any.whl" --no-compile-bytecode --rollback -d "$WORK/rolled"
diff -r "$WORK/normal" "$WORK/rolled"
echo "OK: diff -r reports identical trees (files, scripts, RECORD)"

echo
echo "== 2. A corrupted member aborts, and --rollback restores an untouched tree"
UNTOUCHED="$WORK/untouched"
if run_installer "$WORK/corrupt/demo-1.0.0-py3-none-any.whl" --no-compile-bytecode --rollback -d "$UNTOUCHED" 2>"$WORK/corrupt.stderr"; then
  echo "ERROR: corrupted wheel unexpectedly installed" >&2
  exit 1
fi
cat "$WORK/corrupt.stderr"
if [[ -e "$UNTOUCHED" ]]; then
  echo "ERROR: destination was left behind despite rollback" >&2
  find "$UNTOUCHED" -print >&2
  exit 1
fi
echo "OK: failure left no destination tree"

echo
echo "== 3. --overwrite-existing plus a RECORD failure restores the old bytes/mode"
OVER="$WORK/overwrite"
mkdir -p "$OVER"
python3 -I -B - "$REPO_ROOT/src" "$WORK" "$OVER" <<'PY'
import io
import os
import pathlib
import stat
import sys

src, work, over = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
sys.path.insert(0, src)

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from installer.utils import SCHEME_NAMES

# Seed an old file the install will overwrite.
old = over / "purelib" / "demo" / "aaa.py"
old.parent.mkdir(parents=True)
old.write_bytes(b"OLD BYTES")
os.chmod(old, 0o640)

scheme = {s: str(over / s) for s in SCHEME_NAMES}


class RecordFails(SchemeDictionaryDestination):
    def finalize_installation(self, *args, **kwargs):
        raise OSError("simulated RECORD write failure")


dest = RecordFails(scheme, "/usr/bin/python3", "posix",
                   bytecode_optimization_levels=(), overwrite_existing=True)
with WheelFile.open(work / "good" / "demo-1.0.0-py3-none-any.whl") as source:
    try:
        install(source, dest, {}, rollback=True)
    except OSError as exc:
        print(f"caught {type(exc).__name__}: {exc}")
    else:
        raise SystemExit("ERROR: install unexpectedly succeeded")

assert old.read_bytes() == b"OLD BYTES", old.read_bytes()
assert stat.S_IMODE(old.stat().st_mode) == 0o640
assert not (over / "purelib" / "demo" / "__init__.py").exists()
assert not (over / "scripts").exists()
print("OK: old file bytes and mode restored; new files removed")
PY

echo
echo "== 4. A failed undo keeps both errors and the recovery material"
python3 -I -B - "$REPO_ROOT/src" "$WORK" <<'PY'
import io
import pathlib
import sys

src, work = sys.argv[1], pathlib.Path(sys.argv[2])
sys.path.insert(0, src)

import installer.rollback as rollback_module
from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import InstallationRollbackError
from installer.sources import WheelFile, WheelSource
from installer.utils import SCHEME_NAMES

root = work / "failed-undo"
old = root / "purelib" / "demo" / "aaa.py"
old.parent.mkdir(parents=True)
old.write_bytes(b"ORIGINAL")


class Src(WheelSource):
    def __init__(self):
        super().__init__("demo", "1.0.0")

    @property
    def dist_info_filenames(self):
        return ["WHEEL", "METADATA"]

    def read_dist_info(self, name):
        return {
            "WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            "METADATA": "Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n",
        }[name]

    def get_contents(self):
        yield ("demo/aaa.py", "", "6"), io.BytesIO(b"NEW!!!"), False
        # The write pass fails while reading this member's payload.
        blob = io.BytesIO(b"unused")

        def explode(size=-1):
            raise OSError("installation failed mid-stream")

        blob.read = explode
        yield ("demo/zzz.py", "", "6"), blob, False


class Dest(SchemeDictionaryDestination):
    rollback_supported = True


dest = Dest({s: str(root / s) for s in SCHEME_NAMES}, "/usr/bin/python3", "posix",
            bytecode_optimization_levels=(), overwrite_existing=True)

real_restore = rollback_module._restore_file
rollback_module._restore_file = lambda *a, **k: (_ for _ in ()).throw(
    OSError("undo storage unavailable"))
try:
    try:
        install(Src(), dest, {}, rollback=True)
    except InstallationRollbackError as exc:
        print(f"caught {type(exc).__name__}")
        print(f"  original error (cause): {type(exc.__cause__).__name__}: {exc.__cause__}")
        for failure in exc.failures:
            print(f"  could not restore: {failure.target} ({failure.error})")
            print(f"  saved bytes: {failure.backup_path} -> {failure.backup_path.read_bytes()!r}")
        print(f"  journal kept at: {exc.journal_root}")
        assert exc.journal_root.is_dir()
        assert (exc.journal_root / "rollback-manifest.txt").is_file()
        kept = exc.journal_root
    else:
        raise SystemExit("ERROR: undo failure was not reported")
finally:
    rollback_module._restore_file = real_restore

# The journal is left for manual recovery in real use; the demo is done with it.
import shutil

shutil.rmtree(kept, ignore_errors=True)
print("  (demo removed the retained journal)")
PY

echo
echo "== 5. Unsupported destination is refused before any write"
python3 -I -B - "$REPO_ROOT/src" "$WORK" <<'PY'
import pathlib
import sys

src, work = sys.argv[1], pathlib.Path(sys.argv[2])
sys.path.insert(0, src)

from installer import install
from installer.destinations import WheelDestination
from installer.exceptions import RollbackNotSupportedError
from installer.sources import WheelFile


class Custom(WheelDestination):
    pass


root = work / "unsupported"
with WheelFile.open(work / "good" / "demo-1.0.0-py3-none-any.whl") as source:
    try:
        install(source, Custom(), {}, rollback=True)
    except RollbackNotSupportedError as exc:
        print(f"caught {type(exc).__name__}: {exc}")
    else:
        raise SystemExit("ERROR: unsupported destination was accepted")
assert not root.exists()
print("OK: refused without falling back to a normal install")
PY

echo
echo "== 6. Rollback memory peak is flat across 1 MiB and 128 MiB payloads"
python3 -I -B - "$REPO_ROOT/src" "$WORK" <<'PY'
import pathlib
import sys
import tracemalloc

src, work = sys.argv[1], pathlib.Path(sys.argv[2])
sys.path.insert(0, src)

from installer import install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from installer.utils import SCHEME_NAMES


def peak(wheel: pathlib.Path, root: pathlib.Path) -> int:
    scheme = {s: str(root / s) for s in SCHEME_NAMES}
    dest = SchemeDictionaryDestination(scheme, "/usr/bin/python3", "posix",
                                       bytecode_optimization_levels=())
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        with WheelFile.open(wheel) as source:
            install(source, dest, {}, rollback=True)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


small = peak(work / "small" / "demo-1.0.0-py3-none-any.whl", work / "mem-small")
large = peak(work / "large" / "demo-1.0.0-py3-none-any.whl", work / "mem-large")
print(f"1 MiB payload  peak: {small / 1024 / 1024:.2f} MiB")
print(f"128 MiB payload peak: {large / 1024 / 1024:.2f} MiB")
print(f"difference: {abs(large - small) / 1024 / 1024:.2f} MiB (limit 8 MiB)")
assert abs(large - small) <= 8 * 1024 * 1024
print("OK: memory peak is bounded by the streaming buffer, not payload size")
PY

echo
echo "Demo completed successfully. Workspace retained at: $WORK"
