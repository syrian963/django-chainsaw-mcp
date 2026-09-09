# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Who calls whom, across the whole project.

Every check in this package that reads one file at a time has the same blind
spot, and it is always the same sentence: *the boundary and the call are in
different modules*.

    # views.py
    @transaction.atomic
    def checkout(request):
        finalise(request.user)      # nothing here looks dangerous

    # services.py
    def finalise(user):
        send_receipt.delay(user.pk) # nothing here says it is in a transaction

Neither file is suspicious on its own. In a layered codebase this is where most
of these actually live, because the whole point of the layering is that
`services.py` does not know who called it.

Closing that needs one thing: a call graph. It is built once, cached on file
mtimes like everything else here, and then a check seeds it with the functions
it cares about and asks what they reach.

## What "resolved" means

Python's dynamic dispatch is not decidable, so this does not pretend to be a
whole-program analysis. It resolves what is written plainly:

    from .services import finalise     finalise()        -> app.services.finalise
    from . import services             services.x()      -> app.services.x
    import app.services                app.services.x()  -> app.services.x
    class C: def a(self): self.b()     self.b()          -> app.mod.C.b
    inherited methods                  self.b()          -> resolved through the MRO

and it counts what it could not resolve rather than dropping it, because a
silent gap in a call graph turns every check built on it into a confident wrong
answer.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .project import fingerprint, parse_file

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
}


@dataclass
class Function:
    """One `def` in the project, and what it calls."""

    qualname: str
    module: str
    name: str
    file: str
    line: int
    end_line: int
    class_name: str | None = None
    decorators: tuple[str, ...] = ()
    # Callees split by whether the call site sits inside a transaction opened
    # in this same function. A check that cares about transactions needs the
    # distinction; one that only cares about reachability can union them.
    calls: set[str] = field(default_factory=set)
    calls_in_atomic: set[str] = field(default_factory=set)
    # Callees reached only through transaction.on_commit(...). They run after
    # the commit, so they are the correctly written version and must never be
    # counted as inside the transaction. Without this bucket the analysis
    # cannot tell checkout() from checkout_correctly(), and a check that
    # flags the fixed code alongside the broken code is worse than no check.
    calls_deferred: set[str] = field(default_factory=set)
    # Names that appeared in a call position and could not be resolved to a
    # project function. Kept so the gap is countable.
    unresolved: set[str] = field(default_factory=set)
    opens_atomic: bool = False


@dataclass
class Class:
    """One `class` in the project, and where it lives."""

    qualname: str
    module: str
    name: str
    file: str
    line: int
    end_line: int
    bases: list[str] = field(default_factory=list)


