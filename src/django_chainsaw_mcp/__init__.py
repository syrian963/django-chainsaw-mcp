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
