# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which model is this queryset about?

Almost every check here starts by answering that question, and for a long time
each of them answered it the same narrow way: walk the attribute chain, and
accept it only if it bottoms out at `Model.objects`.

Measured against a real codebase of 7056 queryset-shaped calls, that spelling
accounts for 2655 of them. The rest start somewhere else:

    self.<something>.filter(...)          1247 chains started at `self`
    request.user.orders.filter(...)        488 at `request`
    qs = Order.objects.all()               357 at a local
    qs.filter(...)

A check that only understands the first spelling reads a bit over a third of
the code and reports "nothing found" about the rest, which is the failure this
whole repository is built to avoid: an empty result that looks like a clean
one.

Sharing this resolution takes that project from 2655 to 2839. That is a 7%
gain, not a transformation, and the honest reason is that most of the
remainder needs type inference: `self.instance.items`, `request.user.orders`.
Those are not reachable from the syntax alone and are not guessed at here.

So the resolution lives here, once, and the checks share it. Three things it
understands that the per-check versions did not:

- **any manager**, not only one called `objects`. A project with
  `Order.available` is not a project to go quiet on.
- **a local variable**, resolved per function so that two functions using `qs`
  for two different models stay two different models. A name assigned from two
  models is dropped rather than guessed at.
- **`self.model`** inside a view or a viewset, when the class declares one,
  and **`self.get_queryset()`** when the class declares a model and has not
  replaced that method with one of its own. On the project measured above that
  last rule fires zero times - 566 classes declare a model and inherit the
  method, but almost nothing calls `self.get_queryset()` there. The rule is
  correct and it earns nothing on that codebase, which is worth saying rather
  than implying otherwise.

What it deliberately does not do is guess. `self.get_queryset()` could return
anything, a queryset handed in as an argument could be any model, and a wrong
model here means a finding pointed at the wrong code - which is worse than no
finding at all.
"""

from __future__ import annotations

import ast
from typing import Any

_MANAGER_NAMES = {"objects", "_default_manager"}


def _self_queryset(node: ast.AST, class_models: dict[str, str] | None) -> str | None:
    """`self.get_queryset()` in a class that declares a model.

    DRF's own implementation returns that model's queryset, so the class has
    said what it is - but only when it has not replaced the method with one of
    its own, which could return anything. `scopes()` decides that and records
    it as `inherited_queryset`.
    """
    if not class_models:
        return None
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and node.func.attr == "get_queryset"
    ):
        return None
    return class_models.get("inherited_queryset")


def receiver_model(
    node: ast.AST,
    models: set[str],
    locals_: dict[str, str] | None = None,
    class_models: dict[str, str] | None = None,
) -> str | None:
    """The model class name a chain bottoms out at, or None.

    Args:
        node: the expression the method chain was called on.
        models: every model class name in the project.
        locals_: names assigned a queryset earlier in the same function.
        class_models: enclosing class qualname -> the model it declares, for
            `self.model`.
    """
    resolved = _self_queryset(node, class_models)
    if resolved is not None:
        return resolved

    while isinstance(node, ast.Call):
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        node = func.value

    while isinstance(node, ast.Attribute):
        base = node.value
        if isinstance(base, ast.Name):
            # `Order.objects`, and `Order.available` for a custom manager.
            if node.attr in _MANAGER_NAMES or base.id in models:
                return base.id if base.id in models else None
            # `self.model` - the class said which model it is.
            if base.id == "self" and node.attr == "model" and class_models:
                return class_models.get("self")
        node = base

    if isinstance(node, ast.Name) and locals_:
        return locals_.get(node.id)
    return None


def unwind(
    call: ast.Call,
    models: set[str],
    locals_: dict[str, str] | None = None,
    class_models: dict[str, str] | None = None,
) -> tuple[str, list[tuple[str, ast.Call]]] | None:
    """(model class name, [(method, call node), ...]) for a queryset chain."""
    used: list[tuple[str, ast.Call]] = []
    node: ast.AST = call

    while isinstance(node, ast.Call):
        # The shape has to be recognised before the call is stripped: by the
        # time the loop reaches the bottom, `self.get_queryset()` looks
        # exactly like `self.anything()`.
        resolved = _self_queryset(node, class_models)
        if resolved is not None:
            return resolved, used
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        used.append((func.attr, node))
        node = func.value

    model = receiver_model(node, models, locals_, class_models)
    return (model, used) if model else None


def locals_in(scope: ast.AST, models: set[str]) -> dict[str, str]:
    """Names assigned a queryset in this scope, mapped to their model.

    Straight-line assignment only. A name assigned from two different models
    is dropped: the alternative is picking one, and picking wrong points a
    finding at code that does not have the problem.
    """
    found: dict[str, str] = {}
    conflicting: set[str] = set()

    for node in ast.walk(scope):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if value is None:
            continue

        resolved = receiver_model(value, models, found)
        if resolved is None and isinstance(value, ast.Attribute):
            if isinstance(value.value, ast.Name) and (
                value.attr in _MANAGER_NAMES or value.value.id in models
            ):
                resolved = value.value.id if value.value.id in models else None
        if resolved is None:
            continue

        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id in found and found[target.id] != resolved:
                conflicting.add(target.id)
            found[target.id] = resolved

    for name in conflicting:
        found.pop(name, None)
    return found


def _declared_model(cls: ast.ClassDef, models: set[str]) -> str | None:
    """`model = Order` in a class body, which views and viewsets declare."""
    for node in cls.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id in {"model", "queryset"}
            for t in node.targets
        ):
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id in models:
            return value.id
        resolved = receiver_model(value, models)
        if resolved:
            return resolved
    return None


def scopes(tree: ast.AST, models: set[str]) -> dict[int, dict[str, Any]]:
    """node id -> the locals and the `self.model` in force where it appears.

    Built once per file. A node inside a method sees its function's locals and
    its class's declared model; a node at module level sees the module's.
    """
    context: dict[int, dict[str, Any]] = {}

    def apply(scope: ast.AST, names: dict[str, str], class_models: dict[str, str]) -> None:
        for inner in ast.walk(scope):
            context.setdefault(id(inner), {"locals": names, "class": class_models})

    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        declared = _declared_model(cls, models)
        class_models: dict[str, str] = {"self": declared} if declared else {}
        # A class that writes its own get_queryset() can return anything, so
        # only an inherited one is safe to read the declared model from.
        overrides = any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "get_queryset"
            for node in cls.body
        )
        if declared and not overrides:
            class_models["inherited_queryset"] = declared
        for function in [n for n in ast.walk(cls)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            apply(function, locals_in(function, models), class_models)
        apply(cls, {}, class_models)

    for function in [n for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        apply(function, locals_in(function, models), {})

    apply(tree, locals_in(tree, models), {})
    return context


def context_for(context: dict[int, dict[str, Any]], node: ast.AST) -> tuple[
    dict[str, str], dict[str, str]
]:
    """The (locals, class models) pair for one node."""
    found = context.get(id(node)) or {}
    return found.get("locals") or {}, found.get("class") or {}