@dataclass
class CallGraph:
    functions: dict[str, Function]
    # module -> {local name: qualified target}
    imports: dict[str, dict[str, str]]
    # class qualname -> base class qualnames, in declaration order. Collected
    # as the bare names the source writes and rewritten to qualnames in the
    # resolution pass, because `class Service(Base)` says "Base" and nothing
    # about which module that is.
    bases: dict[str, list[str]]
    # class qualname -> the module it was declared in, needed to resolve those
    # base names against that module's imports.
    class_module: dict[str, str] = field(default_factory=dict)
    classes: dict[str, Class] = field(default_factory=dict)
    files_scanned: int = 0
    unresolved_calls: int = 0

    def ancestors(self, class_qualname: str) -> list[str]:
        """The class and everything it inherits from, nearest first."""
        out: list[str] = []
        seen: set[str] = set()
        stack = [class_qualname]
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.add(current)
            out.append(current)
            stack.extend(self.bases.get(current, []))
        return out

    def class_at(self, file: str, line: int) -> str | None:
        """The innermost class whose body contains this line."""
        best: str | None = None
        best_span = None
        for cls in self.classes.values():
            if cls.file != file or not (cls.line <= line <= cls.end_line):
                continue
            span = cls.end_line - cls.line
            if best_span is None or span < best_span:
                best, best_span = cls.qualname, span
        return best

    def resolve_method(self, class_qualname: str, method: str) -> str | None:
        """`self.b()` through the declared bases, depth first."""
        seen: set[str] = set()
        stack = [class_qualname]
        while stack:
            current = stack.pop(0)
            if current in seen:
                continue
            seen.add(current)
            candidate = f"{current}.{method}"
            if candidate in self.functions:
                return candidate
            stack.extend(self.bases.get(current, []))
        return None

    def reachable(
        self,
        seeds: Iterable[str],
        *,
        transaction_aware: bool = False,
    ) -> dict[str, list[str]]:
        """Everything the seeds reach, with one path to each as the reason.

        With `transaction_aware`, a seed's own callees only count when the call
        site is inside its `atomic()` block, while everything reached from
        there counts wholesale — once you are inside a transaction, every line
        of every function you call is inside it too.
        """
        paths: dict[str, list[str]] = {}
        queue: list[tuple[str, list[str]]] = []

        for seed in seeds:
            fn = self.functions.get(seed)
            if fn is None:
                continue
            first = (
                fn.calls_in_atomic
                if transaction_aware
                else (fn.calls | fn.calls_in_atomic | fn.calls_deferred)
            )
            for callee in sorted(first):
                if callee not in paths:
                    paths[callee] = [seed, callee]
                    queue.append((callee, paths[callee]))

        while queue:
            current, path = queue.pop(0)
            fn = self.functions.get(current)
            if fn is None:
                continue
            # Once inside a transaction, everything a function calls is inside
            # it too - except what it explicitly defers to on_commit.
            onward = fn.calls | fn.calls_in_atomic
            if not transaction_aware:
                onward = onward | fn.calls_deferred
            for callee in sorted(onward):
                if callee in paths:
                    continue
                # A cycle would otherwise walk forever; the first path to a
                # function is also the shortest one, which is the one worth
                # showing a human.
                if callee in path:
                    continue
                paths[callee] = [*path, callee]
                queue.append((callee, paths[callee]))

        return paths


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


# Calls that push their argument past the commit. Kept in step with the same
# set in on_commit.py.
_DEFERRING = {"on_commit", "delay_on_commit", "apply_async_on_commit"}


def _module_name(path: Path, root: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _Collector(ast.NodeVisitor):
    """One pass over one module: its imports, classes, functions and calls."""

    def __init__(self, module: str, file: str) -> None:
        self.module = module
        self.file = file
        self.imports: dict[str, str] = {}
        self.bases: dict[str, list[str]] = {}
        self.class_modules: dict[str, str] = {}
        self.classes: list[Class] = []
        self.functions: list[Function] = []
        self._class_stack: list[str] = []
        self._fn_stack: list[Function] = []
        self._atomic_depth = 0
        self._deferred = 0

    # -- imports ---------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            local = alias.asname or alias.name.split(".")[0]
            target = alias.name if alias.asname else alias.name.split(".")[0]
            self.imports[local] = target
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            # Relative: `from .services import x` inside `shop.views` is
            # `shop.services.x`, and each extra dot climbs one more package.
            base_parts = self.module.split(".")[: -node.level] if self.module else []
            prefix = ".".join(base_parts + ([node.module] if node.module else []))
        else:
            prefix = node.module or ""
        for alias in node.names:
            local = alias.asname or alias.name
            self.imports[local] = f"{prefix}.{alias.name}" if prefix else alias.name
        self.generic_visit(node)

    # -- structure -------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = ".".join([self.module, *self._class_stack, node.name])
        self.bases[qualname] = [_dotted(b) for b in node.bases if _dotted(b)]
        self.class_modules[qualname] = self.module
        self.classes.append(Class(
            qualname=qualname,
            module=self.module,
            name=node.name,
            file=self.file,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
        ))
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def _visit_function(self, node: Any) -> None:
        qualname = ".".join([self.module, *self._class_stack, node.name])
        fn = Function(
            qualname=qualname,
            module=self.module,
            name=node.name,
            file=self.file,
            line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            class_name=".".join([self.module, *self._class_stack]) if self._class_stack else None,
            # A decorator written with arguments is a Call, and _dotted
            # returns "" for one. Recording `shared_task` for both
            # `@shared_task` and `@shared_task(bind=True)` is the only way a
            # consumer can ask what a function is.
            decorators=tuple(
                _dotted(d.func if isinstance(d, ast.Call) else d)
                for d in node.decorator_list
            ),
        )
        decorated = any(_is_atomic(d) for d in node.decorator_list)
        fn.opens_atomic = decorated

        self.functions.append(fn)
        self._fn_stack.append(fn)
        # A function decorated @transaction.atomic has its whole body inside
        # one, so the depth starts at 1 rather than 0.
        saved_depth = self._atomic_depth
        self._atomic_depth = 1 if decorated else 0
        self.generic_visit(node)
        self._atomic_depth = saved_depth
        self._fn_stack.pop()

    visit_FunctionDef = _visit_function  # type: ignore[assignment]
    visit_AsyncFunctionDef = _visit_function  # type: ignore[assignment]

    def visit_With(self, node: ast.With) -> None:
        opened = any(_is_atomic(item.context_expr) for item in node.items)
        if opened:
            self._atomic_depth += 1
            if self._fn_stack:
                self._fn_stack[-1].opens_atomic = True
        self.generic_visit(node)
        if opened:
            self._atomic_depth -= 1

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    # -- calls -----------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else ""
        )

        if name in _DEFERRING:
            self._deferred += 1
            self.generic_visit(node)
            self._deferred -= 1
            return

        if self._fn_stack:
            dotted = _dotted(node.func)
            if dotted:
                if self._deferred:
                    bucket = self._fn_stack[-1].calls_deferred
                elif self._atomic_depth > 0:
                    bucket = self._fn_stack[-1].calls_in_atomic
                else:
                    bucket = self._fn_stack[-1].calls
                # Stored raw here; a second pass resolves them once every
                # module's functions are known.
                bucket.add(dotted)
        self.generic_visit(node)


