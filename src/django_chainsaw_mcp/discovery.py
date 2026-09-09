# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Finding the classes that exist, not just the ones Python happened to load.

Four checks here walked `ModelSerializer.__subclasses__()`, and every one of
them carried the same footnote: *serializers in modules nothing imports at
startup do not exist yet*.

That footnote is worse than it sounds. Django imports `models.py` and
`admin.py` for every app, and whatever the URLconf reaches. It does **not**
import a `serializers.py` that only a management command uses, or one behind a
lazy import, or one belonging to an app whose URLs are not wired into the root
URLconf in this environment. Those serializers are real, they are served in
production, and the analysis quietly reported that they did not exist.

For an exposure check that is a false negative — the serializer leaking a
password hash is the one nobody imported. For a contract snapshot it is worse:
the field disappears from the snapshot rather than from the API, so the diff
says nothing changed.

The fix is to stop waiting to be told. Read the files, find the classes, and
import the modules that define them.

## Importing is a side effect, so it is done carefully

Only modules whose source actually declares a serializer subclass are imported,
each one in a try/except, and every failure is returned rather than swallowed.
A module that cannot be imported is a fact worth knowing — it usually means the
analysis is pointed at the wrong settings — and hiding it would turn an
environment problem into a silently incomplete answer.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

from .project import parse_file

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

# Cached per process: importing is not free and the answer cannot change
# without the process restarting, since a module imported once stays imported.
_loaded: dict[str, dict[str, Any]] = {}


def binding_site(cls: Any, project_root: Path | None = None) -> str | None:
    """Where the project binds this class, as `module.Name`, or None.

    `__subclasses__()` returns classes built at runtime as well as written
    ones, and the two need telling apart - but not by asking whether
    `__module__` agrees.

    Misago builds narrowed serializers with `type(name, (cls,), ...)` from a
    mixin, so the generated class reports `__module__` as
    `rest_framework.serializers` and carries a name made of every field it
    keeps:

        AuthenticatedUserSerializerIdUsernameSlugEmailJoinedOnRank...Subset

    That class is nevertheless bound as `AuthenticatedUserSerializer` in
    `misago/users/serializers/auth.py` and served from there. Judging it by
    `__module__` suppressed a real finding about the fields it exposes.

    So the question asked here is where the project binds the object, which is
    also the name worth printing: a reader can open that file. A class bound
    nowhere - a subset built and used inline - returns None, and the caller
    skips it, because there is no file to send anybody to.
    """
    import sys

    prefix = None
    if project_root is not None:
        prefix = project_root.name

    fallback = None
    for module_name, module in list(sys.modules.items()):
        if module is None or module_name.startswith("django.") or module_name == "django":
            continue
        if module_name.startswith("rest_framework"):
            continue
        try:
            names = vars(module)
        except TypeError:  # pragma: no cover - exotic module objects
            continue
        for attribute, value in list(names.items()):
            if value is not cls:
                continue
            site = f"{module_name}.{attribute}"
            if prefix and module_name.split(".")[0] == prefix:
                return site
            fallback = fallback or site
    return fallback


