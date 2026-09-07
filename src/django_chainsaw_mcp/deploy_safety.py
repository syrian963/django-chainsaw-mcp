# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Cross-reference pending migrations against the code that still uses them.

A migration linter classifies an operation in isolation: RemoveField is
backward incompatible, always. That is true and, repeated often enough,
useless, because the warning fires just as loudly for a field nobody has
touched in two years as for one that half the codebase reads.

The question that actually decides a deploy is different: *has the code caught
up yet?* During a rolling deploy the old pods keep serving while the new schema
is already live. If a pending migration drops `Order.legacy_ref` and any code
path still reads it, those pods start throwing until the last one is replaced.
If nothing references it any more, the same migration is safe to ship today.

Python files are read with the AST, not with a text search, so comments,
docstrings and unrelated words cannot produce a hit. Templates fall back to
patterns because there is no comparable tree to walk.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .django_env import ensure_django
from .project import parse_file, read_source

_DESTRUCTIVE = {
    "RemoveField": "field",
    "RenameField": "field",
    "DeleteModel": "model",
    "RenameModel": "model",
    "RemoveConstraint": "constraint",
    "RemoveIndex": "index",
}

_PY_SUFFIX = ".py"
_TEMPLATE_SUFFIXES = {".html", ".txt", ".jinja", ".jinja2"}

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox",
    ".mypy_cache", ".pytest_cache", "static", "staticfiles", "media",
    "dist", "build", ".ruff_cache", "site-packages",
}

# Field names so common that an unscoped match says nothing.
_GENERIC = {
    "name", "id", "type", "value", "status", "code", "date", "title",
    "order", "state", "data", "text", "key", "label", "content",
}

_TEMPLATE_COMMENT = re.compile(r"\{#.*?#\}", re.S)


@dataclass
class Reference:
    path: str
    line: int
    text: str
    kind: str


class _PythonVisitor(ast.NodeVisitor):
    """Collect the places a field or model name is actually used."""

    def __init__(self, symbol: str, kind: str) -> None:
        self.symbol = symbol
        self.kind = kind
        self.hits: list[tuple[int, str]] = []

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == self.symbol:
            self.hits.append((node.lineno, "attribute access"))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if self.kind == "model" and node.id == self.symbol:
            self.hits.append((node.lineno, "model reference"))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # Docstrings are Constants too, but only an exact match counts, so a
        # sentence mentioning the field cannot register.
        if isinstance(node.value, str):
            if node.value == self.symbol:
                self.hits.append((node.lineno, "string field name"))
            elif node.value.startswith(f"{self.symbol}__") or node.value.endswith(f"__{self.symbol}"):
                self.hits.append((node.lineno, "orm lookup in string"))
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        if node.arg and (node.arg == self.symbol or node.arg.startswith(f"{self.symbol}__")):
            self.hits.append((node.value.lineno, "keyword argument"))
        self.generic_visit(node)


def _iter_files(root: Path) -> Iterable[Path]:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix != _PY_SUFFIX and path.suffix not in _TEMPLATE_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        # Migrations legitimately name the thing they remove.
        if "migrations" in path.parts:
            continue
        yield path


def _scan_python(path: Path, root: Path, symbol: str, kind: str) -> list[Reference]:
    try:
        source = read_source(path)
        tree = parse_file(path)
    except (OSError, SyntaxError):
        return []

    visitor = _PythonVisitor(symbol, kind)
    visitor.visit(tree)
    if not visitor.hits:
        return []

    lines = source.splitlines()
    seen: set[int] = set()
    out: list[Reference] = []
    for lineno, label in visitor.hits:
        if lineno in seen:
            continue
        seen.add(lineno)
        text = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        out.append(Reference(str(path.relative_to(root)), lineno, text[:160], label))
    return out


def _scan_template(path: Path, root: Path, symbol: str) -> list[Reference]:
    try:
        source = read_source(path)
    except OSError:
        return []

    source = _TEMPLATE_COMMENT.sub("", source)
    pattern = re.compile(rf"\.{re.escape(symbol)}\b|\b{re.escape(symbol)}\b\s*(?:\||\}}\}})")

    out: list[Reference] = []
    for number, line in enumerate(source.splitlines(), start=1):
        if pattern.search(line):
            out.append(Reference(str(path.relative_to(root)), number, line.strip()[:160], "template"))
    return out


def _scan(root: Path, symbol: str, kind: str, max_hits: int) -> list[Reference]:
    found: list[Reference] = []
    for path in _iter_files(root):
        found.extend(
            _scan_python(path, root, symbol, kind)
            if path.suffix == _PY_SUFFIX
            else _scan_template(path, root, symbol)
        )
        if len(found) >= max_hits:
            return found[:max_hits]
    return found


def _symbol_for(operation: Any, kind: str) -> str | None:
    if kind in {"field", "model"}:
        return getattr(operation, "old_name", None) or getattr(operation, "name", None)
    return getattr(operation, "name", None)


def _project_app_labels(root: Path) -> set[str]:
    """Apps whose code lives under the search path.

    Django's own contenttypes and auth migrations are not deploy decisions the
    user makes, and their field names are generic enough that scanning for them
    produces nothing but noise.
    """
    from django.apps import apps

    labels = set()
    for config in apps.get_app_configs():
        try:
            path = Path(config.path).resolve()
        except (TypeError, OSError):
            continue
        if path == root or root in path.parents:
            labels.add(config.label)
    return labels