def _resolve_symbol(graph: CallGraph, module: str, dotted: str) -> str:
    """A bare name written in `module` as the qualified thing it refers to."""
    module_imports = graph.imports.get(module, {})
    head, _, rest = dotted.partition(".")
    if head in module_imports:
        target = module_imports[head]
        return f"{target}.{rest}" if rest else target
    same_module = f"{module}.{dotted}" if module else dotted
    if same_module in graph.bases or same_module in graph.functions:
        return same_module
    return dotted


def _resolve_bases(graph: CallGraph) -> None:
    """`class Service(Base)` names a local `Base`; make it a qualname.

    Without this, method inheritance never resolves and every `self.x()` call
    on an inherited method silently disappears from the graph.
    """
    for class_qualname, raw_bases in list(graph.bases.items()):
        module = graph.class_module.get(class_qualname, "")
        resolved = [_resolve_symbol(graph, module, base) for base in raw_bases]
        graph.bases[class_qualname] = resolved
        if class_qualname in graph.classes:
            graph.classes[class_qualname].bases = resolved


def _resolve(graph: CallGraph) -> None:
    """Turn the raw dotted call names into qualified project functions."""
    for fn in graph.functions.values():
        module_imports = graph.imports.get(fn.module, {})

        for bucket_name in ("calls", "calls_in_atomic", "calls_deferred"):
            raw = getattr(fn, bucket_name)
            resolved: set[str] = set()

            for dotted in raw:
                target = _resolve_one(graph, fn, module_imports, dotted)
                if target is not None:
                    resolved.add(target)
                else:
                    fn.unresolved.add(dotted)
                    graph.unresolved_calls += 1

            setattr(fn, bucket_name, resolved)


def _resolve_one(
    graph: CallGraph,
    fn: Function,
    module_imports: dict[str, str],
    dotted: str,
) -> str | None:
    head, _, rest = dotted.partition(".")

    # self.method() and cls.method(), through the declared bases.
    if head in {"self", "cls"} and rest and fn.class_name:
        return graph.resolve_method(fn.class_name, rest.split(".")[0])

    # A name imported directly: `from .services import finalise`.
    if not rest and head in module_imports:
        candidate = module_imports[head]
        return candidate if candidate in graph.functions else None

    # A module imported and then attributed: `from . import services`.
    if rest and head in module_imports:
        candidate = f"{module_imports[head]}.{rest}"
        if candidate in graph.functions:
            return candidate
        # `import app.services` binds `app`, so the full path is already there.
        if dotted in graph.functions:
            return dotted
        return None

    # Defined in this same module.
    same_module = f"{fn.module}.{dotted}" if fn.module else dotted
    if same_module in graph.functions:
        return same_module

    if dotted in graph.functions:
        return dotted

    return None


