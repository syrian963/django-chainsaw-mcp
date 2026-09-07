# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Find querysets that read tenant-scoped data without scoping the query.

This is the bug class behind most IDOR reports: a view loads an object by
primary key and never checks who owns it. Generic static analysers are bad at
it, and say so, because there is no suspicious call to match on. The defect is
the *absence* of a filter, and absence has no syntax.

What makes it tractable here is the model graph. A generic tool sees

    Order.objects.filter(pk=pk)

and has no idea whether Order belongs to anybody. This server already knows that
Order reaches the tenant root through `customer`, so it can ask a much sharper
question: this model is owned, and this queryset filters on nothing that leads
to the owner.

Two halves:

1. Which models are tenant-scoped? Walk forward relations to the tenant root
   and keep the shortest path from each model. `OrderLine` reaches it through
   `order__customer`.
2. Which querysets touch them unscoped? Parse the source, unwind each queryset
   chain, and compare the filter keys against those paths.

Results are candidates, ranked. A filter applied in a base class, a mixin, or a
different method is invisible from here, which is why nothing is called a
vulnerability.
"""

from __future__ import annotations

import ast
import textwrap
from collections import deque
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from . import callgraph
from .django_env import ensure_django
from .project import parse_file, read_source

# Chain methods that can carry a scoping filter.
_FILTERING = {"filter", "exclude", "get", "get_or_create", "update_or_create"}
# Chain methods that return rows without narrowing them.
_UNSCOPED_ENTRY = {"all", "first", "last", "latest", "earliest", "count", "iterator"}
# Pure writes. There is no data to leak by inserting a row, so a chain that ends
# in one of these is not an authorisation question at all. get_or_create and
# update_or_create are deliberately absent: they read first.
_PURE_WRITES = {"create", "bulk_create"}

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox",
    ".mypy_cache", ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

# Files where an unscoped queryset is usually legitimate.
_EXEMPT_NAMES = {"admin.py", "management", "commands", "tests.py", "conftest.py", "factories.py"}


def _ownership_paths(root_label: str, max_depth: int) -> dict[str, dict[str, Any]]:
    """Shortest forward-relation path from every model to the tenant root.

    Breadth-first from the root outward along *reverse* relations, which is the
    same thing as a forward path from the other model back to the root.
    """
    from django.apps import apps

    try:
        root = apps.get_model(root_label)
    except (LookupError, ValueError) as exc:
        known = sorted(m._meta.label for m in apps.get_models())
        raise ValueError(
            f"Unknown tenant root '{root_label}'. Known models: {', '.join(known[:40])}"
        ) from exc

    paths: dict[str, dict[str, Any]] = {
        root._meta.label: {"path": None, "depth": 0, "via": "is the tenant root"}
    }
    queue: deque[tuple[Any, list[str], int]] = deque([(root, [], 0)])

    while queue:
        model, prefix, depth = queue.popleft()
        if depth >= max_depth:
            continue

        for rel in model._meta.related_objects:
            child = rel.related_model
            label = child._meta.label
            if label in paths:
                continue
            # rel.field is the FK on the child pointing back at `model`.
            step = rel.field.name
            path = [step, *prefix]
            paths[label] = {
                "path": "__".join(path),
                "depth": depth + 1,
                "via": f"{label}.{step}",
                "nullable": bool(rel.field.null),
            }
            queue.append((child, path, depth + 1))

    return paths


def _unwind(call: ast.Call) -> tuple[str | None, list[str], list[str]] | None:
    """Flatten a queryset chain into (model name, methods, filter keys).

    Order.objects.filter(customer=u).exclude(archived=True)
        -> ("Order", ["filter", "exclude"], ["customer", "archived"])
    """
    methods: list[str] = []
    keys: list[str] = []
    node: ast.AST = call

    while isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        methods.append(func.attr)
        if func.attr in _FILTERING:
            for keyword in node.keywords:
                if keyword.arg:
                    keys.append(keyword.arg)
            # Q objects and positional args are opaque here; record that.
            if node.args:
                keys.append("<positional>")
        node = func.value

    # node is now Order.objects or Order.objects.something
    while isinstance(node, ast.Attribute):
        if node.attr == "objects" or node.attr.endswith("_set"):
            base = node.value
            if isinstance(base, ast.Name):
                return base.id, list(reversed(methods)), keys
            return None, list(reversed(methods)), keys
        node = node.value

    return None


def _is_scoped(keys: Iterable[str], path: str | None) -> tuple[bool, str]:
    """Does any filter key lead to the tenant root?"""
    if path is None:
        return True, "model is the tenant root itself"

    first_step = path.split("__")[0]
    for key in keys:
        if key == "<positional>":
            return True, "positional or Q() argument, cannot be read statically"
        if key == path or key.startswith(f"{path}__"):
            return True, f"filters on the ownership path '{path}'"
        if key == first_step or key.startswith(f"{first_step}__"):
            return True, f"filters on '{first_step}', the first hop towards the owner"
        if key in {"pk", "id"} and path is None:
            return True, "primary key on the root"
    return False, ""


def _filter_keys_in(node: ast.AST) -> set[str]:
    """Every filter key applied anywhere inside a function body.

    Deliberately cruder than _unwind: the point here is only whether the
    method narrows by ownership at all, and the usual shape is
    `super().get_queryset().filter(customer=...)`, whose base is a call rather
    than a model name, so _unwind declines it.
    """
    keys: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
            continue
        if child.func.attr not in _FILTERING:
            continue
        for keyword in child.keywords:
            if keyword.arg:
                keys.add(keyword.arg)
        if child.args:
            keys.add("<positional>")
    return keys


def _class_filter_keys(graph: callgraph.CallGraph, root: Path) -> dict[str, tuple[set[str], str]]:
    """For each class, the keys its get_queryset narrows by, inherited included.

    This closes the mixin case. `class OrderViewSet(TenantScopedViewSet)` with
    `queryset = Order.objects.all()` on it reads as unscoped on the line the
    analysis sees, and is scoped on every request by a base class that the
    subclass never mentions. Reporting it is how an authorisation check earns
    a reputation for crying wolf.
    """
    parsed: dict[str, ast.AST] = {}
    out: dict[str, tuple[set[str], str]] = {}

    for qualname in graph.classes:
        for ancestor in graph.ancestors(qualname):
            fn = graph.functions.get(f"{ancestor}.get_queryset")
            if fn is None:
                continue
            tree = parsed.get(fn.file)
            if tree is None:
                try:
                    tree = ast.parse((root / fn.file).read_text(encoding="utf-8", errors="replace"))
                except (OSError, SyntaxError):
                    continue
                parsed[fn.file] = tree
            node = next(
                (
                    n for n in ast.walk(tree)
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and n.name == "get_queryset" and n.lineno == fn.line
                ),
                None,
            )
            if node is None:
                continue
            keys = _filter_keys_in(node)
            if keys:
                out[qualname] = (keys, ancestor)
                break

    return out


def _scoped_managers(paths: dict[str, Any]) -> dict[str, str]:
    """Models whose default manager already narrows by ownership.

    `Order.objects` is only a plain manager by convention. If `objects` is a
    manager whose get_queryset filters, then every `Order.objects.all()` in
    the project is scoped and none of them look it.
    """
    import inspect

    from django.apps import apps
    from django.db.models import Manager

    out: dict[str, str] = {}
    for model in apps.get_models():
        label = model._meta.label
        ownership = paths.get(label)
        if not ownership or ownership.get("path") is None:
            continue
        manager = getattr(model, "_default_manager", None)
        if manager is None or type(manager) is Manager:
            continue
        get_queryset = getattr(type(manager), "get_queryset", None)
        if get_queryset is None or get_queryset is Manager.get_queryset:
            continue
        try:
            source = inspect.getsource(get_queryset)
            node = ast.parse(textwrap.dedent(source))
        except (OSError, TypeError, SyntaxError, IndentationError):
            continue
        keys = _filter_keys_in(node)
        scoped, reason = _is_scoped(keys, ownership["path"])
        if scoped and keys:
            out[label] = f"{type(manager).__name__}.get_queryset() {reason}"
    return out


_PERMISSION_HINTS = (
    "has_object_permission", "check_object_permissions", "get_object_or_404",
    "has_perm", "user_can", "can_access", "ensure_owner", "assert_owner",
)


def _enclosing_function(tree: ast.AST, line: int) -> ast.AST | None:
    """The innermost def containing this line."""
    best = None
    best_span = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = node.end_lineno or node.lineno
        if not (node.lineno <= line <= end):
            continue
        span = end - node.lineno
        if best_span is None or span < best_span:
            best, best_span = node, span
    return best


def _narrowed_later(tree: ast.AST, line: int, path: str) -> str | None:
    """A filter applied further down the same function, to a variable.

        orders = Order.objects.all()
        if not request.user.is_staff:
            orders = orders.filter(customer=request.user.customer)

    The first line is what the analysis sees and it is not the whole story.
    Reading the rest of the function is cheap and removes a whole class of
    false positive. It is deliberately generous: any ownership filter anywhere
    below counts, because the cost of missing one real leak is lower than the
    cost of a check nobody trusts.
    """
    function = _enclosing_function(tree, line)
    if function is None:
        return None
    keys = {k for k in _filter_keys_in(function) if k != "<positional>"}
    scoped, reason = _is_scoped(keys, path)
    return reason if scoped else None


def _guarded_after_fetch(tree: ast.AST, line: int) -> str | None:
    """An object-level permission check after loading by primary key.

    Loading a row and then asking a permission class about it is a legitimate
    pattern and reads as unscoped. Naming the guard is not proof it is correct,
    so this downgrades the finding rather than removing it.
    """
    function = _enclosing_function(tree, line)
    if function is None:
        return None
    for node in ast.walk(function):
        name = ""
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name and any(hint in name for hint in _PERMISSION_HINTS):
            return name
    return None


def _iter_python(root: Path) -> Iterable[Path]:
    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        yield path


def _exempt(path: Path) -> bool:
    return any(part in _EXEMPT_NAMES for part in path.parts)


def find_unscoped_queries(
    tenant_root: str = "auth.User",
    search_path: str | None = None,
    max_depth: int = 4,
    include_exempt: bool = False,
) -> dict[str, Any]:
    """Report querysets on tenant-scoped models that carry no ownership filter.

    Args:
        tenant_root: the model that owns data, e.g. "auth.User" or "shop.Customer".
        search_path: directory to scan. Defaults to the project path.
        max_depth: how many relation hops still count as owned.
        include_exempt: also scan admin, management commands and tests, where an
            unscoped queryset is usually intentional.
    """
    config = ensure_django()
    from django.apps import apps

    root_dir = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root_dir.is_dir():
        raise ValueError(f"search_path is not a directory: {root_dir}")

    paths = _ownership_paths(tenant_root, max_depth)
    by_class = {m.__name__: m._meta.label for m in apps.get_models()}

    graph = callgraph.build(root_dir)
    class_keys = _class_filter_keys(graph, root_dir)
    scoped_managers = _scoped_managers(paths)
    suppressed: list[dict[str, Any]] = []

    findings: list[dict[str, Any]] = []
    scanned = 0
    chains_seen = 0

    for file_path in _iter_python(root_dir):
        if _exempt(file_path) and not include_exempt:
            continue
        try:
            source = read_source(file_path)
            tree = parse_file(file_path)
        except (OSError, SyntaxError):
            continue

        scanned += 1
        lines = source.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            result = _unwind(node)
            if result is None:
                continue
            class_name, methods, keys = result
            if not class_name:
                continue
            chains_seen += 1

            # Inserting a row cannot leak anybody's data. Flagging
            # Invoice.objects.create(order=instance) as an authorisation
            # problem is noise, and noise is how these tools get switched off.
            if any(method in _PURE_WRITES for method in methods):
                continue

            label = by_class.get(class_name)
            if not label or label not in paths:
                continue

            ownership = paths[label]
            if ownership["path"] is None:
                continue

            scoped, _reason = _is_scoped(keys, ownership["path"])
            if scoped:
                continue

            # The two cases that are scoped somewhere the line cannot show.
            # They are recorded rather than dropped, so a reviewer can see
            # what was ruled out and on what grounds.
            relative = str(file_path.relative_to(root_dir))

            if label in scoped_managers:
                suppressed.append({
                    "file": relative, "line": node.lineno, "model": label,
                    "scoped_by": "default manager",
                    "detail": scoped_managers[label],
                })
                continue

            later = _narrowed_later(tree, node.lineno, ownership["path"])
            if later:
                suppressed.append({
                    "file": relative, "line": node.lineno, "model": label,
                    "scoped_by": "a filter further down the same function",
                    "detail": later,
                })
                continue

            enclosing = graph.class_at(relative, node.lineno)
            if enclosing and enclosing in class_keys:
                inherited_keys, source_class = class_keys[enclosing]
                inherited_scoped, inherited_reason = _is_scoped(
                    inherited_keys, ownership["path"]
                )
                if inherited_scoped:
                    suppressed.append({
                        "file": relative, "line": node.lineno, "model": label,
                        "scoped_by": "get_queryset()",
                        "detail": (
                            f"{source_class}.get_queryset() {inherited_reason}"
                            + ("" if source_class == enclosing
                               else f", inherited by {enclosing}")
                        ),
                    })
                    continue

            guard = _guarded_after_fetch(tree, node.lineno)

            # An entry point that returns rows without narrowing them.
            terminal = [m for m in methods if m in _UNSCOPED_ENTRY or m in _FILTERING]
            findings.append(
                {
                    "file": str(file_path.relative_to(root_dir)),
                    "line": node.lineno,
                    "code": lines[node.lineno - 1].strip()[:160] if node.lineno <= len(lines) else "",
                    "model": label,
                    "owner_path": ownership["path"],
                    "hops": ownership["depth"],
                    "chain": ".".join(methods) or "(none)",
                    "permission_check_nearby": guard,
                    "filter_keys": keys,
                    "severity": "high" if ownership["depth"] == 1 else "medium",
                    "why": (
                        f"{label} belongs to {tenant_root} through "
                        f"'{ownership['path']}', but this queryset filters on "
                        f"{keys or 'nothing'}. Nothing here ties the rows to the "
                        "requesting user."
                    ),
                    "suggested": f".filter({ownership['path']}=<the request user>)",
                    "entry_methods": terminal,
                }
            )

    # ast.walk visits every Call in a chain, so Order.objects.filter(...).first()
    # unwinds twice: once from .first() and once from .filter(). Both are the
    # same queryset. Keep the outermost, which is the one with the longest chain.
    deduped: dict[tuple[str, int, str], dict[str, Any]] = {}
    for finding in findings:
        key = (finding["file"], finding["line"], finding["model"])
        previous = deduped.get(key)
        if previous is None or len(finding["chain"]) > len(previous["chain"]):
            deduped[key] = finding
    findings = list(deduped.values())

    findings.sort(key=lambda f: (f["severity"] != "high", f["file"], f["line"]))

    owned = {
        label: info for label, info in sorted(paths.items()) if info["path"] is not None
    }
    return {
        "tenant_root": tenant_root,
        "search_path": str(root_dir),
        "files_scanned": scanned,
        "queryset_chains_seen": chains_seen,
        "tenant_scoped_models": owned,
        "scoped_elsewhere": suppressed,
        "scoped_elsewhere_count": len(suppressed),
        "unscoped_count": len(findings),
        "high_severity_count": sum(1 for f in findings if f["severity"] == "high"),
        "findings": findings,
        "note": (
            "Candidates, not vulnerabilities. A filter applied in a base class, "
            "a mixin, a get_queryset() override, a custom manager, or simply "
            "further down the same view is invisible to this. It also treats a "
            "Q() or positional argument as scoped, because their contents "
            "cannot be read statically. Use it to shorten the list a human has "
            "to read, not to close the question. admin.py, management commands "
            "and tests are skipped unless include_exempt is set."
        ),
    }
