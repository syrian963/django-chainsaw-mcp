# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Writes that skip everything the model promised would happen.

`what_happens_on` says: saving an Order creates an Invoice, queues a task,
clears a cache. Every one of those is true for `order.save()`. None of them is
true for these:

    Order.objects.bulk_create(orders)
    Order.objects.filter(status="draft").update(status="placed")
    Order.objects.bulk_update(orders, ["status"])

`bulk_create`, `bulk_update` and `QuerySet.update` go straight to SQL. They do
not call `save()`, so an overridden `save()` never runs, and they do not send
`pre_save`/`post_save`, so no receiver fires. Django documents this, in the
reference for each method, one sentence each. Nothing at the call site says it,
and nothing connects the call site to the list of effects that just did not
happen.

That is the finding: not "bulk_create bypasses signals", which everyone knows in
the abstract, but **this call, on this model, skips these four named effects**.
A cache that is never invalidated and a search index that quietly drifts are
both this bug, and both are found in production by a customer.

`QuerySet.delete()` is deliberately not here. Django collects the objects and
sends `pre_delete`/`post_delete` for each one, so the chain does fire. Listing
it would be a wrong finding dressed up as thoroughness.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .django_env import ensure_django
from .project import parse_file, read_source
from .tenancy import _unwind

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

# What each bypassing method skips. `update` runs on a queryset so the row
# count is unknown; the others take an explicit list.
_BYPASSING = {
    "bulk_create": "no save(), no pre_save, no post_save",
    "bulk_update": "no save(), no pre_save, no post_save",
    "update": "no save(), no pre_save, no post_save, on every matched row",
}


def _dedupe(names: list[str]) -> list[str]:
    """`audit_twice` connected twice is two receivers and one name."""
    counts: dict[str, int] = {}
    for name in names:
        counts[name] = counts.get(name, 0) + 1
    return [f"{n} (x{c})" if c > 1 else n for n, c in counts.items()]


def _effects_for(label: str, cache: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The save-chain for a model, computed once per label."""
    if label not in cache:
        from .signals import what_happens_on

        report = what_happens_on(label, event="save")
        steps = [s for s in report["chain"] if "receiver" in s and not s.get("unreadable")]
        cache[label] = {
            "receivers": _dedupe([s["receiver"] for s in steps if s.get("kind") != "override"]),
            "overrides": [s["receiver"] for s in steps if s.get("kind") == "override"],
            "models_written": report["models_written"],
            "side_effects": sorted({e["kind"] for e in report.get("side_effects", []) if "kind" in e}),
        }
    return cache[label]


def bypassed_effects(
    search_path: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Bulk writes on models whose save() chain they silently skip.

    Args:
        search_path: directory to scan. Defaults to the project root.
        model: restrict to one "app_label.ModelName".
    """
    config = ensure_django()
    from django.apps import apps

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    by_class = {m.__name__: m._meta.label for m in apps.get_models()}
    effects_cache: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    bulk_writes_seen = 0
    unresolved = 0
    files_scanned = 0

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue
        files_scanned += 1
        lines = source.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            method = node.func.attr
            if method not in _BYPASSING:
                continue

            result = _unwind(node)
            if result is None or not result[0]:
                # `qs.update(...)` on a variable: a real bypass on an unknown
                # model. Counted so the gap is visible, not reported as a
                # finding because the model, and therefore the effects, are
                # unknown.
                unresolved += 1
                continue
            class_name, methods, _ = result
            label = by_class.get(class_name)
            if label is None:
                continue
            if model and label != model:
                continue

            bulk_writes_seen += 1
            effects = _effects_for(label, effects_cache)
            skipped = effects["receivers"] + effects["overrides"]
            if not skipped:
                # Nothing was going to happen anyway. bulk_create on a model
                # with no chain is just fast, and calling it a finding is how
                # a check earns an ignore rule.
                continue

            findings.append(
                {
                    "file": str(path.relative_to(root)),
                    "line": node.lineno,
                    "code": lines[node.lineno - 1].strip()[:160] if node.lineno <= len(lines) else "",
                    "model": label,
                    "method": method,
                    "chain": ".".join(methods),
                    "what_is_skipped": _BYPASSING[method],
                    "receivers_not_fired": effects["receivers"],
                    "overrides_not_run": effects["overrides"],
                    "models_not_written": effects["models_written"],
                    "side_effects_not_happening": effects["side_effects"],
                    "severity": "high" if effects["models_written"] or effects["side_effects"] else "medium",
                    "fix": (
                        "If those effects must happen, loop and call save(), or run them "
                        "explicitly after the bulk write. If they must not, say so in a "
                        "comment at the call site so the next reader does not have to "
                        "rediscover this."
                    ),
                }
            )

    findings.sort(key=lambda f: (f["severity"] != "high", f["model"], f["file"], f["line"]))

    return {
        "search_path": str(root),
        "files_scanned": files_scanned,
        "bulk_writes_seen": bulk_writes_seen,
        "bulk_writes_on_unresolved_model": unresolved,
        "finding_count": len(findings),
        "high_severity_count": sum(1 for f in findings if f["severity"] == "high"),
        "findings": findings,
        "note": (
            "bulk_create, bulk_update and QuerySet.update go straight to SQL: no "
            "save(), no pre_save, no post_save. Everything what_happens_on lists "
            "for the model simply does not occur on these lines. Only calls on a "
            "model whose chain does something are reported; a bulk write on a "
            "model with no receivers and no save() override is just fast. "
            "QuerySet.delete() is not listed because Django sends the delete "
            "signals per object, so that chain does fire. A bulk write on a "
            "queryset held in a variable is counted under "
            "bulk_writes_on_unresolved_model and not reported, because the model "
            "is not knowable from the line and inventing one would be worse than "
            "the gap."
        ),
    }
