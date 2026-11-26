"""Installer CLI."""

import argparse
import os.path
import sys
import sysconfig
from collections.abc import Sequence

import installer
from installer.destinations import SchemeDictionaryDestination
from installer.exceptions import (
    InstallationConflictError,
    InstallationRollbackError,
    PreflightNotSupportedError,
    RollbackNotSupportedError,
)
from installer.preflight import PlannedTarget, check_targets
from installer.sources import WheelFile
from installer.utils import get_launcher_kind


def _get_main_parser() -> argparse.ArgumentParser:
    """Construct the main parser."""
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=str, nargs="+", help="wheel file to install")
    parser.add_argument(
        "--destdir",
        "-d",
        metavar="path",
        type=str,
        help="destination directory (prefix to prepend to each file)",
    )
    parser.add_argument(
        "--prefix",
        "-p",
        metavar="path",
        type=str,
        help="override prefix to install packages to",
    )
    parser.add_argument(
        "--compile-bytecode",
        action="append",
        metavar="level",
        type=int,
        choices=[0, 1, 2],
        help="generate bytecode for the specified optimization level(s) (default=0, 1)",
    )
    parser.add_argument(
        "--no-compile-bytecode",
        action="store_true",
        help="don't generate bytecode for installed modules",
    )
    parser.add_argument(
        "--validate-record",
        metavar="part",
        default="none",
        type=str,
        choices=["all", "entries", "none"],
        help="validate the wheel against certain part of its record (default=none)",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="silently overwrite existing files",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "resolve all wheel, script, metadata, and RECORD targets and "
            "reject conflicts before creating any destination files"
        ),
    )
    parser.add_argument(
        "--rollback",
        action="store_true",
        help=(
            "if an installation raises, undo the files, scripts, and RECORD "
            "written by this invocation before exiting; bytecode compilation is "
            "disabled (passing --compile-bytecode too is an error)"
        ),
    )
    return parser


def _get_scheme_dict(
    distribution_name: str, prefix: str | None = None
) -> dict[str, str]:
    """Calculate the scheme dictionary for the current Python environment."""
    vars = {}
    if prefix is None:
        installed_base = sysconfig.get_config_var("base")
        assert installed_base
    else:
        vars["base"] = vars["platbase"] = installed_base = prefix

    scheme_dict = sysconfig.get_paths(vars=vars)

    # calculate 'headers' path, not currently in sysconfig - see
    # https://bugs.python.org/issue44445. This is based on what distutils does.
    # TODO: figure out original vs normalised distribution names
    scheme_dict["headers"] = os.path.join(  # noqa: PTH118
        sysconfig.get_path("include", vars={"installed_base": installed_base}),
        distribution_name,
    )

    return scheme_dict


def _make_destination(
    source: WheelFile, args: argparse.Namespace, bytecode_levels: list[int]
) -> SchemeDictionaryDestination:
    """Build the CLI destination for one opened wheel."""
    return SchemeDictionaryDestination(
        scheme_dict=_get_scheme_dict(source.distribution, prefix=args.prefix),
        interpreter=sys.executable,
        script_kind=get_launcher_kind(),
        bytecode_optimization_levels=bytecode_levels,
        destdir=args.destdir,
        overwrite_existing=args.overwrite_existing,
    )


def _main(cli_args: Sequence[str], program: str | None = None) -> None:
    """Process arguments and perform the install."""
    parser = _get_main_parser()
    if program:
        parser.prog = program
    args = parser.parse_args(cli_args)

    bytecode_levels: list[int] = args.compile_bytecode or []
    if args.no_compile_bytecode:
        bytecode_levels = []
    elif not bytecode_levels:
        bytecode_levels = [0, 1]

    if args.rollback:
        if args.compile_bytecode:
            parser.error(
                "--rollback cannot be combined with --compile-bytecode: .pyc "
                "files are written outside the rollback journal"
            )
        # Rollback requires bytecode compilation to be off; the documented
        # default would otherwise turn it on.
        bytecode_levels = []

    if not args.preflight:
        for wheel in args.wheel:
            with WheelFile.open(wheel) as source:
                if args.validate_record != "none":
                    source.validate_record(
                        validate_contents=args.validate_record == "all"
                    )
                destination = _make_destination(source, args, bytecode_levels)
                installer.install(source, destination, {}, rollback=args.rollback)
        return

    # For --preflight, resolve every wheel in this CLI invocation before any
    # installation pass creates a directory or file.
    planned_installations: list[tuple[str, SchemeDictionaryDestination]] = []
    all_targets: list[PlannedTarget] = []

    for wheel in args.wheel:
        with WheelFile.open(wheel) as source:
            if args.validate_record != "none":
                source.validate_record(validate_contents=args.validate_record == "all")
            destination = _make_destination(source, args, bytecode_levels)
            plan = installer.preflight_installation(source, destination, {})
            all_targets.extend(plan.targets)
            planned_installations.append((wheel, destination))

    # Also detect conflicts between different wheel arguments in this invocation.
    check_targets(all_targets)

    for wheel, destination in planned_installations:
        with WheelFile.open(wheel) as source:
            installer.install(
                source, destination, {}, preflight=True, rollback=args.rollback
            )


if __name__ == "__main__":  # pragma: no cover
    try:
        _main(sys.argv[1:], "python -m installer")
    except (
        InstallationConflictError,
        PreflightNotSupportedError,
        RollbackNotSupportedError,
        InstallationRollbackError,
    ) as exc:
        print(f"installer: error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
