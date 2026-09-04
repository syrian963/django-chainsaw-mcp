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
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from .django_env import ensure_django

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
            path = [step] + prefix
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

    findings: list[dict[str, Any]] = []
    scanned = 0
    chains_seen = 0

    for file_path in _iter_python(root_dir):
        if _exempt(file_path) and not include_exempt:
            continue
        try:
            source = file_path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
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

            scoped, reason = _is_scoped(keys, ownership["path"])
            if scoped:
                continue

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
