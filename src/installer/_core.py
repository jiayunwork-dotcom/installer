"""Core wheel installation logic."""

import posixpath
import warnings
from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
from os import PathLike
from typing import TYPE_CHECKING, BinaryIO, cast

from installer.destinations import WheelDestination
from installer.exceptions import InvalidWheelSource, PreflightNotSupportedError
from installer.preflight import (
    InstallSource,
    InstallationPlan,
    PlannedTarget,
    check_targets,
)
from installer.records import RecordEntry
from installer.sources import WheelSource
from installer.utils import SCHEME_NAMES, Scheme, parse_entrypoints, parse_metadata_file

if TYPE_CHECKING:
    from installer.scripts import ScriptSection

__all__ = ["build_install_plan", "install", "preflight_installation"]


def _process_WHEEL_file(source: WheelSource) -> Scheme:  # noqa: N802
    """Process the WHEEL file, from ``source``.

    Returns the scheme that the archive root should go in.
    """
    stream = source.read_dist_info("WHEEL")
    metadata = parse_metadata_file(stream)

    # Ensure compatibility with this wheel version.
    if not (metadata["Wheel-Version"] and metadata["Wheel-Version"].startswith("1.")):
        message = "Incompatible Wheel-Version {}, only support version 1.x wheels."
        raise InvalidWheelSource(source, message.format(metadata["Wheel-Version"]))

    # Determine where archive root should go.
    if metadata["Root-Is-Purelib"] == "true":
        return cast("Scheme", "purelib")
    else:
        return cast("Scheme", "platlib")


def _determine_scheme(
    path: str, source: WheelSource, root_scheme: Scheme
) -> tuple[Scheme, str]:
    """Determine which scheme to place given path in, from source."""
    data_dir = source.data_dir

    # If it's in not `{distribution}-{version}.data`, then it's in root_scheme.
    if posixpath.commonpath([data_dir, path]) != data_dir:
        return root_scheme, path

    # Figure out which scheme this goes to.
    parts = []
    scheme_name = None
    left = path
    while True:
        left, right = posixpath.split(left)
        parts.append(right)
        if left == source.data_dir:
            scheme_name = right
            break

    if scheme_name not in SCHEME_NAMES:
        msg_fmt = "{path} is not contained in a valid .data subdirectory."
        raise InvalidWheelSource(source, msg_fmt.format(path=path))

    return cast("Scheme", scheme_name), posixpath.join(*reversed(parts[:-1]))


def _is_skipped_pycache_path(path: str) -> bool:
    """Whether installation intentionally omits a wheel's ``__pycache__`` member."""
    return "__pycache__" in path.split("/")[:-1]


def _source_location(source: WheelSource) -> str:
    """Return the best available diagnostic locator for a source."""
    location = getattr(source, "location", None)
    if location:
        return cast(str, location)
    return f"{source.distribution}-{source.version}"


@dataclass
class _ScriptOperation:
    name: str
    module: str
    attr: str
    section: "ScriptSection"


@dataclass
class _FileOperation:
    wheel_path: str
    scheme: Scheme
    destination_path: str
    stream: BinaryIO
    is_executable: bool


@dataclass
class _MetadataOperation:
    filename: str
    scheme: Scheme
    path: str
    contents: bytes


@dataclass
class _PycacheWarning:
    path: str
    distribution: str


@dataclass
class _RecordOperation:
    scheme: Scheme
    path: str


_InstallOperation = (
    _ScriptOperation
    | _FileOperation
    | _MetadataOperation
    | _PycacheWarning
    | _RecordOperation
)


def _iter_install_operations(
    source: WheelSource,
    additional_metadata: dict[str, bytes],
) -> Iterator[_InstallOperation]:
    """Yield every action in an install, with path/scheme decisions made once.

    Both preflight planning and real installation consume this iterator.  This
    keeps source filtering, ``.data`` relocation, operation order, and RECORD
    placement identical between the two phases.
    """
    root_scheme = _process_WHEEL_file(source)

    # RECORD handling
    record_file_path = posixpath.join(source.dist_info_dir, "RECORD")

    # Write the entry_points based scripts.
    if "entry_points.txt" in source.dist_info_filenames:
        entrypoints_text = source.read_dist_info("entry_points.txt")
        for name, module, attr, section in parse_entrypoints(entrypoints_text):
            yield _ScriptOperation(
                name=name,
                module=module,
                attr=attr,
                section=section,
            )

    # Write all the files from the wheel.
    for record_elements, stream, is_executable in source.get_contents():
        source_record = RecordEntry.from_elements(*record_elements)
        path = source_record.path
        # Skip the RECORD, which is written at the end, based on this info.
        if path == record_file_path:
            continue

        if _is_skipped_pycache_path(path):
            yield _PycacheWarning(path=path, distribution=source.distribution)
            continue

        # Figure out where to write this file.
        scheme, destination_path = _determine_scheme(
            path=path,
            source=source,
            root_scheme=root_scheme,
        )
        yield _FileOperation(
            wheel_path=path,
            scheme=scheme,
            destination_path=destination_path,
            stream=stream,
            is_executable=is_executable,
        )

    # Write all the installation-specific metadata
    for filename, contents in additional_metadata.items():
        path = posixpath.join(source.dist_info_dir, filename)
        yield _MetadataOperation(
            filename=filename,
            scheme=root_scheme,
            path=path,
            contents=contents,
        )

    yield _RecordOperation(scheme=root_scheme, path=record_file_path)


