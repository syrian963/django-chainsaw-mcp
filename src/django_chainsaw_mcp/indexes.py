# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Fields the code filters on that the database has no index for.

`django-indexes` answers this at runtime by intercepting requests and watching
what the ORM actually does. That is accurate and it only ever sees the paths
traffic reached: the monthly report, the admin action, the endpoint one customer
uses, none of them show up until somebody runs them.

Reading the source instead covers every path in the repository, works in CI with
no database and no traffic, and can run on a branch before it ships. The cost is
that it cannot weigh anything: a `filter(status=...)` on a table with forty rows
looks exactly like one on a table with forty million.

So this reports where an index is missing and how often the codebase asks for
it, and leaves the decision to somebody who knows the row counts.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import querysets
from .django_env import ensure_django

_LOOKUP_METHODS = {"filter", "exclude", "get", "order_by", "values", "values_list",
                   "get_or_create", "update_or_create", "distinct"}

# Suffixes Django strips before the field name.
_LOOKUPS = {
    "exact", "iexact", "contains", "icontains", "in", "gt", "gte", "lt", "lte",
    "startswith", "istartswith", "endswith", "iendswith", "range", "date",
    "year", "month", "day", "week", "week_day", "quarter", "time", "hour",
    "minute", "second", "isnull", "regex", "iregex", "overlap", "contained_by",
}

# Lookups an index does not help with, so flagging them is noise.
_INDEX_USELESS = {"contains", "icontains", "iexact", "regex", "iregex", "endswith", "iendswith"}

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations", "tests",
}


def _strip_lookup(key: str) -> tuple[str, str | None]:
    """`created_at__gte` -> ("created_at", "gte");  `order__customer` -> ("order__customer", None)."""
    if "__" not in key:
        return key, None
    head, _, tail = key.rpartition("__")
    if tail in _LOOKUPS:
        return head, tail
    return key, None


def _indexed_fields(model: Any) -> set[str]:
    """Every field the database can seek on for this model."""
    indexed: set[str] = set()

    def cover(field: Any) -> None:
        # A ForeignKey is `date` in the model and `date_id` in a queryset, and
        # both name the same indexed column. Recording only `field.name` meant
        # every `filter(date_id=...)` in a codebase was reported as needing an
        # index that Django had already created - on a real project that was
        # four of the seven most-reported candidates.
        indexed.add(field.name)
        attname = getattr(field, "attname", None)
        if attname:
            indexed.add(attname)

    for field in model._meta.get_fields():
        if not getattr(field, "concrete", False):
            continue
        if getattr(field, "primary_key", False) or getattr(field, "unique", False):
            cover(field)
        if getattr(field, "db_index", False):
            cover(field)
        # A ForeignKey gets an index unless it is explicitly turned off.
        if field.is_relation and field.many_to_one and getattr(field, "db_index", True):
            cover(field)

    by_name = {f.name: f for f in model._meta.get_fields() if getattr(f, "concrete", False)}

    def cover_name(name: str) -> None:
        name = name.lstrip("-")
        indexed.add(name)
        field = by_name.get(name)
        attname = getattr(field, "attname", None) if field is not None else None
        if attname:
            indexed.add(attname)

    meta = model._meta
    for index in getattr(meta, "indexes", []):
        for name in getattr(index, "fields", []):
            cover_name(name)
    for constraint in getattr(meta, "constraints", []):
        for name in getattr(constraint, "fields", []) or []:
            cover_name(name)
    for group in getattr(meta, "unique_together", ()) or ():
        # Only the leading column of a composite index is seekable on its own.
        if group:
            cover_name(group[0])
    for group in getattr(meta, "index_together", ()) or ():
        if group:
            indexed.add(group[0])

    return indexed


