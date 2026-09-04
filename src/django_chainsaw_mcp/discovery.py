# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

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

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

# Cached per process: importing is not free and the answer cannot change
# without the process restarting, since a module imported once stays imported.
_loaded: dict[str, dict[str, Any]] = {}


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

    import sys

    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
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
        try:
            importlib.import_module(module)
            imported.append(module)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            failed.append({"module": module, "error": f"{type(exc).__name__}: {exc}"})

    result = {
        "imported": imported,
        "already_loaded": already,
        "failed": failed,
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


def serializers_used_by_views() -> dict[str, list[str]]:
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


def dynamic_serializer_views() -> list[str]:
    """Views that choose their serializer at runtime.

    Their serializers cannot be attributed statically, so any "nothing serves
    this" claim has to be qualified while one of these exists.
    """
    try:
        from rest_framework.generics import GenericAPIView
    except ModuleNotFoundError:
        return []

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
