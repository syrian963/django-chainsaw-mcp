# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Run the template analysis across a whole directory.

analyse_template answers for one file with a context you supply by hand. That
is fine from an assistant and useless in CI, where nobody types a context map
for four hundred templates. This walks a directory instead and resolves the
context per template where it can.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .django_env import ensure_django
from .nplusone import analyse_template

_TEMPLATE_SUFFIXES = {".html", ".jinja", ".jinja2"}
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox",
    ".mypy_cache", ".pytest_cache", "site-packages", "dist", "build",
}


def _iter_templates(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in _TEMPLATE_SUFFIXES
        and not any(part in _SKIP_DIRS for part in path.parts)
    )


class _ViewVisitor(ast.NodeVisitor):
    """Pull template_name plus a model or queryset out of class-based views.

    Only the declarative shape is handled, which is the common one:

        class OrderListView(ListView):
            model = Order
            template_name = "shop/order_list.html"
            context_object_name = "orders"

    Function views and anything computed at runtime are not resolvable
    statically and are left to the caller.
    """

    def __init__(self) -> None:
        self.views: list[dict[str, Any]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        found: dict[str, Any] = {}
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            if not isinstance(target, ast.Name):
                continue

            value = statement.value
            if target.id in {"template_name", "context_object_name"} and isinstance(
                value, ast.Constant
            ):
                found[target.id] = value.value
            elif target.id == "model" and isinstance(value, ast.Name):
                found["model"] = value.id
            elif target.id == "queryset":
                # Order.objects.all() -> Order
                current = value
                while isinstance(current, (ast.Call, ast.Attribute)):
                    current = current.func if isinstance(current, ast.Call) else current.value
                if isinstance(current, ast.Name):
                    found.setdefault("model", current.id)

        if "template_name" in found and "model" in found:
            found["view"] = node.name
            self.views.append(found)

        self.generic_visit(node)


def _view_context_map(root: Path) -> dict[str, dict[str, str]]:
    """template name -> {context variable: model label}, read from views."""
    from django.apps import apps

    by_class_name = {model.__name__: model._meta.label for model in apps.get_models()}
    mapping: dict[str, dict[str, str]] = {}

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue

        visitor = _ViewVisitor()
        visitor.visit(tree)
        for view in visitor.views:
            label = by_class_name.get(view["model"])
            if not label:
                continue
            names = {view.get("context_object_name") or "object_list", "object"}
            mapping.setdefault(view["template_name"], {}).update(
                {name: label for name in names if name}
            )
    return mapping


def scan_templates(
    template_root: str | None = None,
    project_root: str | None = None,
    root_models: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Analyse every template under a directory.

    Args:
        template_root: directory of templates. Defaults to the project path.
        project_root: where to look for views. Defaults to the project path.
        root_models: context variables applied to every template, for names
            that views cannot supply (a base context processor, for instance).
    """
    config = ensure_django()

    templates_dir = Path(template_root).expanduser().resolve() if template_root else config.project_path
    views_dir = Path(project_root).expanduser().resolve() if project_root else config.project_path
    if not templates_dir.is_dir():
        raise ValueError(f"template_root is not a directory: {templates_dir}")

    from_views = _view_context_map(views_dir)
    shared = dict(root_models or {})

    results: list[dict[str, Any]] = []
    analysed = 0
    skipped: list[str] = []
    total_high = 0

    for path in _iter_templates(templates_dir):
        context: dict[str, str] = dict(shared)
        for template_name, mapping in from_views.items():
            if str(path).endswith(template_name):
                context.update(mapping)

        if not context:
            skipped.append(str(path.relative_to(templates_dir)))
            continue

        report = analyse_template(str(path), context)
        analysed += 1
        total_high += report["high_severity_count"]
        if report["candidate_count"]:
            results.append(
                {
                    "template": str(path.relative_to(templates_dir)),
                    "context_used": context,
                    "high_severity_count": report["high_severity_count"],
                    "suggested_queryset": report["suggested_queryset"],
                    "findings": report["findings"],
                }
            )

    results.sort(key=lambda item: -item["high_severity_count"])
    return {
        "template_root": str(templates_dir),
        "templates_found": analysed + len(skipped),
        "templates_analysed": analysed,
        "templates_skipped_no_context": skipped,
        "templates_with_findings": len(results),
        "high_severity_total": total_high,
        "results": results,
        "note": (
            "Context is resolved from class-based views that declare both "
            "template_name and model or queryset. Function views and anything "
            "built at runtime cannot be resolved statically; pass root_models "
            "for those, or they are skipped and listed."
        ),
    }
