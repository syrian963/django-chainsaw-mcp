# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Which findings does a request actually hit?

Every other check here answers "where is this defect". On a large codebase that
produces a few hundred entries sorted by severity, and the list is correct and
nobody knows where to start. Severity ranks the defect. It does not rank the
risk, because risk is severity times how often the code runs, and nothing in
the list says whether a line sits on the path of an endpoint served ten
thousand times an hour or in a management command last run in 2023.

So this walks the other way. It takes the merged findings, maps each one to the
function that contains it, and walks the call graph **backwards** to the entry
points that reach it - HTTP routes, Celery tasks, signal receivers, management
commands. The output is per entry point:

    POST /orders (shop.views.OrderViewSet.create)
      3 findings, worst: critical
        critical  a query in a function the loop calls   shop/services.py:88
        high      side effect inside a transaction       shop/services.py:94

That is a sentence a team can act on: this endpoint, these defects, in this
order. Two hundred scattered findings become twelve endpoints that carry them,
and the endpoint carrying eleven of them is where the afternoon goes.

## Backwards, not forwards

Reachability from every entry point forwards gives the same answer and costs a
full traversal per entry point. Walking back from the few hundred functions
that actually contain a finding is bounded by the findings rather than by the
project, and produces the path as a by-product.

## What "no entry point" means here

A finding nothing reaches is reported in its own bucket, named for what it is:
**not reached from any entry point this can see**. It is not "dead code" and it
is certainly not "safe". A plain Django function view wired up in a URLconf
carries no decorator and belongs to no view class, so nothing in the source
marks it as an entry point and its findings land here. Calling those
unreachable would be a confident wrong answer, which is worse than an
incomplete one.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .callgraph import CallGraph, Function, build
from .project import TreeCache, resolve_root

# Decorator suffixes that make a function an entry point, and what kind.
_TASK_DECORATORS = {"shared_task", "periodic_task", "task"}
_ROUTE_METHODS = {
    "get", "post", "put", "patch", "delete", "head", "options",
    "route", "api_route", "websocket",
}
# A class whose name ends in one of these is a web view, and its HTTP methods
# are entry points. Matched on the name because the base class often lives in a
# library the graph never parsed.
_VIEW_SUFFIXES = ("View", "ViewSet", "APIView", "Resource", "Handler")
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
# DRF routes these onto a ViewSet without any decorator at all.
_DRF_ACTIONS = {"list", "create", "retrieve", "update", "partial_update", "destroy"}
# Methods the framework calls itself. Nothing in the project calls
# `get_queryset`, so without these a finding inside one is reached by no
# caller and lands in `unattributed`, which reads as "probably fine" and is
# exactly wrong for a method that runs on every single request.
#
# The list is deliberately closed rather than "every method on a view class".
# Making a private helper an entry point would stop the backward walk at the
# helper and hide the action that actually serves the request.
_FRAMEWORK_HOOKS = {
    "dispatch", "setup", "get_queryset", "get_object", "get_serializer_class",
    "get_serializer_context", "get_permissions", "get_authenticators",
    "get_throttles", "filter_queryset", "paginate_queryset",
    "perform_create", "perform_update", "perform_destroy",
    "get_context_data", "form_valid", "form_invalid", "get_success_url",
    "run",
}

_SEVERITY = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _last(dotted: str) -> str:
    return dotted.rsplit(".", 1)[-1]


def _is_view_class(graph: CallGraph, class_qualname: str | None) -> bool:
    if class_qualname is None:
        return False
    for ancestor in graph.ancestors(class_qualname):
        if _last(ancestor).endswith(_VIEW_SUFFIXES):
            return True
    return False


def _classify(graph: CallGraph, fn: Function) -> tuple[str, str] | None:
    """(kind, label) if this function is an entry point, else None."""
    for dotted in fn.decorators:
        last = _last(dotted)
        if last in _TASK_DECORATORS and (last != "task" or "." in dotted):
            return "task", f"task {fn.qualname}"
        if last == "receiver":
            return "signal", f"signal receiver {fn.qualname}"
        if last == "api_view":
            return "http", f"{fn.qualname} (@api_view)"
        if last == "action":
            return "http", f"{fn.qualname} (@action)"
        # `@app.get("/x")`, `@router.post(...)`, `@app.route(...)`: an
        # attribute call on something that is not self. A bare `@get` is not
        # matched, because `get` alone is far too common a name to guess with.
        if "." in dotted and last in _ROUTE_METHODS:
            owner = dotted.rsplit(".", 1)[0]
            if _last(owner) not in {"self", "cls", "super"}:
                return "http", f"{last.upper()} via {dotted} -> {fn.qualname}"

    if fn.name in _HTTP_METHODS | _DRF_ACTIONS | _FRAMEWORK_HOOKS and _is_view_class(
        graph, fn.class_name
    ):
        return "http", f"{fn.name} on {fn.class_name}"

    if "management/commands/" in fn.file.replace("\\", "/") and fn.name == "handle":
        return "command", f"manage.py {Path(fn.file).stem}"

    return None


