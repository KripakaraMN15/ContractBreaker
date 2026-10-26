"""Package version resolution without adding a dependency."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("contractbreaker")
except PackageNotFoundError:
    __version__ = "0.1.0"