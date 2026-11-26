"""A library for installing Python wheels."""

__version__ = "1.1.0"
__all__ = [
    "ConflictError",
    "InstallationConflictError",
    "InstallationRollbackError",
    "PreflightConflictError",
    "PreflightError",
    "PreflightNotSupportedError",
    "RollbackError",
    "RollbackNotSupportedError",
    "build_install_plan",
    "install",
    "preflight_installation",
]

from installer._core import build_install_plan, install, preflight_installation
from installer.exceptions import (
    ConflictError,
    InstallationConflictError,
    InstallationRollbackError,
    PreflightConflictError,
    PreflightError,
    PreflightNotSupportedError,
    RollbackError,
    RollbackNotSupportedError,
)