def serializer_label(cls: Any) -> str:
    """The name to print for a serializer class.

    Where the project binds it when that is known, and `__module__` plus the
    class name otherwise. The two differ exactly when the class was built at
    runtime, which is when the second one is unreadable.
    """
    return binding_site(cls) or f"{cls.__module__}.{cls.__qualname__}"


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _declares_serializer(tree: ast.AST) -> bool:
    """Does this module define a class that inherits something Serializer-shaped?

    Matched on the name rather than resolved, because a project base class
    called `BaseOrderSerializer` in another module is exactly the case that
    needs importing, and resolving it would need the import to have happened.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
            if name.endswith("Serializer"):
                return True
    return False


def load_serializer_modules(root: Path) -> dict[str, Any]:
    """Import every module that declares a serializer, so subclass walks see it."""
    key = str(root)
    if key in _loaded:
        return _loaded[key]

    imported: list[str] = []
    already: list[str] = []
    failed: list[dict[str, str]] = []
    slow: list[dict[str, Any]] = []

    import sys
    import time

    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue
        if not _declares_serializer(tree):
            continue

        module = _module_name(path, root)
        if not module:
            continue
        if module in sys.modules:
            already.append(module)
            continue
        started = time.perf_counter()
        started_cpu = time.process_time()
        try:
            importlib.import_module(module)
            imported.append(module)
        except Exception as exc:
            failed.append({"module": module, "error": f"{type(exc).__name__}: {exc}"})
        waited = time.perf_counter() - started
        # A second of wall clock for one import is already unusual. Recording
        # the CPU beside it separates "this module does a lot of work" from
        # "this module waited on something", which are different problems and
        # only the second one is nine minutes long.
        if waited >= 1.0:
            slow.append({
                "module": module,
                "seconds": round(waited, 2),
                "cpu_seconds": round(time.process_time() - started_cpu, 2),
            })

    slow.sort(key=lambda entry: -entry["seconds"])
    result = {
        "imported": imported,
        "already_loaded": already,
        "failed": failed,
        "slow_imports": slow[:10],
        "note": (
            "Modules declaring a serializer are imported so that a subclass walk "
            "can see them. Django only imports models.py, admin.py and whatever "
            "the URLconf reaches, so a serializer used by a management command "
            "or behind a lazy import was previously invisible - and an exposure "
            "check that cannot see a serializer reports it as safe."
            if imported or failed else
            "Every module declaring a serializer was already imported."
        ),
    }
    _loaded[key] = result
    return result


def serializers_used_by_views(root: Path | None = None) -> dict[str, list[str]]:
    """Which views name which serializer, so far as it is declared.

    A serializer no view uses is still in the contract, and removing it is then
    reported as breaking when nobody was reading it. This does not remove that
    finding; it labels it, which is the honest version, because
    `get_serializer_class` can return anything at runtime.
    """
    try:
        from rest_framework.generics import GenericAPIView
        from rest_framework.serializers import BaseSerializer
    except ModuleNotFoundError:
        return {}

    # Views the URLconf never reached are not in __subclasses__() either, and
    # without them every serializer looks unserved - which is the one wrong
    # answer this field must never give.
    if root is not None:
        load_view_modules(root)

    used: dict[str, list[str]] = {}
    seen: set[int] = set()

    def walk(cls: Any) -> list[Any]:
        out = []
        for subclass in cls.__subclasses__():
            if id(subclass) in seen:
                continue
            seen.add(id(subclass))
            out.append(subclass)
            out.extend(walk(subclass))
        return out

    for view in walk(GenericAPIView):
        serializer = getattr(view, "serializer_class", None)
        if not (isinstance(serializer, type) and issubclass(serializer, BaseSerializer)):
            continue
        label = f"{serializer.__module__}.{serializer.__qualname__}"
        used.setdefault(label, []).append(f"{view.__module__}.{view.__qualname__}")

    return used


def dynamic_serializer_views(root: Path | None = None) -> list[str]:
    """Views that choose their serializer at runtime.

    Their serializers cannot be attributed statically, so any "nothing serves
    this" claim has to be qualified while one of these exists.
    """
    try:
        from rest_framework.generics import GenericAPIView
    except ModuleNotFoundError:
        return []

    if root is not None:
        load_view_modules(root)

    out: list[str] = []
    seen: set[int] = set()

    def walk(cls: Any) -> None:
        for subclass in cls.__subclasses__():
            if id(subclass) in seen:
                continue
            seen.add(id(subclass))
            own = subclass.__dict__.get("get_serializer_class")
            if own is not None:
                out.append(f"{subclass.__module__}.{subclass.__qualname__}")
            walk(subclass)

    walk(GenericAPIView)
    return sorted(out)


_loaded_views: dict[str, dict[str, Any]] = {}


_VIEW_HINTS = ("View", "ViewSet")

_loaded_views: dict[str, dict[str, Any]] = {}


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def load_view_modules(root: Path) -> dict[str, Any]:
    """Import every module declaring a View subclass.

    The same reason `load_serializer_modules` exists, and the same failure:
    a view no URLconf reaches in this environment is not in
    `__subclasses__()`. On a real project this made `endpoint_cost` report
    zero endpoints and `api_contract` call every serializer unserved -
    both silently, both looking like clean results.
    """
    key = str(root)
    if key in _loaded_views:
        return _loaded_views[key]

    import sys

    imported: list[str] = []
    failed: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue
        declares = any(
            isinstance(node, ast.ClassDef)
            and any(
                (base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", ""))
                .endswith(_VIEW_HINTS)
                for base in node.bases
            )
            for node in ast.walk(tree)
        )
        if not declares:
            continue
        module = _module_name(path, root)
        if not module or module in sys.modules:
            continue
        try:
            importlib.import_module(module)
            imported.append(module)
        except Exception as exc:
            failed.append({"module": module, "error": f"{type(exc).__name__}: {exc}"})

    _loaded_views[key] = {"imported": imported, "failed": failed}
    return _loaded_views[key]
