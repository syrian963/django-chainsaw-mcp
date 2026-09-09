# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""A prefetch that was paid for and then thrown away.

`prefetch_related` fills a cache on each parent object, and the related
manager hands that cache back - but only to the accessors that can use it.
Ask the manager anything the cache cannot answer and it goes back to the
database, once per parent, with the prefetch query already paid for on top.

    orders = Order.objects.prefetch_related("lines")     # one extra query
    for order in orders:
        for line in order.lines.filter(active=True):     # and one per order
            ...

That is worse than never prefetching at all: the same N+1, plus a query and
the memory to hold every line on the page. It reads as if it were optimised,
which is exactly why it survives review.

Measured against Django 6.1, on a related manager whose parent was prefetched:

    .filter() .exclude() .order_by() .first() .last()      one query per parent
    .only() .defer() .distinct() .values() .values_list()  one query per parent
    .select_related() .annotate() .reverse()               one query per parent

    .count() .exists() .all() .all()[0] .all()[:2]         read the cache

`.count()` and `.exists()` are in the second group, which is not obvious and
is why they are not reported: since Django 4.1 the related manager answers
both from the prefetched result. Only the first group is flagged, and every
method in it re-queries on every Django version, so a finding here does not
depend on which one the project runs.

## What exists

`nplusone` finds the neighbouring problem at runtime - a relation eagerly
loaded and then never touched - by watching a request go past. It needs the
code path to run, so it sees what the tests happen to exercise.
`unused_eager_loading` in this repository answers that same question
statically for DRF viewsets.

Neither answers this one. Here the prefetch *is* used; it is reached through
an accessor that cannot read it. Both halves sit in the same function, so no
request has to run to see it.

## When this stays quiet

The prefetch site and the use site have to be provably the same object: a name
bound to a prefetched queryset in the same scope, or the loop variable
iterating one. A prefetch in a view and the accessor in a template tag two
files away is real and is not reported, because "these are the same objects"
would be a guess, and a wrong guess here tells somebody to delete a line that
is load-bearing.

