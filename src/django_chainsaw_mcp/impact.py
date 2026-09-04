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

from pathlib import Path
from typing import Any

from .callgraph import CallGraph, Function, build
from .project import resolve_root

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


def entry_points(graph: CallGraph) -> dict[str, dict[str, Any]]:
    """Every function the outside world can start executing at."""
    found: dict[str, dict[str, Any]] = {}
    for qualname, fn in graph.functions.items():
        verdict = _classify(graph, fn)
        if verdict is None:
            continue
        kind, label = verdict
        found[qualname] = {
            "kind": kind,
            "label": label,
            "file": fn.file,
            "line": fn.line,
        }
    return found


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
    index = _by_file(graph)

    by_entry: dict[str, dict[str, Any]] = {}
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
            unattributed.append({**finding, "reason": "no function contains this line"})
            continue

        hits = _reaching(reverse, holder, entries, max_depth)
        if not hits:
            unattributed.append({**finding, "reason": f"nothing calls {holder}"})
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
        "entry_points_by_kind": dict(sorted(kinds.items())),
        "entry_points_with_findings": len(ranked),
        "entry_points": ranked,
        "findings_considered": len(report.get("findings", [])),
        "unattributed_count": len(unattributed),
        "unattributed": unattributed,
        "without_a_location_count": len(no_location),
        "max_depth": max_depth,
        "note": (
            "Findings grouped by the entry points that reach them, so the "
            "question becomes which endpoint to fix rather than which line. "
            "Walked backwards from each finding through the call graph, at "
            f"most {max_depth} callers deep. `unattributed` means no entry "
            "point this can see reaches the finding - not that it is "
            "unreachable, and not that it is safe: a plain Django function "
            "view carries no decorator and belongs to no view class, so "
            "nothing in the source marks it as an entry point. Findings "
            "with no file and line, such as a migration or a serializer "
            "class, are counted separately and cannot be attributed at all."
        ),
    }