def _preflight_resolve_target(
    destination: WheelDestination,
    scheme: Scheme,
    path: str,
) -> "str | PathLike[str]":
    resolver = getattr(destination, "resolve_target", None)
    if not callable(resolver):
        raise PreflightNotSupportedError(
            "preflight=True requires the destination to implement "
            "resolve_target(scheme, path); "
            f"{type(destination).__name__} does not expose target mappings"
        )

    try:
        mapped = resolver(scheme, path)
    except NotImplementedError as exc:
        raise PreflightNotSupportedError(
            "preflight=True requires a destination with a concrete target "
            "mapping; "
            f"{type(destination).__name__}.resolve_target is not implemented"
        ) from exc

    if not isinstance(mapped, (str, PathLike)):
        raise PreflightNotSupportedError(
            f"{type(destination).__name__}.resolve_target returned "
            f"{type(mapped).__name__!r}, expected a path"
        )

    return cast("str | PathLike[str]", mapped)


def _preflight_resolve_script_target(
    destination: WheelDestination,
    operation: _ScriptOperation,
) -> tuple[Scheme, str, "str | PathLike[str]"]:
    resolver = getattr(destination, "resolve_script_target", None)
    if not callable(resolver):
        return (
            Scheme("scripts"),
            operation.name,
            _preflight_resolve_target(destination, Scheme("scripts"), operation.name),
        )

    try:
        result = resolver(
            name=operation.name,
            module=operation.module,
            attr=operation.attr,
            section=operation.section,
        )
    except NotImplementedError:
        return (
            Scheme("scripts"),
            operation.name,
            _preflight_resolve_target(destination, Scheme("scripts"), operation.name),
        )

    scheme, logical_path, target = result
    return cast(Scheme, scheme), str(logical_path), target


def _preflight_resolve_record_target(
    destination: WheelDestination,
    operation: _RecordOperation,
) -> "str | PathLike[str]":
    resolver = getattr(destination, "resolve_record_target", None)
    if callable(resolver):
        try:
            mapped = resolver(operation.scheme, operation.path)
            return cast("str | PathLike[str]", mapped)
        except NotImplementedError:
            pass
    return _preflight_resolve_target(destination, operation.scheme, operation.path)


