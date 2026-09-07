# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Three concurrency defects that read as perfectly ordinary code.

## Read, modify, save

    product = Product.objects.get(pk=pk)
    product.stock -= quantity
    product.save()

Three lines, and a window between the first and the third. Two requests read
`stock = 10`, both subtract 3, both write 7. One sale is gone. Under load this
is not rare; it is the normal case, because the requests that collide are
exactly the ones for the product everybody is buying.

The fix is to let the database do the arithmetic:

    Product.objects.filter(pk=pk).update(stock=F("stock") - quantity)

or to hold a row lock across the read and the write:

    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=pk)
        product.stock -= quantity
        product.save()

Both are silent here. Only the unprotected form is a finding, and the check
needs all three parts on the same object in the same function before it says
anything: a fetch or a parameter, an augmented assignment to one of its
attributes, and a `save()` on it afterwards. Counters, balances, stock levels,
retry counts. These are the fields where being off by one costs money.

## `select_for_update()` with no transaction to hold the lock

    product = Product.objects.select_for_update().get(pk=pk)

Outside `atomic()` there is nothing for the lock to live in, and Django raises
`TransactionManagementError` the moment the queryset is evaluated. It is a
crash, not a race, and it is only found by the first request that hits the
line. Whether a transaction is open is exactly what the call graph already
knows, including one opened by a caller in another module and the implicit one
`ATOMIC_REQUESTS` puts around every view.

## `get_or_create()` on fields nothing makes unique

    Tag.objects.get_or_create(name=label)

