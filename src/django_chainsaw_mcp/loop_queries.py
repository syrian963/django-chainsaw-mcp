# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Queries written inside a loop.

The template and serializer checks here find the N+1 that a *framework*
causes: a relation crossed while rendering. This one finds the N+1 somebody
wrote by hand, in a view or a service, which is where it lives in a codebase
whose views build their responses themselves rather than handing a queryset to
a serializer.

Three different things wear the same shape, and they have three different
fixes, which is the whole reason to separate them:

    for order in orders:
        customer = Customer.objects.get(pk=order.customer_id)   # N+1

The query depends on the loop variable, so it runs once per row. Fetch them
all at once before the loop, or let the ORM do it with `select_related`.

    for order in orders:
        config = Config.objects.get(key="vat")                  # invariant

The query does not depend on the loop at all. It is the same question, asked N
times, for the same answer. Move it above the loop; there is nothing clever to
do here and nothing to trade off.

    for order in orders:
        order.status = "closed"
        order.save()                                            # N writes

N round trips, each in its own transaction unless something wraps them.
`bulk_update` fixes it and skips the signals, which is its own decision -
`bypassed_effects` is the check for that side of it.

## What exists

`django-check` does static N+1 detection for relation access inside a loop,
as an LSP and a CLI, and it is the closest thing to this. `nplusone` and the
debug toolbar find it at runtime, once the code path has run.

What is added here is the separation above. "There is a query in this loop" is
one finding; "this query does not use the loop variable and should be three
lines higher" is a different one, with a fix nobody has to think about.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .project import TreeCache, parse_file, read_source, resolve_root

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

# Calls that put a query on the wire when they are reached.
_READS = {"get", "first", "last", "latest", "earliest", "count", "exists",
          "aggregate", "values", "values_list", "all", "filter", "exclude",
          "get_object_or_404", "in_bulk"}
_WRITES = {"save", "delete", "create", "update", "get_or_create",
           "update_or_create", "add", "remove", "set"}

# Managers and querysets are reached through these.
_ORM_HINTS = ("objects", "_default_manager", "_base_manager")

# A loop over one of these is not a loop over rows.
_CHEAP_ITERABLES = (ast.List, ast.Tuple, ast.Set, ast.Dict)


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Name):
            parts.append(current.id)
            break
        else:
            break
    return ".".join(reversed(parts))


