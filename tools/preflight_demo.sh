#!/usr/bin/env bash
# Reproducible, offline terminal demonstration for install preflight checks.
# All wheels and destination trees are created under a fresh mktemp directory.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d /tmp/installer-preflight-demo.XXXXXX)"
echo "== Temporary workspace: $WORK"

# Build wheels using only the standard library.  The builder runs with -I and
# receives the workspace as an absolute argument.
python3 -I -B - "$WORK" <<'PY'
import base64
import hashlib
import pathlib
import sys
import zipfile

work = pathlib.Path(sys.argv[1])
dist = "demo"
version = "1.0.0"
di = f"{dist}-{version}.dist-info"
data = f"{dist}-{version}.data"


def digest(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")


def make_wheel(filename: str, files: dict[str, bytes], entry_points: bytes | None = None) -> None:
    output = work / filename
    output.parent.mkdir(parents=True, exist_ok=True)
    members: dict[str, bytes] = {
        f"{di}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{di}/METADATA": b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n",
    }
    if entry_points is not None:
        members[f"{di}/entry_points.txt"] = entry_points
    members.update(files)

    rows = [f"{name},sha256={digest(blob)},{len(blob)}" for name, blob in members.items()]
    rows.append(f"{di}/RECORD,,")
    members[f"{di}/RECORD"] = ("\n".join(rows) + "\n").encode()

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as wheel:
        for name, blob in members.items():
            wheel.writestr(name, blob)


make_wheel(
    "same-scheme/demo-1.0.0-py3-none-any.whl",
    {
        f"{data}/purelib/demo/duplicate.py": b"# from purelib\n",
        f"{data}/platlib/demo/duplicate.py": b"# from platlib; purelib==platlib\n",
    },
)

make_wheel(
    "nested/demo-1.0.0-py3-none-any.whl",
    {
        "demo/leaf": b"this path is a file\n",
        "demo/leaf/child.txt": b"this path needs leaf to be a directory\n",
    },
)

make_wheel(
    "entry-script/demo-1.0.0-py3-none-any.whl",
    {f"{data}/scripts/demo-tool": b"#!/bin/sh\necho legacy\n"},
    b"[console_scripts]\ndemo-tool = demo:main\n",
)

make_wheel(
    "ok/demo-1.0.0-py3-none-any.whl",
    {
        "demo/__init__.py": b"__version__ = '1.0.0'\n",
        "demo/main.py": b"def main():\n    print('ok')\n",
        f"{data}/data/demo/message.txt": b"hello from .data/data\n",
        f"{data}/scripts/demo-legacy": b"#!python\nprint('shebang rewritten')\n",
        f"{di}/top_level.txt": b"demo\n",
    },
    b"[console_scripts]\ndemo-cli = demo.main:main\n",
)
PY

# Run `python -m installer`-equivalent _main() under isolated Python while
# adding this checkout's src/ explicitly to sys.path.
RUNNER='
import sys
sys.path.insert(0, sys.argv[1])
from installer.__main__ import _main
from installer.exceptions import InstallationConflictError
try:
    _main(sys.argv[2:], "python -m installer")
except InstallationConflictError as exc:
    print(f"installer: error: {exc}", file=sys.stderr)
    raise SystemExit(1)
'

run_installer() {
  python3 -I -B -c "$RUNNER" "$REPO_ROOT/src" "$@"
}

echo
echo "== 1. CLI --preflight rejects a file/file-parent conflict"
NESTED_DEST="$WORK/dest-nested"
if run_installer "$WORK/nested/demo-1.0.0-py3-none-any.whl" \
    --preflight --no-compile-bytecode --destdir "$NESTED_DEST" \
    >"$WORK/nested.stdout" 2>"$WORK/nested.stderr"; then
  echo "ERROR: nested conflict was not rejected" >&2
  exit 1
fi
cat "$WORK/nested.stderr"
grep -q "target conflict" "$WORK/nested.stderr"
grep -q "demo/leaf" "$WORK/nested.stderr"
grep -q "demo/leaf/child.txt" "$WORK/nested.stderr"
if [[ -e "$NESTED_DEST" ]]; then
  echo "ERROR: destination was created despite preflight conflict" >&2
  find "$NESTED_DEST" -print >&2
  exit 1
fi
echo "OK: no destination tree was created"

echo
echo "== 2. CLI --preflight rejects an entry-point/.data-script collision"
SCRIPT_DEST="$WORK/dest-scripts"
if run_installer "$WORK/entry-script/demo-1.0.0-py3-none-any.whl" \
    --preflight --no-compile-bytecode --destdir "$SCRIPT_DEST" \
    >"$WORK/script.stdout" 2>"$WORK/script.stderr"; then
  echo "ERROR: script conflict was not rejected" >&2
  exit 1
fi
cat "$WORK/script.stderr"
grep -q "entry point 'demo-tool'" "$WORK/script.stderr"
grep -q "${DATA_DIR:-demo-1.0.0.data}/scripts/demo-tool" "$WORK/script.stderr"
if [[ -e "$SCRIPT_DEST" ]]; then
  echo "ERROR: script destination was created despite conflict" >&2
  exit 1
fi
echo "OK: no script destination tree was created"

echo
echo "== 3. Library preflight catches different scheme names mapped to one directory"
python3 -I -B - "$REPO_ROOT/src" "$WORK" <<'PY'
import pathlib
import sys

sys.path.insert(0, sys.argv[1])

from installer import InstallationConflictError, install
from installer.destinations import SchemeDictionaryDestination
from installer.sources import WheelFile
from installer.utils import SCHEME_NAMES

work = pathlib.Path(sys.argv[2])
scheme_root = work / "library-schemes"
shared = scheme_root / "same-lib-directory"
schemes = {
    name: str(shared if name in {"purelib", "platlib"} else scheme_root / name)
    for name in SCHEME_NAMES
}
destination = SchemeDictionaryDestination(
    schemes, "/demo/interpreter", "posix", bytecode_optimization_levels=()
)

try:
    with WheelFile.open(work / "same-scheme" / "demo-1.0.0-py3-none-any.whl") as source:
        install(source, destination, {}, preflight=True)
except InstallationConflictError as exc:
    print(exc)
    text = str(exc)
    assert "purelib/demo/duplicate.py" in text
    assert "platlib/demo/duplicate.py" in text
    assert str(shared / "demo" / "duplicate.py") in text
else:
    raise SystemExit("ERROR: purelib/platlib conflict was not rejected")

assert not shared.exists(), "preflight created the shared scheme directory"
print("OK: conflict reported with both wheel members and actual target; no directories created")
PY

echo
echo "== 4. Valid wheel: normal install and --preflight install trees are identical"
NORMAL_DEST="$WORK/normal"
PREFLIGHT_DEST="$WORK/preflight"
run_installer "$WORK/ok/demo-1.0.0-py3-none-any.whl" \
  --no-compile-bytecode --destdir "$NORMAL_DEST"
run_installer "$WORK/ok/demo-1.0.0-py3-none-any.whl" \
  --preflight --no-compile-bytecode --destdir "$PREFLIGHT_DEST"

diff -r "$NORMAL_DEST" "$PREFLIGHT_DEST"
echo "OK: diff -r reports identical installed trees"

RECORD="$(find "$PREFLIGHT_DEST" -path '*/demo-1.0.0.dist-info/RECORD' -print -quit)"
CLI_SCRIPT="$(find "$PREFLIGHT_DEST" -path '*/bin/demo-cli' -type f -print -quit)"
LEGACY_SCRIPT="$(find "$PREFLIGHT_DEST" -path '*/bin/demo-legacy' -type f -print -quit)"
test -n "$RECORD"
test -n "$CLI_SCRIPT"
test -n "$LEGACY_SCRIPT"
grep -q '^demo/__init__.py,' "$RECORD"
head -1 "$CLI_SCRIPT" | grep -q '^#!'
head -1 "$LEGACY_SCRIPT" | grep -q '^#!/usr/bin/python'
echo "OK: RECORD and rewritten shebangs are present"

echo
echo "Demo completed successfully. Workspace retained at: $WORK"
