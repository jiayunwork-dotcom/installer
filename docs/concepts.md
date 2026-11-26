# Concepts

This library has two main abstractions:

- {any}`WheelSource`: Serves as source of information about a wheel file.
- {any}`WheelDestination`: Handles all file writing and post-installation
  processing.

## WheelSource

These objects represent a wheel file, abstracting away how the actual file is
stored or accessed.

This allows the core install logic to be used with in-memory wheel files, or
unzipped-on-disk wheel, or with {any}`zipfile.ZipFile` objects from an on-disk
wheel, or something else entirely.

This protocol/abstraction is designed to be implementable without a direct
dependency on this library. This allows for other libraries in the Python
packaging ecosystem to provide implementations of the protocol, allowing for
more code reuse opportunities.

One of the benefits of this fully described interface is the possibility to
decouple the implementation of additional validation on wheels (such as
validating the RECORD entries in a wheel match the actual contents of the wheel,
or enforcing signing requirements) based on what the specific use case demands.

## WheelDestination

These objects are responsible for handling the writing-to-filesystem
interactions, determining RECORD file entries and post-install actions (like
generating .pyc files). While this is a lot of responsibility, this was
explicitly provided to make it possible for custom `WheelDestination`
implementations to be more powerful and flexible.

Most of these tasks can either be delegated to utilities provided in this
library (eg: script generation), or to the Python standard library (eg:
generating `.pyc` files).

## Preflight conflict checks

`install(..., preflight=True)` first resolves every location that the
installation will claim: wheel members after `.data` relocation, generated
entry-point scripts, additional metadata, and the rewritten `RECORD`. It
compares the destination-specific absolute paths, so different scheme names
that map to the same directory are still detected. It also rejects a file
target that would have to be a parent directory of another target.

When a conflict is found, `installer.exceptions.InstallationConflictError`
is raised before the destination creates any directory or file. The exception
contains structured `conflicts`, and its message lists each source and
resolved target.

Path mapping belongs to the destination. `SchemeDictionaryDestination`
implements the optional `resolve_target`, `resolve_script_target`, and
`resolve_record_target` hooks using the same helpers as its write path.
Third-party destinations with custom routing or script naming should implement
those hooks to support preflight; otherwise preflight raises
`PreflightNotSupportedError` rather than guessing paths hidden inside their
write methods.

Preflight performs an additional metadata-only pass over
`WheelSource.get_contents()` and closes each stream without reading payload
bytes. This preserves streaming memory use, at the cost of iterating the
source twice when installation proceeds. `WheelSource.get_contents()` already
documents that multiple iterations are supported and yield equivalent
contents.

## Failure rollback

Preflight rejects conflicts that are visible from the target list alone. It
cannot prevent a wheel member from failing mid-stream (for example a corrupted
compressed member) or a destination write from raising after earlier files were
already written. `install(..., rollback=True)` adds a failure-atomic option for
those in-process failures: if the installation raises an ordinary exception,
`install()` returns only after the destination's files, scripts, `RECORD`, byte
contents and executable bits match their state before the call.

### What is journaled, and when

The rollback mode writes payloads directly to the destination (exactly like the
normal flow) and journals only what undo needs, resolved *before* the first
write:

- for a target that already exists as a regular file: a fixed-buffer copy of its
  bytes plus its permission bits;
- for an existing symlink: the link target, plus the bytes/mode of the file it
  resolves to;
- for a target the install creates: a marker that it must be removed;
- for every ancestor directory the install creates: a marker for removal, but
  only while empty.

This trades *temporary disk space* for a short *half-written window*: the
journal holds a copy of only the pre-existing files this install overwrites
(usually nothing on a fresh install), not a second copy of the new wheel
payload. The destination is half-installed only during the `install()` call;
on success the journal is deleted, on failure it is played back before
`install()` returns. The guarantee covers ordinary in-process exceptions; it
does **not** cover power loss, `SIGKILL`, or another process changing the
destination concurrently.

Undo removes only files this call created and only directories this call
created that are still empty. Pre-existing unrelated files and directories are
left untouched. With the CLI, each wheel argument is its own installation, so
`--rollback` undoes the failing wheel's writes; earlier wheels that installed
successfully are kept.

### Signal on failure

A failed undo does not claim success. The installer raises
`InstallationRollbackError`, whose `__cause__` is the original installation
error, whose `failures` list each target that could not be restored, and whose
`journal_root` points at the kept journal. A `rollback-manifest.txt` in that
directory records each saved backup's path, so recovery can be completed by
hand. The saved bytes are never deleted when undo fails.

### Destination support

Only destinations that can guarantee undo opt in: set `rollback_supported =
True` and route every write through `resolve_target` /
`resolve_script_target` / `resolve_record_target`. A destination that does not
opt in is rejected with `RollbackNotSupportedError` *before* any write; the
installer never silently falls back to a non-atomic install.
`SchemeDictionaryDestination` supports rollback when bytecode compilation is
disabled (`bytecode_optimization_levels=()`), because `.pyc` files are written
outside the journaled write set; requesting both raises before writing.