def _routed(graph: CallGraph) -> dict[str, dict[str, Any]]:
    """What the URLconf says, which is the only place some views are named.

    A plain function view carries no decorator and belongs to no class.
    Nothing in its source says it serves requests, so a check that reads only
    the source reports every defect on its path as reached by nothing - which
    is the one wrong answer this whole file exists to avoid.

    The URLconf also supplies the URL, and "GET /orders/<id>/confirm/" is a
    better thing to hand somebody than a dotted Python path.
    """
    try:
        from django.urls import get_resolver
    except Exception:
        return {}

    try:
        resolver = get_resolver()
        patterns = resolver.url_patterns
    except Exception:
        return {}

    found: dict[str, dict[str, Any]] = {}

    def label_for(callback: Any) -> str | None:
        holder = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
        target = holder if holder is not None else callback
        module = getattr(target, "__module__", None)
        name = getattr(target, "__qualname__", None)
        if not module or not name:
            return None
        return f"{module}.{name}"

    def walk(entries: Any, prefix: str, depth: int) -> None:
        if depth > 20:
            return
        for entry in entries:
            route = f"{prefix}{entry.pattern}"
            nested = getattr(entry, "url_patterns", None)
            if nested is not None:
                walk(nested, route, depth + 1)
                continue
            dotted = label_for(getattr(entry, "callback", None))
            if dotted is None:
                continue
            # Router patterns are regexes. `^reports/$` is the same URL as
            # `/reports/` and only one of them is worth showing a human.
            plain = route.replace("^", "").replace("$", "").replace(r"\.", ".")
            url = plain if plain.startswith("/") else f"/{plain}"
            if dotted in graph.functions:
                found.setdefault(dotted, {"url": url, "target": dotted})
            else:
                # A class: every entry-point method on it serves this URL.
                for qualname in graph.functions:
                    if qualname.startswith(f"{dotted}."):
                        found.setdefault(qualname, {"url": url, "target": dotted})
                found.setdefault(dotted, {"url": url, "target": dotted})

    try:
        walk(patterns, "", 0)
    except Exception:
        return found
    return found


def entry_points(graph: CallGraph) -> dict[str, dict[str, Any]]:
    """Every function the outside world can start executing at."""
    routed = _routed(graph)
    found: dict[str, dict[str, Any]] = {}
    for qualname, fn in graph.functions.items():
        verdict = _classify(graph, fn)
        url = routed.get(qualname, {}).get("url")
        if verdict is None:
            # A URL that names this function directly is a plain function
            # view: nothing else in the source marks it, and the URLconf is
            # the only thing that knows.
            #
            # A URL that names the *class* is not a licence to promote every
            # method on it. `_internal` on a routed ViewSet would become an
            # entry point, the backward walk would stop there, and the action
            # that actually serves the request would be hidden - which is why
            # the hook list is closed in the first place.
            if routed.get(qualname, {}).get("target") != qualname:
                continue
            verdict = ("http", f"{qualname} (routed)")
        kind, label = verdict
        found[qualname] = {
            "kind": kind,
            "label": f"{url} -> {label}" if url else label,
            "url": url,
            "file": fn.file,
            "line": fn.line,
        }
    return found


def _called_by_name(graph: CallGraph) -> dict[str, set[str]]:
    """Bare method name -> the functions that call something by that name.

    Half the calls in a large project cannot be resolved to a definition:
    `order.copy()` names a method, and which class `order` is cannot be
    decided without type inference. Those are not evidence of nothing calling
    the method - they are evidence of exactly the opposite, and reporting
    "nothing calls it" when 65 sites call something with that name is a
    confident wrong answer.

    Resolving them by name was measured before it was rejected: on a project
    of 11300 functions a unique-name rule would have resolved 8% of the
    unresolved calls and attributed 25 more findings, for the price of
    attributing some of them to the wrong endpoint. Reporting the ambiguity is
    worth more than guessing at it.
    """
    out: dict[str, set[str]] = {}
    for qualname, fn in graph.functions.items():
        for dotted in fn.unresolved:
            out.setdefault(dotted.rsplit(".", 1)[-1], set()).add(qualname)
    return out