def _raw_sql_sites(root: Path) -> list[dict[str, Any]]:
    """Every place this project writes SQL by hand.

    The column-reference search reads the ORM and templates. SQL assembled as
    a string is opaque to it, and a `clear` verdict on a RemoveField is only
    worth as much as the reader's confidence that nothing hand-written mentions
    the column. Listing the sites turns that from an unbounded worry into a
    finite list - usually a short one.
    """
    out: list[dict[str, Any]] = []
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue
        lines = source.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            kind = None
            if name == "execute" and isinstance(func, ast.Attribute):
                target = func.value
                base = getattr(target, "attr", None) or getattr(target, "id", None) or ""
                if "cursor" in str(base).lower():
                    kind = "cursor.execute()"
            elif name in {"raw", "extra"}:
                kind = f".{name}()"
            elif name == "RunSQL":
                kind = "RunSQL"
            if kind is None:
                continue
            out.append({
                "file": str(path.relative_to(root)),
                "line": node.lineno,
                "kind": kind,
                "code": lines[node.lineno - 1].strip()[:140] if node.lineno <= len(lines) else "",
            })
    out.sort(key=lambda entry: (entry["file"], entry["line"]))
    return out


def deploy_safety(
    search_path: str | None = None,
    max_hits_per_symbol: int = 25,
    include_third_party: bool = False,
) -> dict[str, Any]:
    """Check whether pending destructive migrations are safe to deploy yet.

    Args:
        search_path: directory to scan. Defaults to the configured project path.
        max_hits_per_symbol: stop after this many references per symbol.
        include_third_party: also analyse migrations from apps outside the
            project, such as django.contrib. Off by default: those are not your
            deploy decision and their field names are too generic to match on.
    """
    config = ensure_django()
    from django.db import connections
    from django.db.migrations.loader import MigrationLoader

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    # App ownership is a property of the project, not of the directory being
    # scanned. Deriving it from search_path made narrowing the scan silently
    # drop every migration from the analysis.
    own_apps = _project_app_labels(config.project_path)

    try:
        loader = MigrationLoader(connections["default"], ignore_no_migrations=True)
        applied = set(loader.applied_migrations or ())
        db_reachable = True
    except Exception:
        loader = MigrationLoader(None, ignore_no_migrations=True)
        applied = set()
        db_reachable = False

    blocking: list[dict[str, Any]] = []
    clear: list[dict[str, Any]] = []
    skipped_apps: set[str] = set()
    checked = 0

    for key, migration in sorted(loader.disk_migrations.items()):
        app_label = key[0]
        if key in applied:
            continue
        if app_label not in own_apps and not include_third_party:
            if any(_DESTRUCTIVE.get(type(op).__name__) for op in migration.operations):
                skipped_apps.add(app_label)
            continue

        for operation in migration.operations:
            kind = _DESTRUCTIVE.get(type(operation).__name__)
            if kind is None:
                continue
            symbol = _symbol_for(operation, kind)
            if not symbol:
                continue

            checked += 1
            references = _scan(root, symbol, kind, max_hits_per_symbol)
            entry: dict[str, Any] = {
                "app": app_label,
                "migration": key[1],
                "operation": type(operation).__name__,
                "symbol": symbol,
                "symbol_kind": kind,
                "model": getattr(operation, "model_name", None),
                "reference_count": len(references),
                "references": [r.__dict__ for r in references],
            }
            if symbol.lower() in _GENERIC:
                entry["confidence"] = "low"
                entry["confidence_reason"] = (
                    f"'{symbol}' is a common name; matches may belong to other models."
                )
            else:
                entry["confidence"] = "high"

            if references:
                entry["verdict"] = "blocking"
                entry["explanation"] = (
                    f"{entry['operation']} drops '{symbol}', but {len(references)} "
                    "place(s) still refer to it. During a rolling deploy the old "
                    "pods keep running against the new schema and will fail."
                )
                entry["safer"] = (
                    "Ship a release that stops using it, deploy that everywhere, "
                    "then ship this migration."
                )
                blocking.append(entry)
            else:
                entry["verdict"] = "clear"
                entry["explanation"] = (
                    f"{entry['operation']} drops '{symbol}' and no remaining "
                    "reference was found outside migrations. The code has caught up."
                )
                clear.append(entry)

    raw_sql = _raw_sql_sites(root)

    return {
        "database_reachable": db_reachable,
        "raw_sql_sites": raw_sql,
        "raw_sql_count": len(raw_sql),
        "search_path": str(root),
        "project_apps": sorted(own_apps),
        "skipped_third_party_apps": sorted(skipped_apps),
        "destructive_operations_checked": checked,
        "blocking_count": len(blocking),
        "clear_count": len(clear),
        "blocking": blocking,
        "clear": clear,
        "note": (
            "Python files are parsed with the AST, so comments and docstrings "
            "cannot produce a hit; templates use patterns. Dynamic access such "
            "as getattr(obj, name), raw SQL built at runtime, and references in "
            "other repositories are invisible. 'clear' means nothing was found "
            "here, not that nothing exists anywhere.\n\n"
            + (
                f"{len(raw_sql)} place(s) in this project build SQL by hand "
                "(cursor.execute, .raw(), .extra(), RunSQL). Those are listed "
                "under raw_sql_sites and none of them were searched for column "
                "references, so a 'clear' verdict is clear except for these. "
                "They are the specific reason the sentence above is not a "
                "guarantee, and they are short enough to read."
                if raw_sql else
                "No raw SQL was found in this project, so the search covered "
                "every place a column name could reasonably appear."
            )
        ),
    }
