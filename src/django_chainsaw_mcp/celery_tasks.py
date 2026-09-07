# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a task is handed, and what arrives at the other end.

    order = Order.objects.get(pk=pk)
    send_confirmation.delay(order)

The worker does not get that order. It gets whatever the serialiser made of
it, rehydrated later, on another machine, at a time nobody controls. Three
things follow, and all of them are quiet:

**The data is stale.** Between the `delay()` and the worker picking it up, the
row can change. The task acts on the snapshot, and the newer write is the one
that gets overwritten.

**It may not serialise at all.** Celery's default serialiser has been JSON
since 4.0, and a model instance is not JSON. Depending on configuration this is
a `TypeError` at the call site, a pickle of the entire object graph, or a
worker that fails to decode - and the first of those is the lucky case.

**The payload is the whole object.** Every field, including the ones the task
does not read, through the broker, for every call.

The fix is one character of intent: pass `order.pk`, and let the task load what
it needs at the moment it runs. Django's and Celery's documentation both say
so, and nothing checks it.

## What already exists

`flake8-pie` has Celery lints - explicit task names, crontab arguments,
expirations. None of them look at what is passed.

Celery's own `strict_typing` does check the signature, at **call time**. That
catches an arity mistake the moment the line runs, which for a nightly job or
an error branch is in production, months later, in a log nobody reads. Before
the deploy is a better time.
"""

from __future__ import annotations

import ast
from typing import Any

from .project import parse_file, read_source, resolve_root

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

_TASK_DECORATORS = {"shared_task", "task", "periodic_task"}
_DISPATCH = {"delay", "apply_async", "delay_on_commit", "s", "si"}

# Calls that hand back one model instance.
_FETCHES = {"get", "first", "last", "latest", "earliest", "get_object_or_404",
            "get_or_create", "update_or_create", "create"}

# A parameter named this way is asking for an identifier, not an object.
_ID_SUFFIXES = ("_id", "_pk", "_uuid", "_ids", "_pks")


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


def _is_task(node: ast.AST) -> bool:
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        name = _dotted(target).split(".")[-1]
        if name in _TASK_DECORATORS:
            return True
    return False


class _ImportMap(ast.NodeVisitor):
    """Local name -> the module it was imported from, for one file.

    Matching a dispatch to a task by bare name was wrong: this project's own
    fixtures have a plain object called `send_confirmation` in one module and a
    Celery task with the same name in another, and every call to the first was
    reported against the second's signature. A name is only a task if this file
    imported it from the module that defines it.
    """

    def __init__(self, module: str) -> None:
        self.module = module
        self.origins: dict[str, str] = {}

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            base = self.module.rsplit("/", 1)[0].replace("/", ".") if "/" in self.module else ""
            prefix = f"{base}.{node.module}" if node.module else base
        else:
            prefix = node.module or ""
        for alias in node.names:
            self.origins[alias.asname or alias.name] = f"{prefix}.{alias.name}".strip(".")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.origins[alias.asname or alias.name.split(".")[0]] = alias.name
        self.generic_visit(node)


class _TaskIndex(ast.NodeVisitor):
    """Every Celery task in the project, by name, with its parameters."""

    def __init__(self, module: str) -> None:
        self.module = module
        self.tasks: dict[str, dict[str, Any]] = {}

    def _visit(self, node: Any) -> None:
        if _is_task(node):
            args = node.args
            required = len(args.args) - len(args.defaults)
            # A bound task gets `self` first and the caller never passes it.
            bound = any(
                isinstance(d, ast.Call)
                and any(k.arg == "bind" and getattr(k.value, "value", False) is True
                        for k in d.keywords)
                for d in node.decorator_list
            )
            names = [a.arg for a in args.args]
            if bound and names:
                names = names[1:]
                required -= 1
            self.tasks[node.name] = {
                "module": self.module,
                "line": node.lineno,
                "parameters": names,
                "required": max(required, 0),
                "accepts_varargs": args.vararg is not None,
                "bound": bound,
            }
        self.generic_visit(node)

    visit_FunctionDef = _visit  # type: ignore[assignment]
    visit_AsyncFunctionDef = _visit  # type: ignore[assignment]


class _CallScan(ast.NodeVisitor):
    """Task dispatches in one function, and where their arguments came from."""

    def __init__(self, model_names: set[str]) -> None:
        self.model_names = model_names
        # variable -> the model it holds
        self.instances: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # A nested def is its own scope and is scanned as one, so descending
        # into it here counted every dispatch inside it twice. On a real
        # project that reported 11 dispatches where the source has 10.
        return

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            model = self._instance_of(node.value)
            if model:
                self.instances[node.targets[0].id] = model
        self.generic_visit(node)

    def _instance_of(self, node: ast.AST) -> str | None:
        """`Order.objects.get(...)` -> "Order", when Order is a known model."""
        if not isinstance(node, ast.Call):
            return None

        called = _dotted(node.func).split(".")[-1] if _dotted(node.func) else ""
        if not called and not isinstance(node.func, ast.Attribute):
            return None

        # get_object_or_404(Order, pk=pk) is a plain name call, not an
        # attribute one, and checking for Attribute first skipped it entirely.
        if called in {"get_object_or_404", "get_list_or_404", "aget_object_or_404"}:
            for argument in node.args:
                if isinstance(argument, ast.Name) and argument.id in self.model_names:
                    return argument.id
            return None

        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _FETCHES:
            return None
        chain = _dotted(node.func)
        head = chain.split(".")[0] if chain else ""
        if head in self.model_names:
            return head
        return None

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute) and node.func.attr in _DISPATCH:
            task_name = _dotted(node.func.value).split(".")[-1]
            positional = list(node.args)
            if node.func.attr == "apply_async":
                positional = []
                for keyword in node.keywords:
                    if keyword.arg == "args" and isinstance(keyword.value, (ast.List, ast.Tuple)):
                        positional = list(keyword.value.elts)
            self.calls.append({
                "task": task_name,
                "dispatch": node.func.attr,
                "line": node.lineno,
                "args": positional,
                "keywords": [k.arg for k in node.keywords if k.arg],
            })
        self.generic_visit(node)

    def argument_model(self, node: ast.AST) -> str | None:
        """Is this argument a model instance?"""
        if isinstance(node, ast.Name):
            return self.instances.get(node.id)
        direct = self._instance_of(node)
        if direct:
            return direct
        # `self.object`, `form.instance` - conventional names for an instance
        if isinstance(node, ast.Attribute) and node.attr in {"object", "instance"}:
            return "(an instance)"
        return None


def _resolve_task(
    name: str,
    origins: dict[str, str],
    tasks: dict[str, dict[str, Any]],
    relative: str,
) -> dict[str, Any] | None:
    """Is this dispatched name a task this project defines?

    Only when the file imported it from the module that defines it, or the
    task is defined in this same file. Matching on the bare name alone
    reported every call to an unrelated object of the same name against the
    task's signature.
    """
    task = tasks.get(name)
    if task is None:
        return None
    if task["module"] == relative:
        return task
    origin = origins.get(name)
    if origin is None:
        return None
    # `from .tasks import send_confirmation` in shop/views.py resolves to
    # "shop.tasks.send_confirmation"; the task lives in "shop/tasks.py".
    module_path = task["module"].removesuffix(".py").replace("/", ".")
    return task if origin.rsplit(".", 1)[0].endswith(module_path.rsplit(".", 1)[-1]) or \
        module_path.endswith(origin.rsplit(".", 1)[0].rsplit(".", 1)[-1]) else None


def _model_class_names() -> set[str]:
    try:
        from django.apps import apps

        return {model.__name__ for model in apps.get_models()}
    except Exception:
        return set()


def celery_arguments(search_path: str | None = None) -> dict[str, Any]:
    """Model instances handed to tasks, and calls whose arity cannot be right.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    root = resolve_root(search_path)
    model_names = _model_class_names()

    tasks: dict[str, dict[str, Any]] = {}
    trees: dict[str, tuple[ast.AST, list[str]]] = {}

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue
        relative = str(path.relative_to(root))
        trees[relative] = (tree, source.splitlines())
        index = _TaskIndex(relative)
        index.visit(tree)
        tasks.update(index.tasks)

    instance_args: list[dict[str, Any]] = []
    arity: list[dict[str, Any]] = []
    dispatches = 0

    for relative, (tree, lines) in sorted(trees.items()):
        imports = _ImportMap(relative)
        imports.visit(tree)

        # Each function once, plus whatever sits at module level. Walking the
        # module and then every function inside it scanned each call twice and
        # reported every finding twice.
        scopes: list[list[ast.AST]] = [
            node.body for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        scopes.append([
            statement for statement in tree.body
            if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ])

        for body in scopes:
            scan = _CallScan(model_names)
            for child in body:
                scan.visit(child)

            for call in scan.calls:
                task = _resolve_task(call["task"], imports.origins, tasks, relative)
                if task is None:
                    continue
                dispatches += 1
                code = (lines[call["line"] - 1].strip()[:140]
                        if call["line"] <= len(lines) else "")

                # -- a model instance where an identifier belongs -------------
                for position, argument in enumerate(call["args"]):
                    model = scan.argument_model(argument)
                    if model is None:
                        continue
                    parameter = (task["parameters"][position]
                                 if position < len(task["parameters"]) else f"#{position}")
                    expects_id = parameter.endswith(_ID_SUFFIXES)
                    instance_args.append({
                        "file": relative,
                        "line": call["line"],
                        "code": code,
                        "task": call["task"],
                        "task_defined_in": f"{task['module']}:{task['line']}",
                        "parameter": parameter,
                        "model": model,
                        "severity": "high",
                        "parameter_expects_id": expects_id,
                        "why": (
                            f"the worker does not get this {model}. It gets whatever the "
                            "serialiser made of it, rehydrated later on another machine: "
                            "the row may have changed in between, the whole object crosses "
                            "the broker, and with the JSON serialiser it may not encode at "
                            "all"
                            + (f". The parameter is called '{parameter}', which is asking "
                               "for an identifier" if expects_id else "")
                        ),
                        "fix": f"pass the identifier and let the task load it: "
                               f"{call['task']}.{call['dispatch']}(<obj>.pk)",
                    })

                # -- arity that cannot be right -------------------------------
                if task["accepts_varargs"] or call["keywords"]:
                    continue
                given = len(call["args"])
                allowed = len(task["parameters"])
                if given < task["required"] or given > allowed:
                    arity.append({
                        "file": relative,
                        "line": call["line"],
                        "code": code,
                        "task": call["task"],
                        "task_defined_in": f"{task['module']}:{task['line']}",
                        "given": given,
                        "expects": (f"{task['required']}"
                                    if task["required"] == allowed
                                    else f"{task['required']} to {allowed}"),
                        "parameters": task["parameters"],
                        "severity": "high",
                        "why": (
                            f"{call['task']} takes {task['required']} argument(s) and this "
                            f"passes {given}. Celery's strict_typing raises when the line "
                            "runs, which for a nightly job or an error branch is in "
                            "production, months later"
                        ),
                        "fix": f"the task signature is ({', '.join(task['parameters'])})",
                    })

    instance_args.sort(key=lambda f: (f["file"], f["line"]))
    arity.sort(key=lambda f: (f["file"], f["line"]))

    return {
        "search_path": str(root),
        "tasks_found": len(tasks),
        "dispatches_checked": dispatches,
        "instance_argument_count": len(instance_args),
        "arity_mismatch_count": len(arity),
        "finding_count": len(instance_args) + len(arity),
        "instance_arguments": instance_args,
        "arity_mismatches": arity,
        "note": (
            "A task handed a model instance does not receive that instance. It "
            "receives whatever the serialiser made of it, rehydrated later on "
            "another machine, so the row may have changed in between, the whole "
            "object crosses the broker, and under the JSON serialiser - the "
            "default since Celery 4 - it may not encode at all. Passing the "
            "primary key and loading it in the task is the documented fix.\n\n"
            "flake8-pie has Celery lints for task names, crontab arguments and "
            "expirations; none of them look at what is passed. Celery's own "
            "strict_typing checks the signature at call time, which catches an "
            "arity mistake the moment the line runs - in production, for any "
            "path that runs rarely.\n\n"
            "A dispatch is only checked when the task is defined in this "
            "project and matched by name, so a task imported under an alias, "
            "or one whose name is shared by two modules, is either skipped or "
            "matched to the wrong signature. Arity is not checked when the call "
            "uses keywords or the task takes *args."
        ),
    }