def _callers(graph: CallGraph) -> dict[str, set[str]]:
    """callee -> the functions that call it."""
    reverse: dict[str, set[str]] = {}
    for qualname, fn in graph.functions.items():
        for callee in fn.calls | fn.calls_in_atomic | fn.calls_deferred:
            reverse.setdefault(callee, set()).add(qualname)
    return reverse


def _by_file(graph: CallGraph) -> dict[str, list[Function]]:
    index: dict[str, list[Function]] = {}
    for fn in graph.functions.values():
        index.setdefault(fn.file, []).append(fn)
    return index


def _containing(index: dict[str, list[Function]], relative: str, line: int) -> str | None:
    """The innermost function whose body contains this line."""
    best: str | None = None
    best_span: int | None = None
    for fn in index.get(relative, ()):
        if not (fn.line <= line <= fn.end_line):
            continue
        span = fn.end_line - fn.line
        if best_span is None or span < best_span:
            best, best_span = fn.qualname, span
    return best


def _reaching(
    reverse: dict[str, set[str]],
    start: str,
    entries: dict[str, dict[str, Any]],
    max_depth: int,
) -> dict[str, list[str]]:
    """Entry points that reach `start`, each with the path it takes to get there."""
    if start in entries:
        return {start: [start]}

    hits: dict[str, list[str]] = {}
    seen = {start}
    frontier: list[tuple[str, list[str]]] = [(start, [start])]
    for _ in range(max_depth):
        following: list[tuple[str, list[str]]] = []
        for current, path in frontier:
            for caller in sorted(reverse.get(current, ())):
                if caller in seen:
                    continue
                seen.add(caller)
                upward = [caller, *path]
                if caller in entries:
                    hits.setdefault(caller, upward)
                    # An entry point is where the walk stops. Continuing past
                    # it would attribute the finding to whatever else happens
                    # to call the view, which is not a caller in any sense
                    # that matters to a request.
                    continue
                following.append((caller, upward))
        if not following:
            break
        frontier = following
    return hits


def _serializer_uses(
    graph: CallGraph, trees: TreeCache, wanted: set[str]
) -> dict[str, list[str]]:
    """Serializer class -> the functions whose body names it.

    DRF's declarative `serializer_class` is one way to serve a serializer and
    not the common one on a large codebase: a plain `APIView` builds the
    serializer in the method body, and there is no attribute to read. That
    left every serializer finding on such a project unattributed - 161 of
    them, the largest single group.

    The name in the body is resolved through the module's own imports, which
    the call graph already records, so `Foo` in two modules meaning two
    different classes stays two different classes. A bare-name match would
    have been much easier and would have attributed findings to the wrong
    endpoint, which is the failure mode this whole file is built to avoid.
    """
    if not wanted:
        return {}

    short: dict[str, set[str]] = {}
    for qualname in wanted:
        short.setdefault(qualname.rsplit(".", 1)[-1], set()).add(qualname)

    uses: dict[str, list[str]] = {}
    for qualname, fn in graph.functions.items():
        node = trees.function(fn)
        if node is None:
            continue
        imports = graph.imports.get(fn.module, {})
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                name = child.id
            elif isinstance(child, ast.Attribute):
                name = child.attr
            else:
                continue
            candidates = short.get(name)
            if not candidates:
                continue
            resolved = imports.get(name) or f"{fn.module}.{name}"
            if resolved in candidates:
                uses.setdefault(resolved, []).append(qualname)
    return {key: sorted(set(value)) for key, value in uses.items()}


def _class_uses(
    graph: CallGraph, trees: TreeCache, wanted: set[str]
) -> dict[str, list[str]]:
    """Serializer class -> the classes whose body names it.

    A nested serializer is a field on its parent:

        class OrderSerializer(ModelSerializer):
            customer = CustomerSerializer()

    That reference is in a class body, not inside any function, so neither the
    backward walk nor the "built in a method body" route can see it. The chain
    that matters is nested -> parent -> the view serving the parent, and it can
    be several links long.
    """
    if not wanted:
        return {}

    short: dict[str, set[str]] = {}
    for qualname in wanted:
        short.setdefault(qualname.rsplit(".", 1)[-1], set()).add(qualname)

    bodies: dict[str, list[str]] = {}
    for relative in sorted({fn.file for fn in graph.functions.values()}
                           | {cls.file for cls in graph.classes.values()}):
        tree = trees.module(relative)
        if tree is None:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            holder = graph.class_at(relative, node.lineno)
            if holder is None:
                continue
            imports = graph.imports.get(graph.class_module.get(holder, ""), {})
            for child in ast.walk(node):
                # A name inside a method belongs to the method, which the
                # function route already covers.
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if isinstance(child, ast.Name):
                    name = child.id
                elif isinstance(child, ast.Attribute):
                    name = child.attr
                else:
                    continue
                candidates = short.get(name)
                if not candidates:
                    continue
                resolved = imports.get(name) or f"{graph.class_module.get(holder, '')}.{name}"
                if resolved in candidates and resolved != holder:
                    bodies.setdefault(resolved, []).append(holder)
    return {key: sorted(set(value)) for key, value in bodies.items()}


