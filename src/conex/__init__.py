"""conex v2 — Confluence export tool, rewritten from scratch."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # Single source of truth: the installed distribution version (pyproject).
    __version__ = _pkg_version("conex")
except PackageNotFoundError:  # running from a source checkout that isn't installed
    __version__ = "2.0.0"