Two requests miss the `get` at the same moment, both `create`, and there are
two tags called `label`. The next `get_or_create` raises
`MultipleObjectsReturned`, which the method does not catch. Django's own
documentation says this works only when the lookup fields carry a database
uniqueness constraint; the tickets about it (#12579, #29499) are old and will
stay open, because the database is the only thing that can enforce it. Whether
the lookup is covered by a unique field, a `unique_together` or a
`UniqueConstraint` is a fact about the model, and it is checked here.

Nothing in the linter ecosystem looks at any of these three. The Django docs
describe all three, in the reference for `F()`, `select_for_update()` and
`get_or_create()`,
which is where nobody is looking when they write the three lines above.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from . import callgraph
from .django_env import ensure_django

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

# Calls that hand back one model instance you might then mutate.
_FETCHES = {"get", "first", "last", "latest", "earliest", "get_or_create",
            "update_or_create", "get_object_or_404"}

# Names conventionally bound to a model instance in Django code. A parameter
# with one of these names is treated as an instance at medium confidence.
_INSTANCE_NAMES = {"instance", "obj", "object", "self", "item", "record", "row"}


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def _is_atomic(node: ast.AST) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    dotted = _dotted(target)
    return dotted == "atomic" or dotted.endswith(".atomic")


def _uses_f_expression(node: ast.AST) -> bool:
    """`F("stock") - qty` anywhere in the value: the database does the maths."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = child.func.id if isinstance(child.func, ast.Name) else (
                child.func.attr if isinstance(child.func, ast.Attribute) else ""
            )
            if name == "F":
                return True
    return False


def _chain_methods(call: ast.Call) -> set[str]:
    """Every method name in a queryset chain, e.g. {objects, select_for_update, get}."""
    out: set[str] = set()
    node: ast.AST = call
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        out.add(node.func.attr)
        node = node.func.value
    while isinstance(node, ast.Attribute):
        out.add(node.attr)
        node = node.value
    return out


class _FunctionScan(ast.NodeVisitor):
    """One function: where its instances come from, what mutates them, what saves them."""

    def __init__(self, base_depth: int) -> None:
        self.depth = base_depth
        # name -> ("fetched" | "locked" | "param", line)
        self.origins: dict[str, tuple[str, int]] = {}
        # name -> list of (attr, line, depth_at_write)
        self.mutations: dict[str, list[tuple[str, int, int]]] = {}
        # name -> list of save lines
        self.saves: dict[str, list[int]] = {}
        # select_for_update calls at depth 0: (line, code chain)
        self.unlocked_locks: list[int] = []

    def visit_With(self, node: ast.With) -> None:
        opened = any(_is_atomic(item.context_expr) for item in node.items)
        if opened:
            self.depth += 1
        self.generic_visit(node)
        if opened:
            self.depth -= 1

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        # product = Product.objects.[select_for_update().]get(...)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Call):
                methods = _chain_methods(value)
                if methods & _FETCHES or "get_object_or_404" in methods:
                    kind = "locked" if "select_for_update" in methods and self.depth > 0 else "fetched"
                    self.origins[node.targets[0].id] = (kind, node.lineno)
        # product.stock = product.stock - qty  (the long-hand form)
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Attribute):
            target = node.targets[0]
            if isinstance(target.value, ast.Name) and not _uses_f_expression(node.value):
                same = f"{target.value.id}.{target.attr}"
                if any(_dotted(n) == same for n in ast.walk(node.value) if isinstance(n, ast.Attribute)):
                    self.mutations.setdefault(target.value.id, []).append(
                        (target.attr, node.lineno, self.depth)
                    )
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        # product.stock -= qty
        target = node.target
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            if not _uses_f_expression(node.value):
                self.mutations.setdefault(target.value.id, []).append(
                    (target.attr, node.lineno, self.depth)
                )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "save" and isinstance(func.value, ast.Name):
                self.saves.setdefault(func.value.id, []).append(node.lineno)
            if func.attr == "select_for_update" and self.depth == 0:
                self.unlocked_locks.append(node.lineno)
        self.generic_visit(node)


_UPSERTS = {"get_or_create", "update_or_create"}


def _unique_sets(model: Any) -> list[frozenset[str]]:
    """Every set of field names the database guarantees unique, as names.

    A lookup is covered when it contains one of these sets in full. A lookup on
    a *superset* is also covered: if `sku` is unique then `sku, name` can
    match at most one row.
    """
    out: list[frozenset[str]] = [frozenset({"pk"}), frozenset({model._meta.pk.name})]
    for field in model._meta.concrete_fields:
        if getattr(field, "unique", False):
            out.append(frozenset({field.name}))
            if field.attname != field.name:
                out.append(frozenset({field.attname}))
    for group in getattr(model._meta, "unique_together", ()) or ():
        out.append(frozenset(group))
    for constraint in getattr(model._meta, "constraints", ()) or ():
        fields = getattr(constraint, "fields", None)
        # A conditional UniqueConstraint only holds where its condition is
        # true, and whether the lookup satisfies the condition is not
        # decidable here, so it is not counted as cover.
        if fields and getattr(constraint, "condition", None) is None:
            out.append(frozenset(fields))
    return out


def _upsert_findings(tree: ast.AST, relative: str, lines: list[str], by_class: dict[str, Any]) -> list[dict[str, Any]]:
    from .tenancy import _unwind

    out: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _UPSERTS:
            continue
        result = _unwind(node)
        if result is None or not result[0]:
            continue
        class_name, _methods, raw_keys = result
        model = by_class.get(class_name)
        if model is None:
            continue

        # `defaults=` is not a lookup, and a positional Q() cannot be read.
        keys = [k for k in raw_keys if k not in {"defaults", "<positional>"}]
        if "<positional>" in raw_keys or not keys:
            continue
        # `customer__email=...` reaches through a relation; whether the far
        # side is unique is a different model's business and is not judged.
        if any("__" in k and not k.endswith(("__exact",)) for k in keys):
            continue
        lookup = frozenset(k.removesuffix("__exact") for k in keys)

        unique_sets = _unique_sets(model)
        if any(group <= lookup for group in unique_sets):
            continue

        line = node.lineno
        out.append({
            "file": relative,
            "line": line,
            "code": lines[line - 1].strip()[:160] if line <= len(lines) else "",
            "model": model._meta.label,
            "method": node.func.attr,
            "lookup": sorted(lookup),
            "unique_on_model": sorted(sorted(g) for g in unique_sets if "pk" not in g and g != {model._meta.pk.name}),
            "severity": "high",
            "why": (
                "two requests miss the get at the same moment, both create, and "
                "there are two rows. The next call raises MultipleObjectsReturned, "
                "which get_or_create does not catch. Only a database constraint "
                "on the lookup fields prevents this"
            ),
            "fix": (
                f"add a UniqueConstraint(fields={sorted(lookup)}) to {model.__name__}.Meta "
                "and a migration; or look up by a field that is already unique"
            ),
        })
    return out


def _function_nodes(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def race_conditions(
    search_path: str | None = None,
    include_parameters: bool = True,
) -> dict[str, Any]:
    """Read-modify-save races, and row locks taken outside any transaction.

    Args:
        search_path: directory to scan. Defaults to the project root.
        include_parameters: also report mutations of an instance passed in as
            a parameter (medium confidence, since it may be unsaved or locked
            by the caller).
    """
    config = ensure_django()
    from django.conf import settings

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    atomic_requests = any(
        conf.get("ATOMIC_REQUESTS") for conf in getattr(settings, "DATABASES", {}).values()
    )
    graph = callgraph.build(root)
    in_caller_transaction = callgraph.inside_transaction(graph)

    from django.apps import apps

    by_class = {m.__name__: m for m in apps.get_models()}

    races: list[dict[str, Any]] = []
    unlocked: list[dict[str, Any]] = []
    upserts: list[dict[str, Any]] = []
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
        relative = str(path.relative_to(root))

        # Called inside this same iteration, so the late binding B023
        # warns about cannot happen here.
        def code(line: int) -> str:
            return lines[line - 1].strip()[:160] if line <= len(lines) else ""  # noqa: B023

        upserts.extend(_upsert_findings(tree, relative, lines, by_class))

        for fn in _function_nodes(tree):
            qualname = _qualname_of(graph, relative, fn)
            # A transaction may already be open around this whole function:
            # a decorator, a caller in another module, or ATOMIC_REQUESTS.
            decorated = any(_is_atomic(d) for d in fn.decorator_list)
            reached = qualname in in_caller_transaction if qualname else False
            wrapped_view = atomic_requests and _is_view(graph, qualname, fn)
            base_depth = 1 if (decorated or reached or wrapped_view) else 0

            scan = _FunctionScan(base_depth)
            for child in fn.body:
                scan.visit(child)

            params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}

            for name, mutations in scan.mutations.items():
                if name not in scan.saves:
                    continue
                origin = scan.origins.get(name)
                if origin is None:
                    if name in params and include_parameters:
                        kind, confidence = "parameter", "medium"
                    elif name in _INSTANCE_NAMES and include_parameters:
                        kind, confidence = "instance-like name", "medium"
                    else:
                        continue
                else:
                    kind = origin[0]
                    if kind == "locked":
                        # select_for_update inside atomic: the row is held
                        # from the read to the write. This is the fix, not
                        # the bug.
                        continue
                    confidence = "high"

                for attr, line, _depth in mutations:
                    if not any(save_line > line for save_line in scan.saves[name]):
                        continue
                    races.append({
                        "file": relative,
                        "line": line,
                        "code": code(line),
                        "function": qualname or fn.name,
                        "instance": name,
                        "field": attr,
                        "instance_origin": kind,
                        "confidence": confidence,
                        "saved_at_line": min(s for s in scan.saves[name] if s > line),
                        "severity": "high",
                        "why": (
                            "two requests read the same value, both change it in "
                            "Python, both write their own result; one change is lost. "
                            "A transaction does not help: neither request sees the "
                            "other's uncommitted write"
                        ),
                        "fix": (
                            f"{name}.__class__.objects.filter(pk={name}.pk)"
                            f".update({attr}=F(\"{attr}\") <op> delta)  # the database does the maths; "
                            f"or select_for_update() inside transaction.atomic() to hold the row"
                        ),
                    })

            for line in scan.unlocked_locks:
                unlocked.append({
                    "file": relative,
                    "line": line,
                    "code": code(line),
                    "function": qualname or fn.name,
                    "severity": "high",
                    "why": (
                        "select_for_update() needs a transaction to hold the lock in. "
                        "Outside atomic() Django raises TransactionManagementError when "
                        "the queryset is evaluated. This is a crash, found by the first "
                        "request that reaches it"
                    ),
                    "fix": "wrap the fetch and the write in `with transaction.atomic():`",
                })

    races.sort(key=lambda f: (f["confidence"] != "high", f["file"], f["line"]))
    unlocked.sort(key=lambda f: (f["file"], f["line"]))
    upserts.sort(key=lambda f: (f["file"], f["line"]))

    return {
        "search_path": str(root),
        "files_scanned": files_scanned,
        "atomic_requests": atomic_requests,
        "race_count": len(races),
        "unlocked_lock_count": len(unlocked),
        "unsafe_upsert_count": len(upserts),
        "finding_count": len(races) + len(unlocked) + len(upserts),
        "high_confidence_count": (
            sum(1 for f in races if f["confidence"] == "high") + len(unlocked) + len(upserts)
        ),
        "races": races,
        "locks_outside_transaction": unlocked,
        "unsafe_upserts": upserts,
        "note": (
            "A race is reported only when all three parts are on the same object "
            "in the same function: a fetch (or a parameter), an in-Python change "
            "to one of its fields, and a save() afterwards. F() expressions and a "
            "select_for_update() taken inside atomic() are the fixes, and both are "
            "silent. An instance passed in as a parameter is medium confidence, "
            "because the caller may hold the lock. A lock outside a transaction is "
            "judged with the call graph: a caller's atomic(), a decorator, or "
            "ATOMIC_REQUESTS on a view all count as a transaction. A get_or_create or "
            "update_or_create is unsafe when no unique field, unique_together or "
            "unconditional UniqueConstraint on the model covers its lookup; a lookup "
            "through a relation (customer__email) is another model's business and is "
            "not judged, and a positional Q() cannot be read. What this cannot "
            "see is a lock held by some other mechanism, such as an advisory lock "
            "or a distributed one, around the read-modify-save."
        ),
    }


def _qualname_of(graph: callgraph.CallGraph, relative: str, fn: ast.AST) -> str | None:
    for candidate in graph.functions.values():
        if candidate.file == relative and candidate.line == fn.lineno and candidate.name == fn.name:
            return candidate.qualname
    return None


_VIEW_HINTS = ("View", "ViewSet", "APIView")
_VIEW_METHODS = {"get", "post", "put", "patch", "delete", "head", "options",
                 "list", "create", "retrieve", "update", "partial_update", "destroy"}


def _is_view(graph: callgraph.CallGraph, qualname: str | None, fn: ast.AST) -> bool:
    args = fn.args.args
    if args and args[0].arg == "request":
        return True
    if not qualname:
        return False
    info = graph.functions.get(qualname)
    if info is None or not info.class_name or info.name not in _VIEW_METHODS:
        return False
    return any(
        any(hint in ancestor.rsplit(".", 1)[-1] for hint in _VIEW_HINTS)
        for ancestor in graph.ancestors(info.class_name)
    )
