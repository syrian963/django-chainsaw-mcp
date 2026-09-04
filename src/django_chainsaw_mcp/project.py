# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What kind of Python project is this, and which checks can say anything?

Every check here began by calling `ensure_django()`, which meant a project
without a settings module got a boot error instead of an answer — including
for the checks that never needed Django at all. Reading money out of a
`Decimal`, losing an update between a read and a save, dispatching work inside
a transaction: those are properties of Python and of whatever database library
is in use, and the Django import was an accident of where the code started.

So the framework is detected rather than assumed, and each check declares what
it needs:

    needs Django       models, migrations, signals, templates, DRF
    needs a web layer  endpoints, permissions, serialisation
    needs nothing      the call graph, decimal precision, read-modify-save

A project can be several of these at once. A Django project that also serves a
FastAPI app is normal, and detection reports every framework found rather than
picking a winner.

## Detection reads the source, not the environment

`import fastapi` succeeding proves the package is installed, which is a fact
about the virtualenv and not about the project. What matters is whether the
project's own files use it, so detection looks for the import in the source
tree and for the shapes the framework is used through.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
}

# Top-level module name -> the framework it means. Matched on the first
# segment of an import, so `fastapi.responses` counts as fastapi.
_FRAMEWORK_IMPORTS = {
    "django": "django",
    "rest_framework": "drf",
    "fastapi": "fastapi",
    "starlette": "starlette",
    "flask": "flask",
    "sqlalchemy": "sqlalchemy",
    "sqlmodel": "sqlmodel",
    "pydantic": "pydantic",
    "tortoise": "tortoise",
    "peewee": "peewee",
    "celery": "celery",
    "aiohttp": "aiohttp",
    "httpx": "httpx",
    "requests": "requests",
}


@dataclass
class ProjectProfile:
    """What was found, and how much of it."""

    root: Path
    frameworks: dict[str, int] = field(default_factory=dict)
    files_scanned: int = 0
    async_functions: int = 0
    sync_functions: int = 0

    def uses(self, name: str) -> bool:
        return self.frameworks.get(name, 0) > 0

    @property
    def is_django(self) -> bool:
        return self.uses("django")

    @property
    def is_async_web(self) -> bool:
        return self.uses("fastapi") or self.uses("starlette")

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "files_scanned": self.files_scanned,
            "frameworks": dict(sorted(self.frameworks.items(), key=lambda kv: -kv[1])),
            "async_functions": self.async_functions,
            "sync_functions": self.sync_functions,
            "note": (
                "Frameworks are counted by how many of the project's own files "
                "import them, not by what is installed: a package in the "
                "virtualenv that nothing imports is a fact about the "
                "environment. A project can be several of these at once and "
                "all of them are reported."
            ),
        }


def profile(root: Path) -> ProjectProfile:
    """Read the source tree and report what it is built on."""
    found = ProjectProfile(root=root)

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        found.files_scanned += 1

        seen_here: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    head = alias.name.split(".")[0]
                    if head in _FRAMEWORK_IMPORTS:
                        seen_here.add(_FRAMEWORK_IMPORTS[head])
            elif isinstance(node, ast.ImportFrom):
                if node.module and not node.level:
                    head = node.module.split(".")[0]
                    if head in _FRAMEWORK_IMPORTS:
                        seen_here.add(_FRAMEWORK_IMPORTS[head])
            elif isinstance(node, ast.AsyncFunctionDef):
                found.async_functions += 1
            elif isinstance(node, ast.FunctionDef):
                found.sync_functions += 1

        for name in seen_here:
            found.frameworks[name] = found.frameworks.get(name, 0) + 1

    return found


_cached: dict[str, ProjectProfile] = {}


def get_profile(root: Path) -> ProjectProfile:
    key = str(root)
    if key not in _cached:
        _cached[key] = profile(root)
    return _cached[key]


class NoProjectError(RuntimeError):
    """Raised when no project path is configured at all."""


def project_root() -> Path:
    """The directory to analyse, without requiring Django to boot.

    Falls back to the same environment variable the Django bootstrap uses, so
    an existing configuration keeps working and a non-Django project only has
    to set the path.
    """
    import os

    from .django_env import PROJECT_PATH_VAR

    raw = os.environ.get(PROJECT_PATH_VAR)
    if not raw:
        raise NoProjectError(
            f"{PROJECT_PATH_VAR} is not set. Point it at the directory to "
            "analyse. A Django project also needs "
            "DJANGO_CHAINSAW_SETTINGS_MODULE; the framework-independent "
            "checks do not."
        )
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise NoProjectError(f"{PROJECT_PATH_VAR} is not a directory: {root}")
    return root


def resolve_root(search_path: str | None = None, *, need_django: bool = False) -> Path:
    """The root a check should scan, booting Django only if it needs it.

    A check that reads models has to boot; one that reads source does not, and
    making it boot anyway is how a tool ends up refusing to analyse a FastAPI
    project for reasons that have nothing to do with the question asked.
    """
    if need_django:
        from .django_env import ensure_django

        config = ensure_django()
        root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    else:
        root = Path(search_path).expanduser().resolve() if search_path else project_root()

    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")
    return root


class TreeCache:
    """One parse per file, with its function definitions indexed.

    Two checks walk a call graph and then want the AST of one function at a
    time. Re-parsing the file for each of them cost 60 seconds on a project
    with 11300 functions, so the parse is done once and the definitions in it
    are indexed by (name, line) - which is exactly how the call graph
    identifies them.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._index: dict[str, dict[tuple[str, int], Any] | None] = {}

    def definitions(self, relative: str) -> dict[tuple[str, int], Any] | None:
        if relative not in self._index:
            try:
                source = (self.root / relative).read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
            except (OSError, SyntaxError):
                self._index[relative] = None
                return None
            self._index[relative] = {
                (node.name, node.lineno): node
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
        return self._index[relative]

    def function(self, fn: Any) -> Any | None:
        """The AST node for one call-graph function, or None."""
        index = self.definitions(fn.file)
        if index is None:
            return None
        return index.get((fn.name, fn.line))