That was not a free choice. Matching on the relation name anywhere in the same
file found 79 sites across nine large projects; five were read and four of the
five were coincidence - the same relation name, unrelated objects. The
scope-local rule finds 7 in the same nine projects, and all 7 were read and
confirmed. Saleor's is the one to look at: a comment saying "get cached
variant with related fields", and two lines later a `.values_list()` that
throws the cache away.
"""

from __future__ import annotations

import ast
from typing import Any

from .project import parse_file, resolve_root

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

# Each of these re-queries when it is asked of a prefetched related manager.
# The list is short on purpose: it holds only what was measured, so a finding
# is a fact rather than a suspicion.
_DEFEATS_THE_CACHE = frozenset({
    "filter", "exclude", "order_by", "first", "last", "only", "defer",
    "values", "values_list", "distinct", "select_related", "annotate",
    "reverse",
})

# `obj.lines.all().filter(...)` is `obj.lines.filter(...)` with a step in
# between, and `.all()` on its own is the accessor that reads the cache.
_TRANSPARENT = "all"


def _dotted(node: ast.AST) -> str | None:
    """`self.object` and `orders` as text; None for anything else.

    A base this cannot name is a base it cannot match against a prefetch, and
    reporting one would be reporting a guess.
    """
    parts: list[str] = []
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    return ".".join(reversed(parts))


def _prefetched_relations(node: ast.AST) -> set[str]:
    """Relations this expression prefetches, by the name the accessor uses.

    `prefetch_related("lines__product")` fills the cache for `lines` as well,
    so the first segment is what an accessor can read back.

    `Prefetch("lines", to_attr="active")` is the exception: the cache lands on
    a new attribute and `obj.lines` is left doing what it always did, so it is
    not treated as prefetched.
    """
    relations: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if not isinstance(func, ast.Attribute) or func.attr != "prefetch_related":
            continue
        for argument in sub.args:
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                relations.add(argument.value.split("__")[0])
            elif (
                isinstance(argument, ast.Call)
                and isinstance(argument.func, ast.Name)
                and argument.func.id == "Prefetch"
                and argument.args
                and isinstance(argument.args[0], ast.Constant)
                and isinstance(argument.args[0].value, str)
                and not any(keyword.arg == "to_attr" for keyword in argument.keywords)
            ):
                relations.add(argument.args[0].value.split("__")[0])
    return relations


class _Bindings:
    """What each name carried, and from which line.

    Bindings are kept in source order rather than collapsed into one set per
    name, so a name that is prefetched and then reassigned to a plain queryset
    stops being prefetched from that line on. Without that, the second half of
    a function inherits a claim from the first half that is no longer true.
    """

    def __init__(self) -> None:
        """An empty scope: no name carries anything yet."""
        self._by_name: dict[str, list[tuple[int, frozenset[str], bool]]] = {}

    def bind(self, name: str, line: int, relations: set[str], per_row: bool) -> None:
        """Record what `name` holds from this line on.

        An empty `relations` is a binding too - it is what ends an earlier one.
        """
        self._by_name.setdefault(name, []).append(
            (line, frozenset(relations), per_row)
        )

    def at(self, name: str, line: int) -> tuple[frozenset[str], bool]:
        """What `name` held at this line: the newest binding at or above it."""
        held: tuple[frozenset[str], bool] = (frozenset(), False)
        for bound_line, relations, per_row in self._by_name.get(name, ()):
            if bound_line <= line:
                held = (relations, per_row)
        return held

    def __bool__(self) -> bool:
        """True when this scope binds anything at all, prefetched or not."""
        return bool(self._by_name)

    def carried(self, name: str) -> bool:
        """Is this a name the scope binds, rather than one it only reads?"""
        return name in self._by_name


_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _walk_scope(scope: ast.AST):
    """Every node in this scope, stopping at the next one down.

    Walking the module body into the functions it contains was worth one wrong
    answer on Saleor: a name bound in `generate_fulfillment_lines_payload`
    matched an accessor a hundred lines earlier in a different function. Each
    function is scanned as a scope of its own, so nothing is lost except a
    name a nested function closes over - which is the trade this takes,
    because a missed finding costs less than a confident wrong one.
    """
    stack = [scope]
    while stack:
        current = stack.pop()
        if current is not scope:
            yield current
        for child in ast.iter_child_nodes(current):
            if isinstance(child, _NESTED_SCOPES):
                continue
            stack.append(child)


def _binding_events(scope: ast.AST) -> list[tuple[int, int, str, ast.AST, bool]]:
    """Every name binding in this scope, as `(line, rank, name, value, per_row)`.

    `rank` orders two bindings written on the same line. A comprehension is
    the case that needs it:

        users = [(u, u.replies.filter(...)) for u in users]

    The comprehension iterates the *old* `users`, and only then is the name
    rebound. Ranking the loop variable ahead of the assignment on that line is
    what makes the read see the value that was actually iterated.

    A comprehension has no line number of its own, so it takes the one from
    the expression that holds it - which is where the reader sees it too.
    """
    events: list[tuple[int, int, str, ast.AST, bool]] = []

    def loop_targets(target: ast.AST, iterable: ast.AST, line: int) -> None:
        """Record the loop variable, if it is a name this can write down."""
        name = _dotted(target)
        if name is not None:
            events.append((line, 0, name, iterable, True))

    for node in _walk_scope(scope):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            for generator in node.generators:
                loop_targets(generator.target, generator.iter, node.lineno)
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            loop_targets(node.target, node.iter, node.lineno)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            if node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                name = _dotted(target)
                if name is not None:
                    events.append((node.lineno, 1, name, node.value, False))

    events.sort(key=lambda event: (event[0], event[1]))
    return events


def _collect(scope: ast.AST) -> _Bindings:
    """Names in this scope that hold, or iterate, a prefetched queryset.

    Applied in source order, so a name reassigned halfway down the function
    stops carrying what the first half gave it.
    """
    bindings = _Bindings()

    for line, _rank, name, value, per_row in _binding_events(scope):
        relations = _prefetched_relations(value)
        if not relations and isinstance(value, (ast.Name, ast.Attribute)):
            source = _dotted(value)
            if source is not None and bindings.carried(source):
                relations = set(bindings.at(source, line)[0])
        if per_row and not relations:
            # A loop over something unprefetched says nothing about the loop
            # variable; it is not a rebinding of an outer name either.
            continue
        # Bound with nothing prefetched is still a binding: it is what ends an
        # earlier one.
        bindings.bind(name, line, relations, per_row)

    return bindings


def _defeats(node: ast.Call) -> tuple[str, str, str] | None:
    """`(base, relation, method)` when this call re-queries a related manager."""
    outer = node.func
    if not isinstance(outer, ast.Attribute) or outer.attr not in _DEFEATS_THE_CACHE:
        return None
    accessor = outer.value
    if (
        isinstance(accessor, ast.Call)
        and isinstance(accessor.func, ast.Attribute)
        and accessor.func.attr == _TRANSPARENT
        and not accessor.args
    ):
        accessor = accessor.func.value
    if not isinstance(accessor, ast.Attribute):
        return None
    base = _dotted(accessor.value)
    if base is None:
        return None
    return base, accessor.attr, outer.attr


def _scan_scope(scope: ast.AST) -> list[dict[str, Any]]:
    """Calls in this scope that re-query a relation the scope prefetched."""
    bindings = _collect(scope)
    if not bindings:
        return []

    found: list[dict[str, Any]] = []
    for node in _walk_scope(scope):
        if not isinstance(node, ast.Call):
            continue
        defeat = _defeats(node)
        if defeat is None:
            continue
        base, relation, method = defeat
        relations, per_row = bindings.at(base, node.lineno)
        if relation not in relations:
            continue
        found.append({
            "line": node.lineno,
            "variable": base,
            "relation": relation,
            "method": method,
            "per_row": per_row,
        })
    return found


def defeated_prefetches(
    search_path: str | None = None,
    include_tests: bool = False,
) -> dict[str, Any]:
    """Relations that were prefetched and then re-queried anyway.

    Args:
        search_path: directory to scan. Defaults to the configured project.
        include_tests: also report inside test files, where a query per row
            costs a slow suite rather than a slow request.
    """
    root = resolve_root(search_path)

    findings: list[dict[str, Any]] = []
    files_scanned = 0
    prefetch_sites_scanned = 0

    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        is_test = "tests" in path.parts or path.name.startswith("test_")
        if is_test and not include_tests:
            continue
        try:
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue
        files_scanned += 1

        sites = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "prefetch_related"
        )
        if not sites:
            continue
        prefetch_sites_scanned += sites

        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            lines = []
        relative = str(path.relative_to(root))

        # A nested function is walked twice - once inside its parent's scope,
        # where a closed-over name is still in view, and once as a scope of its
        # own. The same call must not be reported twice for that.
        seen: set[int] = set()
        scopes: list[ast.AST] = [tree]
        scopes += [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for scope in scopes:
            for hit in _scan_scope(scope):
                if hit["line"] in seen:
                    continue
                seen.add(hit["line"])
                per_row = hit["per_row"]
                findings.append({
                    "file": relative,
                    "line": hit["line"],
                    "code": (lines[hit["line"] - 1].strip()[:140]
                             if hit["line"] <= len(lines) else ""),
                    "variable": hit["variable"],
                    "relation": hit["relation"],
                    "call": f"{hit['variable']}.{hit['relation']}.{hit['method']}()",
                    "in_test": is_test,
                    "severity": "high" if per_row else "medium",
                    "why": (
                        f".{hit['method']}() cannot be answered from the "
                        f"prefetch cache, so the related manager queries again"
                        + (
                            " - once per row of the queryset being iterated, "
                            "with the prefetch query already paid for on top"
                            if per_row else
                            ". The prefetch on this queryset is bought and "
                            "never read"
                        )
                    ),
                    "fix": (
                        f"move the condition into the prefetch with "
                        f"Prefetch(\"{hit['relation']}\", queryset=..., "
                        f"to_attr=\"...\") and read that attribute, or drop the "
                        f"prefetch if {hit['relation']} is only ever reached "
                        f"through .{hit['method']}()"
                        if hit["method"] in {"filter", "exclude", "only", "defer",
                                             "select_related", "annotate"}
                        else
                        f"read the prefetched rows and do the .{hit['method']}() "
                        f"in Python over {hit['variable']}.{hit['relation']}.all(), "
                        f"or drop the prefetch if it is never read as it stands"
                    ),
                })

    findings.sort(key=lambda f: (0 if f["severity"] == "high" else 1,
                                 f["file"], f["line"]))
    per_row_count = sum(1 for f in findings if f["severity"] == "high")

    return {
        "search_path": str(root),
        "files_scanned": files_scanned,
        "prefetch_sites_scanned": prefetch_sites_scanned,
        "finding_count": len(findings),
        "per_row_count": per_row_count,
        "wasted_only_count": len(findings) - per_row_count,
        "findings": findings,
        "note": (
            "A prefetched related manager answers .count(), .exists(), .all() "
            "and a slice from its cache. Anything else - .filter(), "
            ".order_by(), .first(), .values_list() and the rest - goes back to "
            "the database, once per parent object, on top of the prefetch "
            "query that was already run. Measured on Django 6.1; every method "
            "reported here re-queries on earlier versions too.\n\n"
            "Reported only where the prefetch and the accessor are provably "
            "the same object: a name bound in the same scope, or the loop "
            "variable iterating it. A prefetch in a view and an accessor in a "
            "template tag is the same defect and is not reported, because the "
            "fix is to delete or rewrite a line and a guess is not a good "
            "enough reason to suggest that.\n\n"
            "nplusone finds the neighbouring problem - eagerly loaded and "
            "never touched - at runtime. unused_eager_loading answers that one "
            "statically for DRF viewsets. Here the prefetch is used, through "
            "an accessor that cannot read it."
        ),
    }
