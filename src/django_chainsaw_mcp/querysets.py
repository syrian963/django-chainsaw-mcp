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


def needs_context(call: ast.Call) -> bool:
    """Could a local or `self.model` change the answer for this chain?

    Only a chain that bottoms out at a bare name or at `self` can: everything
    else either names a model directly or names nothing.

    This exists because the scope lookup is the expensive part, and it was
    being paid for every call node in the project - hundreds of thousands of
    them, almost none of which is a queryset. Walking the chain is a handful
    of steps; scanning the file's scopes is not.
    """
    node: ast.AST = call
    for _ in range(60):
        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Attribute):
                return False
            node = func.value
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "self":
                return True
            node = node.value
        else:
            break
    return isinstance(node, ast.Name)


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


def scopes(tree: ast.AST, models: set[str]) -> list[tuple[int, int, int, str, dict, dict]]:
    """Every scope in the file as (span, start, end, kind, locals, class models).

    The first version mapped every node id to its scope, which meant walking
    each scope in full - and a node inside a nested function was visited once
    per enclosing scope. On a real codebase that turned two checks from 22 s
    and 20 s into 66 s and 61 s.

    This walks each function body exactly once, to collect its locals, and
    leaves the lookup to a line comparison.

    A function with no queryset locals is still recorded, with an empty dict.
    Leaving it out let the module's locals apply inside it, which is how a
    `qs` in one function came to resolve to another function's model.
    """
    out: list[tuple[int, int, int, str, dict, dict]] = []

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
        if not class_models:
            continue
        end_line = cls.end_lineno or cls.lineno
        out.append((end_line - cls.lineno, cls.lineno, end_line, "class", {}, class_models))

    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        end_line = fn.end_lineno or fn.lineno
        out.append((end_line - fn.lineno, fn.lineno, end_line, "function",
                    locals_in(fn, models), {}))

    out.append((10 ** 9, 0, 10 ** 9, "module", locals_in(tree, models), {}))
    out.sort(key=lambda entry: entry[0])
    return out


def context_for(
    spans: list[tuple[int, int, int, str, dict, dict]], node: ast.AST
) -> tuple[dict[str, str], dict[str, str]]:
    """The (locals, class models) in force where this node appears.

    Spans are sorted smallest first, so the first one containing the line is
    the innermost. The locals come from the innermost function - or the module
    if the node is not in one - and the class models from the innermost class,
    because a method's span carries locals and its class's carries
    `self.model`.
    """
    line = getattr(node, "lineno", None)
    if line is None:
        return {}, {}
    names: dict[str, str] | None = None
    class_models: dict[str, str] | None = None
    for _span, start, end, kind, local, klass in spans:
        if not (start <= line <= end):
            continue
        if names is None and kind in {"function", "module"}:
            names = local
        if class_models is None and kind == "class":
            class_models = klass
        if names is not None and class_models is not None:
            break
    return names or {}, class_models or {}
