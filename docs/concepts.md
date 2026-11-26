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