def _serializer_map(root: Path) -> tuple[dict[str, list[str]], str | None]:
    """Serializer class -> the views that declare it, or {} if DRF is absent.

    A serializer field is not inside any function, so the backward walk has
    nothing to start from and every N+1 in a serializer lands in
    `unattributed`. It is served by a view, though, and DRF records which -
    which is the same question this file asks about a function, asked about a
    class instead.
    """
    try:
        from .discovery import serializers_used_by_views

        return serializers_used_by_views(root), None
    except Exception as exc:
        # An empty map and a map that could not be built look identical from
        # the outside, and one of them means "nothing to attribute" while the
        # other means "could not look". Saying which is the difference between
        # a result and a guess.
        return {}, f"{type(exc).__name__}: {exc}"


def _entries_on(entries: dict[str, dict[str, Any]], view: str) -> list[str]:
    return [q for q in entries if q.startswith(f"{view}.")]


def _location(finding: dict[str, Any]) -> tuple[str, int] | None:
    """file and line, from either the structured fields or `location`."""
    if finding.get("file") and finding.get("line"):
        return str(finding["file"]), int(finding["line"])
    raw = finding.get("location") or ""
    if ":" in raw:
        head, _, tail = raw.rpartition(":")
        if tail.isdigit() and head.endswith(".py"):
            return head, int(tail)
    return None


