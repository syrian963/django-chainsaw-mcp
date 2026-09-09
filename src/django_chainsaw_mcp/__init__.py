# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""django-chainsaw-mcp: read-only introspection of Django projects over MCP.

Nothing is imported eagerly here. Importing .server at package level makes
``python -m django_chainsaw_mcp.server`` import the module twice and emit a
RuntimeWarning, so main() is resolved lazily instead.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .server import main

__all__ = ["main"]


def __getattr__(name: str):
    if name == "main":
        from .server import main

        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# The installed version, rather than a copy of it. Three places used to state
# it by hand - the MCP server's handshake and two SARIF defaults - and all
# three said 0.1.0 while the package on PyPI said 0.1.3, because nothing
# compares a string in the source against the release.
try:  # pragma: no cover - trivial, and the fallback is only for a source tree
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as _version

    __version__ = _version("django-chainsaw-mcp")
except PackageNotFoundError:  # running from a checkout that was never installed
    __version__ = "0.0.0+unknown"