def _names_used(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _loop_targets(node: ast.For) -> set[str]:
    return {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}


class _LoopScan(ast.NodeVisitor):
    """Queries inside loops, with the loop variables they do or do not use."""

    def __init__(self, model_names: set[str]) -> None:
        self.model_names = model_names
        self.findings: list[dict[str, Any]] = []
        # Calls made inside a loop that are not themselves ORM calls. One of
        # them may be a project function that queries, which is the shape this
        # check could not see: the loop is here and the query is one file away.
        self.calls_in_loops: list[dict[str, Any]] = []
        self._loops: list[dict[str, Any]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # A nested def runs when it is called, not here.
        if self._loops:
            return
        self.generic_visit(node)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _enter_loop(self, node: Any) -> None:
        cheap = isinstance(node.iter, _CHEAP_ITERABLES)

        # The iterable is evaluated once, before the first iteration, so a
        # query there is one query and not N. Visiting it inside the loop
        # context reported `for o in Order.objects.all():` as a query in a
        # loop, which is the one line in the whole pattern that is fine.
        self.visit(node.iter)

        # `for x in qs.iterator()` still iterates rows; the iterator only
        # changes how they are fetched, not how many queries the body causes.
        self._loops.append({
            "targets": _loop_targets(node),
            "line": node.lineno,
            "cheap": cheap,
            "source": _dotted(node.iter) or "<expression>",
        })
        for child in node.body + node.orelse:
            self.visit(child)
        self._loops.pop()

    visit_For = _enter_loop  # type: ignore[assignment]
    visit_AsyncFor = _enter_loop  # type: ignore[assignment]

    def _enter_comprehension(self, node: Any) -> None:
        """`[enrich(o) for o in orders]` is a loop with different syntax.

        Missing it meant the most idiomatic way to write the N+1 was the one
        shape this check could not see.
        """
        generators = node.generators
        targets: set[str] = set()
        cheap = True
        for generator in generators:
            targets |= {n.id for n in ast.walk(generator.target) if isinstance(n, ast.Name)}
            if not isinstance(generator.iter, _CHEAP_ITERABLES):
                cheap = False
        if generators:
            # Same reason as a for loop: the outermost iterable runs once.
            # A later generator's iterable depends on the earlier target and
            # does run per row, so it stays inside.
            self.visit(generators[0].iter)

        self._loops.append({
            "targets": targets,
            "line": node.lineno,
            "cheap": cheap,
            "source": _dotted(generators[0].iter) if generators else "<expression>",
        })
        for index, generator in enumerate(generators):
            if index:
                self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for field in ("elt", "key", "value"):
            part = getattr(node, field, None)
            if part is not None:
                self.visit(part)
        self._loops.pop()

    visit_ListComp = _enter_comprehension  # type: ignore[assignment]
    visit_SetComp = _enter_comprehension  # type: ignore[assignment]
    visit_DictComp = _enter_comprehension  # type: ignore[assignment]
    visit_GeneratorExp = _enter_comprehension  # type: ignore[assignment]

    def visit_Call(self, node: ast.Call) -> None:
        if self._loops:
            self._classify(node)
        self.generic_visit(node)

    def _classify(self, node: ast.Call) -> None:
        if not isinstance(node.func, ast.Attribute):
            # get_object_or_404(Model, ...) is a plain name call.
            name = getattr(node.func, "id", "")
            if name != "get_object_or_404":
                self._record_plain_call(node)
                return
            kind, method = "read", name
        else:
            method = node.func.attr
            if method in _READS:
                kind = "read"
            elif method in _WRITES:
                kind = "write"
            else:
                return

            chain = _dotted(node.func)
            lowered = chain.lower()
            head = chain.split(".")[0] if chain else ""
            # A manager, a known model, or an attribute on the loop variable.
            # `[].count(x)` and `some_dict.get(k)` must not qualify.
            touches_orm = (
                any(hint in lowered for hint in _ORM_HINTS)
                or head in self.model_names
                or (kind == "write" and method in {"save", "delete"})
            )
            if not touches_orm:
                self._record_plain_call(node)
                return

        innermost = self._loops[-1]
        if innermost["cheap"] and kind == "read":
            # A loop over a literal list of two things is not the problem.
            return

        used = _names_used(node)
        all_targets: set[str] = set()
        for loop in self._loops:
            all_targets |= loop["targets"]
        depends = bool(used & all_targets)

        depth = len([loop for loop in self._loops if not loop["cheap"]])

        self.findings.append({
            "line": node.lineno,
            "kind": kind,
            "call": _dotted(node.func) or method,
            "depends_on_loop": depends,
            "loop_line": innermost["line"],
            "loop_depth": depth,
            "loop_source": innermost["source"],
        })

    def _record_plain_call(self, node: ast.Call) -> None:
        """A call inside a loop that is not itself a query."""
        if not self._loops:
            return
        innermost = self._loops[-1]
        if innermost["cheap"]:
            return
        name = _dotted(node.func)
        if not name:
            return
        all_targets: set[str] = set()
        for loop in self._loops:
            all_targets |= loop["targets"]
        self.calls_in_loops.append({
            "line": node.lineno,
            "call": name,
            "depends_on_loop": bool(_names_used(node) & all_targets),
            "loop_line": innermost["line"],
            "loop_depth": len([lp for lp in self._loops if not lp["cheap"]]),
            "loop_source": innermost["source"],
        })


def _dedupe_chain(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One finding per query expression, not one per link in the chain.

    `Invoice.objects.filter(order_id=pk).first()` is two qualifying calls and
    one query. Reporting both doubled every chained lookup, so the longest
    chain on a line wins - it is also the most informative one to print.
    """
    best: dict[tuple[int, str, str], dict[str, Any]] = {}
    for finding in findings:
        root = finding["call"].split(".")[0] if finding["call"] else ""
        key = (finding["line"], root, finding["kind"])
        current = best.get(key)
        if current is None or len(finding["call"]) > len(current["call"]):
            best[key] = finding
    return sorted(best.values(), key=lambda f: f["line"])


def _resolve_callee(graph: Any, relative: str, dotted: str) -> str | None:
    """The project function a call inside a loop refers to, if any."""
    module = relative.removesuffix(".py").replace("/", ".")
    for candidate in (f"{module}.{dotted}", dotted):
        if candidate in graph.functions:
            return candidate
    imports = graph.imports.get(module, {})
    head, _, rest = dotted.partition(".")
    target = imports.get(head)
    if target is None:
        return None
    full = f"{target}.{rest}" if rest else target
    return full if full in graph.functions else None


def _model_class_names() -> set[str]:
    try:
        from django.apps import apps

        return {model.__name__ for model in apps.get_models()}
    except Exception:
        return set()


def _querying_functions(
    root: Path,
    graph: Any,
    model_names: set[str],
) -> dict[str, dict[str, Any]]:
    """Project functions that query, whether or not the query is in a loop.

    A loop calling `enrich(order)` is an N+1 when `enrich` queries, and
    neither function looks wrong alone. This is the same shape the transaction
    check follows across modules, and the call graph already knows the edge.
    """
    trees = TreeCache(root)
    querying: dict[str, dict[str, Any]] = {}

    for fn in graph.functions.values():
        node = trees.function(fn)
        if node is None:
            continue
        scan = _LoopScan(model_names)
        # Pretend the whole body is inside a loop, so every ORM call in it is
        # classified rather than only the ones the author already looped over.
        scan._loops.append({"targets": set(), "line": fn.line,
                            "cheap": False, "source": "<body>"})
        for child in node.body:
            scan.visit(child)
        scan._loops.pop()

        reads = [f for f in scan.findings if f["kind"] == "read"]
        if reads:
            querying[fn.qualname] = {
                "file": fn.file,
                "line": reads[0]["line"],
                "call": reads[0]["call"],
            }
    return querying


def _reaches_query(
    graph: Any,
    start: str,
    querying: dict[str, dict[str, Any]],
    max_depth: int,
) -> tuple[str, list[str]] | None:
    """The nearest querying function this one reaches, with the path to it.

    Including itself. `for o in orders: enrich(o)` where `enrich` queries is
    the whole point, and only looking at what `enrich` calls missed every
    single case of it.
    """
    if start in querying:
        return start, [start]
    seen = {start}
    queue: list[tuple[str, list[str]]] = [(start, [start])]
    while queue:
        current, path = queue.pop(0)
        if len(path) > max_depth:
            continue
        fn = graph.functions.get(current)
        if fn is None:
            continue
        for callee in sorted(fn.calls | fn.calls_in_atomic | fn.calls_deferred):
            if callee in seen:
                continue
            seen.add(callee)
            next_path = [*path, callee]
            if callee in querying:
                return callee, next_path
            queue.append((callee, next_path))
    return None


def queries_in_loops(
    search_path: str | None = None,
    include_writes: bool = True,
    follow_calls: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Database work written inside a loop, split by what the fix is.

    Args:
        search_path: directory to scan. Defaults to the configured project.
        include_writes: also report save()/delete() inside a loop.
        follow_calls: also report a loop that calls a function which queries.
        max_depth: how many calls deep to follow.
    """
    root = resolve_root(search_path)
    model_names = _model_class_names()

    per_row: list[dict[str, Any]] = []
    invariant: list[dict[str, Any]] = []
    writes: list[dict[str, Any]] = []
    through_call: list[dict[str, Any]] = []
    files_scanned = 0

    graph = None
    querying: dict[str, dict[str, Any]] = {}
    if follow_calls:
        from . import callgraph

        graph = callgraph.build(root)
        querying = _querying_functions(root, graph, model_names)

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if "test" in path.name or "tests" in path.parts:
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue
        files_scanned += 1
        lines = source.splitlines()
        relative = str(path.relative_to(root))

        scan = _LoopScan(model_names)
        scan.visit(tree)

        if graph is not None:
            for call in scan.calls_in_loops:
                target = _resolve_callee(graph, relative, call["call"])
                if target is None:
                    continue
                reached = _reaches_query(graph, target, querying, max_depth)
                if reached is None:
                    continue
                name, path = reached
                info = querying[name]
                through_call.append({
                    "file": relative,
                    "line": call["line"],
                    "code": (lines[call["line"] - 1].strip()[:140]
                             if call["line"] <= len(lines) else ""),
                    "call": call["call"],
                    "queries_in": name,
                    "query": info["call"],
                    "query_at": f"{info['file']}:{info['line']}",
                    # The path ends at the query itself, not at the function
                    # holding it: "enrich" alone leaves the reader to go and
                    # find out what enrich actually does.
                    "reached_through": [*path, info["call"]],
                    "loop_at_line": call["loop_line"],
                    "loop_depth": call["loop_depth"],
                    "loop_over": call["loop_source"],
                    "severity": "critical" if call["loop_depth"] > 1 else "high",
                    "why": (
                        f"this call reaches {name}, which queries"
                        + ("" if len(path) == 1
                           else f", {len(path) - 1} call(s) further on")
                        + ". Nothing in the loop body looks like a query, so "
                          "this one is easy to miss"
                    ),
                    "fix": (
                        "fetch what the callee needs before the loop and pass it "
                        "in, or give it a bulk form that takes the whole list"
                    ),
                })

        for finding in _dedupe_chain(scan.findings):
            entry = {
                "file": relative,
                "line": finding["line"],
                "code": (lines[finding["line"] - 1].strip()[:140]
                         if finding["line"] <= len(lines) else ""),
                "call": finding["call"],
                "loop_at_line": finding["loop_line"],
                "loop_depth": finding["loop_depth"],
                "loop_over": finding["loop_source"],
            }
            nested = finding["loop_depth"] > 1

            if finding["kind"] == "write":
                if not include_writes:
                    continue
                entry.update({
                    "severity": "medium",
                    "why": (
                        "one round trip per iteration, each in its own "
                        "transaction unless something wraps the loop"
                        + (", and this loop is nested inside another" if nested else "")
                    ),
                    "fix": (
                        "collect the objects and use bulk_update, which is faster "
                        "and skips the signals - see bypassed_effects for what "
                        "that costs"
                    ),
                })
                writes.append(entry)
            elif finding["depends_on_loop"]:
                entry.update({
                    "severity": "high" if not nested else "critical",
                    "why": (
                        "this query uses the loop variable, so it runs once per "
                        "row"
                        + (". The loop is nested, so it runs once per row of the "
                           "outer loop as well" if nested else "")
                    ),
                    "fix": (
                        "fetch them all before the loop and index by key, or let "
                        "the ORM do it with select_related / prefetch_related on "
                        "the queryset being iterated"
                    ),
                })
                per_row.append(entry)
            else:
                entry.update({
                    "severity": "medium",
                    "why": (
                        "this query does not use the loop variable, so it asks "
                        "the same question every iteration and gets the same "
                        "answer"
                    ),
                    "fix": "move it above the loop",
                })
                invariant.append(entry)

    order = {"critical": 0, "high": 1, "medium": 2}
    for bucket in (per_row, invariant, writes, through_call):
        bucket.sort(key=lambda f: (order.get(f["severity"], 9), f["file"], f["line"]))

    return {
        "search_path": str(root),
        "files_scanned": files_scanned,
        "per_row_count": len(per_row),
        "loop_invariant_count": len(invariant),
        "write_count": len(writes),
        "through_a_call_count": len(through_call),
        "finding_count": len(per_row) + len(invariant) + len(writes) + len(through_call),
        "high_severity_count": sum(
            1 for f in per_row + through_call if f["severity"] in {"high", "critical"}
        ),
        "per_row": per_row,
        "loop_invariant": invariant,
        "writes_in_loops": writes,
        "through_a_call": through_call,
        "note": (
            "Three shapes share one appearance and have three different fixes. "
            "A query that uses the loop variable runs once per row and needs a "
            "bulk fetch or a prefetch. A query that does not use it asks the "
            "same question every iteration for the same answer and simply "
            "belongs above the loop - there is nothing to trade off there. A "
            "write in a loop is N round trips, and the bulk fix skips signals, "
            "which bypassed_effects is the check for.\n\n"
            "A loop that calls a function which queries is the same N+1 with "
            "nothing in the loop body to see, and it is reported with the path "
            "that reaches the query. That is the shape a codebase organised "
            "into services actually has.\n\n"
            "django-check does static N+1 detection for relation access in a "
            "loop and is the closest existing tool; nplusone and the debug "
            "toolbar find it at runtime once the path has run. The separation "
            "above is what is added.\n\n"
            "A loop over a literal list is not treated as a loop over rows for "
            "reads. Tests are skipped, because a query per row in a test "
            "fixture is not a production problem. A queryset reached through a "
            "name this cannot resolve to a model still counts when it is "
            "clearly a manager call, and is missed when it is not."
        ),
    }