def impact(
    report: dict[str, Any] | None = None,
    search_path: str | None = None,
    max_depth: int = 8,
    tenant_root: str = "auth.User",
) -> dict[str, Any]:
    """Group the merged findings by the entry points that reach them.

    Args:
        report: an existing `check.run_all()` report. One is run if not given.
        search_path: the tree to build the call graph from.
        max_depth: how many callers to walk back through.
        tenant_root: passed through when this runs the checks itself.
    """
    root = resolve_root(search_path)
    graph = build(root)

    if report is None:
        from .check import run_all

        report = run_all(tenant_root=tenant_root)

    entries = entry_points(graph)
    reverse = _callers(graph)
    by_name = _called_by_name(graph)
    index = _by_file(graph)
    serving, serving_error = _serializer_map(root)
    owners = {
        graph.class_at(*where)
        for where in (
            _location(f) for f in report.get("findings", [])
        )
        if where is not None
    }
    named = {o for o in owners if o}
    trees = TreeCache(root)
    uses = _serializer_uses(graph, trees, named)
    class_uses = _class_uses(graph, trees, named)

    by_entry: dict[str, dict[str, Any]] = {}
    declarative: dict[str, dict[str, Any]] = {}
    unattributed: list[dict[str, Any]] = []
    no_location: list[dict[str, Any]] = []
    holders: dict[tuple[str, int], str | None] = {}

    for finding in report.get("findings", []):
        where = _location(finding)
        if where is None:
            no_location.append(finding)
            continue
        relative, line = where
        if where not in holders:
            holders[where] = _containing(index, relative, line)
        holder = holders[where]
        if holder is None:
            hits = {}
            owner = graph.class_at(relative, line)
            # A nested serializer is served through its parent, which may
            # itself be nested. Follow that chain before giving up.
            # Each step carries the nesting it came through, so the path
            # reads parent -> nested rather than stopping at the parent and
            # leaving the reader to find the field themselves.
            chain = [(owner, [owner])] if owner else []
            walked = {owner} if owner else set()
            while chain:
                current, nesting = chain.pop(0)
                # A function that builds the class is a caller like any other,
                # so the ordinary backward walk applies from there.
                for user in uses.get(current, ()):
                    for entry, path in _reaching(
                        reverse, user, entries, max_depth
                    ).items():
                        hits.setdefault(entry, [*path, *reversed(nesting)])
                for parent in class_uses.get(current, ()):
                    if parent not in walked:
                        walked.add(parent)
                        chain.append((parent, [*nesting, parent]))
            for view in serving.get(owner or "", ()):
                for entry in _entries_on(entries, view):
                    hits[entry] = [entry, owner]
                if not _entries_on(entries, view):
                    # A ViewSet that declares `serializer_class` and overrides
                    # nothing is still an endpoint. There is no function to
                    # point at, so the class is the honest answer.
                    declarative.setdefault(view, {
                        "kind": "http", "label": f"{view} (declares the serializer)",
                        "file": "", "line": 0, "entry": view, "findings": [],
                    })
                    hits[view] = [view, owner]
            if not hits:
                unattributed.append(
                    {**finding, "reason": "no function contains this line"}
                )
                continue
            for entry, path in hits.items():
                slot = by_entry.get(entry) or declarative.get(entry)
                if slot is None:
                    slot = by_entry.setdefault(
                        entry, {**entries[entry], "entry": entry, "findings": []}
                    )
                by_entry.setdefault(entry, slot)
                slot["findings"].append({
                    "check": finding.get("check"),
                    "severity": finding.get("severity"),
                    "title": finding.get("title"),
                    "location": f"{relative}:{line}",
                    "through": path,
                })
            continue

        hits = _reaching(reverse, holder, entries, max_depth)
        if not hits:
            fn = graph.functions[holder]
            sites = by_name.get(fn.name, ())
            if sites:
                sharing = sum(
                    1 for other in graph.functions.values() if other.name == fn.name
                )
                reason = (
                    f"{len(sites)} call site(s) name .{fn.name}(), but which "
                    "object they are called on cannot be decided without type "
                    "inference"
                    + (f"; {sharing} functions in the project share that name"
                       if sharing > 1 else "")
                )
            else:
                reason = f"nothing in the project calls {holder}"
            unattributed.append({**finding, "reason": reason})
            continue

        for entry, path in hits.items():
            slot = by_entry.setdefault(
                entry, {**entries[entry], "entry": entry, "findings": []}
            )
            slot["findings"].append({
                "check": finding.get("check"),
                "severity": finding.get("severity"),
                "title": finding.get("title"),
                "location": f"{relative}:{line}",
                "through": path,
            })

    ranked = sorted(
        by_entry.values(),
        key=lambda e: (
            min(_SEVERITY.get(f["severity"], 9) for f in e["findings"]),
            -len(e["findings"]),
            e["entry"],
        ),
    )
    for entry in ranked:
        entry["findings"].sort(
            key=lambda f: (_SEVERITY.get(f["severity"], 9), f["location"])
        )
        entry["finding_count"] = len(entry["findings"])
        entry["worst"] = entry["findings"][0]["severity"]

    kinds: dict[str, int] = {}
    for meta in entries.values():
        kinds[meta["kind"]] = kinds.get(meta["kind"], 0) + 1

    return {
        "entry_points_found": len(entries),
        "entry_points_from_the_urlconf": sum(
            1 for meta in entries.values() if meta.get("url")
        ),
        "serializers_declared_by_a_view": len(serving),
        "serializer_map_error": serving_error,
        "classes_built_inside_a_function": len(uses),
        "classes_nested_in_another_class": len(class_uses),
        "entry_points_by_kind": dict(sorted(kinds.items())),
        "entry_points_with_findings": len(ranked),
        "entry_points": ranked,
        "findings_considered": len(report.get("findings", [])),
        "unattributed_count": len(unattributed),
        "unattributed_because_the_receiver_is_unknown": sum(
            1 for f in unattributed if "cannot be decided" in f["reason"]
        ),
        "unattributed": unattributed,
        "without_a_location_count": len(no_location),
        "max_depth": max_depth,
        "note": (
            "Findings grouped by the entry points that reach them, so the "
            "question becomes which endpoint to fix rather than which line. "
            "Walked backwards from each finding through the call graph, at "
            f"most {max_depth} callers deep. `unattributed` means no entry "
            "point this can see reaches the finding - not that it is "
            "unreachable, and not that it is safe. On a Django project the "
            "URLconf is read, so plain function views are found; without one "
            "- a non-Django project, or a settings module that could not be "
            "loaded - a view with no decorator and no view class is invisible "
            "and its findings land there. A finding whose holder is a "
            "method that something calls by name, on an object that cannot be "
            "identified without type inference, is counted in "
            "`unattributed_because_the_receiver_is_unknown`: that one is not "
            "unreached, it is unresolved, and the two are different answers. "
            "Findings with no file and line at all, such as a migration or a "
            "template, are counted separately and cannot be attributed."
        ),
    }
