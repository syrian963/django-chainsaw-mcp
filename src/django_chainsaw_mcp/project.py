# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""What kind of Python project is this, and which checks can say anything?

Every check here began by calling `ensure_django()`, which meant a project
without a settings module got a boot error instead of an answer — including
for the checks that never needed Django at all. Reading money out of a
`Decimal`, losing an update between a read and a save, dispatching work inside
a transaction: those are properties of Python and of whatever database library
is in use, and the Django import was an accident of where the code started.

So the framework is detected rather than assumed, and each check declares what
it needs. As it stands that is 18 checks needing Django, one needing nothing
but Python, one FastAPI and one SQLAlchemy:

    needs Django       models, migrations, signals, templates, DRF, and also
                       decimal precision and read-modify-save, because both
                       of those resolve field definitions through the registry
    needs FastAPI      route inventory and what the routes serialise
    needs SQLAlchemy   relationship loading
    needs nothing      blocking calls on the event loop, and the call graph
                       every other check is built on

An earlier version of this docstring listed decimal precision and
read-modify-save under "needs nothing", which they are not: both walk
`apps.get_models()`. The aspiration was reasonable and the sentence was
false, which is the worse of the two.

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

# Directories that are inside the project tree without being the project.
# A virtualenv at `.venv/` is the normal layout, and everything installed into
# it sits under the project path - so "is this path under the root" is not the
# same question as "is this ours", and anything asking the second one has to
# come through here.
SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
}

# Kept for the readers that already import the private name.
_SKIP_DIRS = SKIP_DIRS


def is_vendored(path: Path) -> bool:
    """True when a path is inside the tree but not part of the project.

    Wagtail installs its dependencies into `/tmp/wag/.venv`, so Django's own
    `contenttypes` reports an app path with the project root in its parents.
    Treating that as one of the project's own apps put third-party migrations
    into a deploy report they were explicitly meant to stay out of.
    """
    return any(part in SKIP_DIRS for part in path.parts)

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


def fingerprint(root: Path) -> tuple[int, float, int]:
    """How many files, how recent, how large - enough to notice an edit.

    Stat-only, so it costs a fraction of a parse. The newest mtime alone is
    not enough: an edit within the same clock tick that leaves the length
    unchanged is exactly the case that bit this project once already, through
    Python's own bytecode cache. The total size catches the ordinary version
    of that.
    """
    count = 0
    newest = 0.0
    total = 0
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            info = path.stat()
        except OSError:
            continue
        count += 1
        total += info.st_size
        newest = max(newest, info.st_mtime)
    return (count, newest, total)


# One read and one parse per file, for the whole process.
#
# Twenty checks walk the project's Python files, and every one of them used to
# read and parse the tree itself. Measured on a codebase of 2144 files that is
# 5.4 seconds per pass, so roughly 108 of the 338 seconds a full `check` took
# were the same files being parsed twenty times over. One full `ast.walk` over
# all of them, by contrast, is 1.5 seconds - the parsing was the expensive
# half, not the analysis.
#
# The trade is memory: holding every tree costs 315 MB on that project. For a
# tool that has already booted Django and 424 models, that is the cheaper side
# of the deal.
#
# `read_source` and `parse_file` keep the signatures and the exceptions of the
# code they replace - OSError from a read, SyntaxError from a parse - so a
# caller's existing try/except still does exactly what it did.
# Off by default, and that is the whole design.
#
# A single CLI subcommand reads each file once. Retaining every tree for it
# buys nothing and costs 315 MB, and the benchmark said so plainly: holding
# the trees unconditionally took `check` from 35.6 s to 78 s and made every
# single-pass command two to three times slower. Allocation and garbage
# collection pressure is not free.
#
# So the cache is opt-in, and the two callers that ask many questions about
# one unchanged tree turn it on: `check`, which runs twenty analyses, and the
# MCP server, which answers for as long as it is running.
_caching = False
_read: dict[tuple[str, int, int], str] = {}
_parsed: dict[tuple[str, int, int], Any] = {}


def _key(path: Path) -> tuple[str, int, int] | None:
    """Identity of a file's current contents, or None if it cannot be stat'ed.

    A stat is microseconds against a parse's milliseconds, so paying it on
    every call is what makes the cache safe to use from a long-lived server:
    an edited file has a different key and is read again.
    """
    try:
        info = path.stat()
    except OSError:
        return None
    return (str(path), info.st_mtime_ns, info.st_size)


def enable_source_cache() -> None:
    """Keep every file read and parsed from here on. For many passes over one tree."""
    global _caching
    _caching = True


def disable_source_cache() -> None:
    """Stop retaining, and drop what is held."""
    global _caching
    _caching = False
    clear_source_cache()


def read_source(path: Path) -> str:
    """The file's text. Raises OSError exactly as `read_text` does."""
    if not _caching:
        return path.read_text(encoding="utf-8", errors="replace")
    key = _key(path)
    if key is None:
        return path.read_text(encoding="utf-8", errors="replace")
    if key not in _read:
        _read[key] = path.read_text(encoding="utf-8", errors="replace")
    return _read[key]


def parse_file(path: Path) -> Any:
    """The parsed module. Raises OSError or SyntaxError exactly as before.

    A file that failed to parse is not cached as a failure: the exception is
    what the caller expects, and re-raising a stored one would lose its
    traceback.
    """
    if not _caching:
        return ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    key = _key(path)
    source = read_source(path)
    if key is None:
        return ast.parse(source)
    if key not in _parsed:
        _parsed[key] = ast.parse(source)
    return _parsed[key]


def clear_source_cache() -> None:
    """Forget every cached file. For tests, and for a long-lived server."""
    _read.clear()
    _parsed.clear()


def source_cache_size() -> dict[str, int]:
    return {"files_read": len(_read), "files_parsed": len(_parsed)}


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
        self._trees: dict[str, Any | None] = {}
        self._index: dict[str, dict[tuple[str, int], Any] | None] = {}

    def module(self, relative: str) -> Any | None:
        """The parsed module, or None if it could not be read or parsed."""
        if relative not in self._trees:
            try:
                source = (self.root / relative).read_text(
                    encoding="utf-8", errors="replace"
                )
                self._trees[relative] = ast.parse(source)
            except (OSError, SyntaxError):
                self._trees[relative] = None
        return self._trees[relative]

    def definitions(self, relative: str) -> dict[tuple[str, int], Any] | None:
        if relative not in self._index:
            tree = self.module(relative)
            if tree is None:
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
