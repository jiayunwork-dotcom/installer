"""A library for installing Python wheels."""

__version__ = "1.1.0"
__all__ = [
    "install",
    "build_install_plan",
    "preflight_installation",
    "InstallationConflictError",
    "ConflictError",
    "PreflightConflictError",
    "PreflightError",
    "PreflightNotSupportedError",
]

from installer._core import build_install_plan, install, preflight_installation
from installer.exceptions import (
    ConflictError,
    InstallationConflictError,
    PreflightConflictError,
    PreflightError,
    PreflightNotSupportedError,
)