def _unwind(
    call: ast.Call,
    models: set[str],
    locals_: dict[str, str],
    class_models: dict[str, str],
) -> tuple[str | None, list[tuple[str, str]]] | None:
    """Flatten a queryset chain into (model class name, [(method, key), ...]).

    The receiver is resolved by `querysets`, which understands a custom
    manager, a local variable and `self.model` as well as `Model.objects`.
    """
    unwound = querysets.unwind(call, models, locals_, class_models)
    if unwound is None:
        return None
    model, chain = unwound
    used: list[tuple[str, str]] = []
    for method, node in chain:
        if method not in _LOOKUP_METHODS:
            continue
        for keyword in node.keywords:
            if keyword.arg:
                used.append((method, keyword.arg))
        if method in {"order_by", "values", "values_list", "distinct"}:
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    used.append((method, argument.value.lstrip("-")))
    return model, used


def missing_indexes(
    search_path: str | None = None,
    min_occurrences: int = 1,
) -> dict[str, Any]:
    """Report fields the code filters or sorts on that carry no index.

    Args:
        search_path: directory to scan. Defaults to the project path.
        min_occurrences: only report a field asked for at least this often.
    """
    config = ensure_django()
    from django.apps import apps

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    by_class = {m.__name__: m for m in apps.get_models()}
    index_cache: dict[str, set[str]] = {}

    # (model label, field) -> occurrences
    hits: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    skipped_lookups: dict[str, int] = defaultdict(int)
    files_scanned = 0

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue

        files_scanned += 1
        lines = source.splitlines()

        context = querysets.scopes(tree, set(by_class))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            names, class_models = querysets.context_for(context, node)
            result = _unwind(node, set(by_class), names, class_models)
            if result is None:
                continue
            class_name, used = result
            model = by_class.get(class_name or "")
            if model is None or not used:
                continue

            label = model._meta.label
            if label not in index_cache:
                index_cache[label] = _indexed_fields(model)
            indexed = index_cache[label]

            for method, key in used:
                field_name, lookup = _strip_lookup(key)

                # Traversals across a relation are the related table's problem.
                if "__" in field_name:
                    continue
                if lookup in _INDEX_USELESS:
                    skipped_lookups[lookup] += 1
                    continue
                try:
                    field = model._meta.get_field(field_name)
                except Exception:
                    continue
                if not getattr(field, "concrete", False):
                    continue
                if field_name in indexed:
                    continue

                hits[(label, field_name)].append(
                    {
                        "file": str(path.relative_to(root)),
                        "line": node.lineno,
                        "method": method,
                        "lookup": lookup,
                        "code": lines[node.lineno - 1].strip()[:140] if node.lineno <= len(lines) else "",
                    }
                )

    findings = []
    for (label, field_name), occurrences in hits.items():
        if len(occurrences) < min_occurrences:
            continue
        model = apps.get_model(label)
        field = model._meta.get_field(field_name)
        methods = sorted({o["method"] for o in occurrences})
        findings.append(
            {
                "model": label,
                "field": field_name,
                "field_type": type(field).__name__,
                "occurrences": len(occurrences),
                "methods": methods,
                "used_at": occurrences[:10],
                "severity": "high" if len(occurrences) >= 3 or "order_by" in methods else "medium",
                "suggested_model_change": (
                    f"{field_name} = models.{type(field).__name__}(..., db_index=True)"
                ),
                "suggested_meta_change": (
                    f'Meta.indexes = [models.Index(fields=["{field_name}"])]'
                ),
                "why": (
                    f"{label}.{field_name} is used in {', '.join(methods)} at "
                    f"{len(occurrences)} place(s) and has no index, primary key, "
                    "unique constraint or Meta index covering it."
                ),
            }
        )

    findings.sort(key=lambda f: (-f["occurrences"], f["model"], f["field"]))
    return {
        "search_path": str(root),
        "files_scanned": files_scanned,
        "candidate_count": len(findings),
        "high_severity_count": sum(1 for f in findings if f["severity"] == "high"),
        "findings": findings,
        "ignored_lookups": dict(skipped_lookups),
        "note": (
            "Static reading of queryset chains. It counts how often the code "
            "asks for a field, never how many rows are behind it, so a hot "
            "field on a small table and a cold one on a huge table look the "
            "same. Lookups a btree index cannot serve (contains, icontains, "
            "iexact, regex, endswith) are ignored rather than reported. "
            "Relation traversals belong to the other table and are skipped. "
            "Adding an index is not free: it costs write throughput and disk, "
            "so treat this as a shortlist, not a task list."
        ),
    }