def build_install_plan(
    source: WheelSource,
    destination: WheelDestination,
    additional_metadata: dict[str, bytes],
) -> InstallationPlan:
    """Resolve every target in an installation without writing payload contents.

    The plan follows the same operation iterator used by :func:`install`.  It
    opens wheel streams only to advance :meth:`WheelSource.get_contents` and
    closes them without reading payload bytes.
    """
    source_location = _source_location(source)
    targets: list[PlannedTarget] = []
    record_operation: _RecordOperation | None = None

    for operation in _iter_install_operations(source, additional_metadata):
        if isinstance(operation, _PycacheWarning):
            # Planning uses the same skip decision but does not duplicate the
            # user-visible warning emitted by the real installation pass.
            continue

        if isinstance(operation, _ScriptOperation):
            script_scheme, script_path, mapped = _preflight_resolve_script_target(
                destination, operation
            )
            origin = InstallSource(
                kind="entry-script",
                name=operation.name,
                description=(
                    f"{operation.section}_scripts entry point {operation.name!r} "
                    f"({operation.module}:{operation.attr})"
                ),
                scheme=script_scheme,
                logical_path=script_path,
                location=source_location,
            )
            targets.append(
                PlannedTarget.create(
                    source=origin,
                    scheme=script_scheme,
                    logical_path=script_path,
                    filename=mapped,
                )
            )
            continue

        if isinstance(operation, _FileOperation):
            try:
                mapped = _preflight_resolve_target(
                    destination, operation.scheme, operation.destination_path
                )
                origin = InstallSource(
                    kind="wheel-member",
                    name=operation.wheel_path,
                    wheel_path=operation.wheel_path,
                    description=f"wheel member {operation.wheel_path!r}",
                    scheme=operation.scheme,
                    logical_path=operation.destination_path,
                    location=source_location,
                )
                targets.append(
                    PlannedTarget.create(
                        source=origin,
                        scheme=operation.scheme,
                        logical_path=operation.destination_path,
                        filename=mapped,
                    )
                )
            finally:
                # Planning only needs record metadata/path.  Closing promptly
                # keeps WheelFile from retaining decompressed payload data.
                operation.stream.close()
            continue

        if isinstance(operation, _MetadataOperation):
            mapped = _preflight_resolve_target(
                destination, operation.scheme, operation.path
            )
            origin = InstallSource(
                kind="additional-metadata",
                name=operation.filename,
                wheel_path=operation.path,
                description=(
                    f"additional metadata {operation.filename!r} "
                    f"(written to {operation.path!r})"
                ),
                scheme=operation.scheme,
                logical_path=operation.path,
                location=source_location,
            )
            targets.append(
                PlannedTarget.create(
                    source=origin,
                    scheme=operation.scheme,
                    logical_path=operation.path,
                    filename=mapped,
                )
            )
            continue

        assert isinstance(operation, _RecordOperation)
        record_operation = operation
        record_mapped = _preflight_resolve_record_target(destination, operation)
        record_origin = InstallSource(
            kind="record",
            name=operation.path,
            wheel_path=operation.path,
            description=f"rewritten RECORD file {operation.path!r}",
            scheme=operation.scheme,
            logical_path=operation.path,
            location=source_location,
        )
        targets.append(
            PlannedTarget.create(
                source=record_origin,
                scheme=operation.scheme,
                logical_path=operation.path,
                filename=record_mapped,
            )
        )

    assert record_operation is not None
    return InstallationPlan(
        root_scheme=record_operation.scheme,
        record_file_path=record_operation.path,
        targets=tuple(targets),
    )


def preflight_installation(
    source: WheelSource,
    destination: WheelDestination,
    additional_metadata: dict[str, bytes],
) -> InstallationPlan:
    """Build the installation plan and raise before writes if targets conflict."""
    plan = build_install_plan(source, destination, additional_metadata)
    check_targets(plan.targets)
    return plan


def install(
    source: WheelSource,
    destination: WheelDestination,
    additional_metadata: dict[str, bytes],
    preflight: bool = False,
) -> None:
    """Install wheel described by ``source`` into ``destination``.

    :param source: wheel to install.
    :param destination: where to write the wheel.
    :param additional_metadata: additional metadata files to generate, usually
                                generated by the caller.
    :param preflight: resolve all targets and reject conflicts before creating
                      any destination directories or files. Enabling this may
                      iterate ``source.get_contents()`` one additional time.

    """
    if preflight:
        preflight_installation(source, destination, additional_metadata)

    written_records = []

    for operation in _iter_install_operations(source, additional_metadata):
        if isinstance(operation, _PycacheWarning):
            warnings.warn(
                (
                    f"Skip installing {operation.path} from {operation.distribution}."
                    " Installing files in a __pycache__ directory poses a security risk."
                    " __pycache__ directories should not be included in wheels."
                    f" This is probably an issue in the build process of '{operation.distribution}'."
                ),
                RuntimeWarning,
                stacklevel=2,
            )
            continue

        if isinstance(operation, _ScriptOperation):
            record = destination.write_script(
                name=operation.name,
                module=operation.module,
                attr=operation.attr,
                section=operation.section,
            )
            written_records.append((Scheme("scripts"), record))
            continue

        if isinstance(operation, _FileOperation):
            record = destination.write_file(
                scheme=operation.scheme,
                path=operation.destination_path,
                stream=operation.stream,
                is_executable=operation.is_executable,
            )
            written_records.append((operation.scheme, record))
            continue

        if isinstance(operation, _MetadataOperation):
            with BytesIO(operation.contents) as other_stream:
                record = destination.write_file(
                    scheme=operation.scheme,
                    path=operation.path,
                    stream=other_stream,
                    is_executable=False,
                )
            written_records.append((operation.scheme, record))
            continue

        assert isinstance(operation, _RecordOperation)
        written_records.append(
            (operation.scheme, RecordEntry(operation.path, None, None))
        )
        destination.finalize_installation(
            scheme=operation.scheme,
            record_file_path=operation.path,
            records=written_records,
        )
