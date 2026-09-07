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
from typing import Any, ClassVar

from .django_env import ensure_django
from .nplusone import analyse_template, set_include_search_dirs

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


def _model_of(node: ast.AST, locals_: dict[str, str]) -> str | None:
    """The model class name a value stands for, if it plainly is one.

    `Order.objects.filter(...)`, `Order.objects.get(...)`, or a local name
    assigned from one of those earlier in the same function.
    """
    if isinstance(node, ast.Name):
        return locals_.get(node.id)
    current = node
    while isinstance(current, (ast.Call, ast.Attribute)):
        current = current.func if isinstance(current, ast.Call) else current.value
    if isinstance(current, ast.Name):
        # Only a manager access counts. `datetime.now()` unwinds to `datetime`
        # and would otherwise be offered as a model.
        for attr in ast.walk(node):
            if isinstance(attr, ast.Attribute) and attr.attr in {"objects", "_default_manager"}:
                return current.id
    return None


class _RenderVisitor(ast.NodeVisitor):
    """Context handed to a template by a function view.

        def order_list(request):
            orders = Order.objects.all()
            return render(request, "shop/order_list.html", {"orders": orders})

    This is how most Django views are written, and none of it was resolvable
    before: on a real project the class-based reader supplied context for 28
    templates out of 2181, so the analysis had nothing to say about the other
    2153. The template name and the context keys are both literals here, and
    the values are ordinary queryset chains.
    """

    _RENDERERS: ClassVar[set[str]] = {
        "render", "TemplateResponse", "render_to_response",
    }

    def __init__(self) -> None:
        # template name -> {context variable: model class name}
        self.found: dict[str, dict[str, str]] = {}
        self._locals: dict[str, str] = {}
        self._dicts: dict[str, dict[str, str]] = {}

    def _visit_function(self, node: ast.AST) -> None:
        # Locals do not survive the function they were written in.
        saved_locals, saved_dicts = self._locals, self._dicts
        self._locals, self._dicts = {}, {}
        self.generic_visit(node)
        self._locals, self._dicts = saved_locals, saved_dicts

    visit_FunctionDef = _visit_function  # type: ignore[assignment]
    visit_AsyncFunctionDef = _visit_function  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            model = _model_of(node.value, self._locals)
            if model:
                self._locals[name] = model
            elif isinstance(node.value, ast.Dict):
                self._dicts[name] = self._read_dict(node.value)
        self.generic_visit(node)

    def _read_dict(self, node: ast.Dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                continue
            model = _model_of(value, self._locals)
            if model:
                out[key.value] = model
        return out

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        if name in self._RENDERERS:
            template = None
            context: dict[str, str] = {}
            for argument in node.args:
                if template is None and isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    template = argument.value
                elif isinstance(argument, ast.Dict):
                    context.update(self._read_dict(argument))
                elif isinstance(argument, ast.Name) and argument.id in self._dicts:
                    context.update(self._dicts[argument.id])
            for keyword in node.keywords:
                if keyword.arg == "template_name" and isinstance(keyword.value, ast.Constant):
                    template = keyword.value.value
                elif keyword.arg == "context":
                    if isinstance(keyword.value, ast.Dict):
                        context.update(self._read_dict(keyword.value))
                    elif isinstance(keyword.value, ast.Name) and keyword.value.id in self._dicts:
                        context.update(self._dicts[keyword.value.id])
            if template and context:
                self.found.setdefault(template, {}).update(context)
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

        render_visitor = _RenderVisitor()
        render_visitor.visit(tree)
        for template_name, context in render_visitor.found.items():
            resolved = {
                variable: by_class_name[model]
                for variable, model in context.items()
                if model in by_class_name
            }
            if resolved:
                mapping.setdefault(template_name, {}).update(resolved)

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

    # An include is written relative to a template directory, so the scan
    # hands over the roots it knows about for the engine to fall back on.
    set_include_search_dirs(
        [templates_dir] + [p.parent for p in templates_dir.rglob("templates") if p.is_dir()]
    )

    from_views = _view_context_map(views_dir)
    shared = dict(root_models or {})

    results: list[dict[str, Any]] = []
    analysed = 0
    skipped: list[str] = []
    unreadable: list[dict[str, str]] = []
    total_high = 0

    for path in _iter_templates(templates_dir):
        context: dict[str, str] = dict(shared)
        for template_name, mapping in from_views.items():
            if str(path).endswith(template_name):
                context.update(mapping)

        if not context:
            skipped.append(str(path.relative_to(templates_dir)))
            continue

        try:
            report = analyse_template(str(path), context)
        except Exception as exc:
            unreadable.append({
                "template": str(path.relative_to(templates_dir)),
                "error": f"{type(exc).__name__}: {exc}".split("\n")[0][:200],
            })
            continue
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
        "templates_unreadable": unreadable,
        "templates_unreadable_count": len(unreadable),
        "templates_with_findings": len(results),
        "high_severity_total": total_high,
        "results": results,
        "note": (
            "Context is resolved from class-based views that declare both "
            "template_name and model or queryset, and from render() and "
            "TemplateResponse calls whose template name and context keys are "
            "literals - which is how most function views are written. A "
            "context built at runtime, or handed through a helper, still "
            "cannot be read; pass root_models for those, or they are skipped "
            "and listed. A template that could "
            "not be parsed at all is listed under templates_unreadable with the "
            "reason rather than ending the run: a partial written to be included "
            "may depend on tags its parent loads, and one of those must not cost "
            "the other nine hundred."
        ),
    }