# One graph per tree, reused across checks in the same process.
#
# Seven analyses build a call graph, and `check` runs all of them: on a project
# of 2100 files that is seven full parses of the same unchanged source, and it
# dominated the runtime of every aggregate command. The graph is pure a
# function of the files, so building it more than once buys nothing.
_cache: dict[str, tuple[tuple[int, float, int], CallGraph]] = {}


def clear_cache() -> None:
    """Forget every cached graph. For tests, and for a long-lived server."""
    _cache.clear()


def build(root: Path, *, refresh: bool = False) -> CallGraph:
    """The call graph for `root`, built once and reused while the files match.

    Args:
        root: the tree to read.
        refresh: rebuild even if the fingerprint is unchanged.
    """
    key = str(root)
    mark = fingerprint(root)
    if not refresh:
        cached = _cache.get(key)
        if cached is not None and cached[0] == mark:
            return cached[1]
    graph = _build(root)
    _cache[key] = (mark, graph)
    return graph


def _build(root: Path) -> CallGraph:
    """Read every Python file under `root` and link the calls up."""
    graph = CallGraph(functions={}, imports={}, bases={}, class_module={})

    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue

        graph.files_scanned += 1
        module = _module_name(path, root)
        collector = _Collector(module, str(path.relative_to(root)))
        collector.visit(tree)

        graph.imports[module] = collector.imports
        graph.bases.update(collector.bases)
        graph.class_module.update(collector.class_modules)
        for cls in collector.classes:
            graph.classes[cls.qualname] = cls
        for fn in collector.functions:
            # A duplicate qualname means two defs with the same name in one
            # module; the later one wins at import time, so it wins here.
            graph.functions[fn.qualname] = fn

    _resolve_bases(graph)
    _resolve(graph)
    return graph


def transaction_entry_points(graph: CallGraph) -> list[str]:
    """Functions that open a transaction themselves."""
    return sorted(fn.qualname for fn in graph.functions.values() if fn.opens_atomic)


def inside_transaction(graph: CallGraph) -> dict[str, list[str]]:
    """Functions that run inside a transaction opened by somebody else.

    The value is the call path that put them there, which is the whole point:
    a finding in `services.py` is unactionable without the caller that made it
    a problem.
    """
    return graph.reachable(transaction_entry_points(graph), transaction_aware=True)


def find_dispatch_wrappers(
    graph: CallGraph,
    dispatching: Callable[[str], bool],
) -> dict[str, list[str]]:
    """Project functions that exist to dispatch a task, found rather than configured.

    A codebase that wraps Celery in `notify(user, template)` defeats a check
    that matches on `.delay`. But the wrapper still calls `.delay` somewhere,
    so it can be found by looking instead of by asking.
    """
    wrappers: dict[str, list[str]] = {}
    for fn in graph.functions.values():
        hits = sorted(name for name in fn.unresolved if dispatching(name))
        if hits:
            wrappers[fn.qualname] = hits
    return wrappers


def summary(graph: CallGraph) -> dict[str, Any]:
    resolved = sum(
        len(f.calls) + len(f.calls_in_atomic) + len(f.calls_deferred)
        for f in graph.functions.values()
    )
    return {
        "files_scanned": graph.files_scanned,
        "functions": len(graph.functions),
        "classes": len(graph.bases),
        "resolved_calls": resolved,
        "unresolved_calls": graph.unresolved_calls,
        "note": (
            "Unresolved calls are not failures. Most of them are the standard "
            "library, third-party packages and the ORM, none of which are "
            "project functions and none of which need to be in here. The number "
            "is reported so that a resolution rate falling off a cliff is "
            "visible rather than quietly making every check built on this "
            "less complete."
        ),
    }
