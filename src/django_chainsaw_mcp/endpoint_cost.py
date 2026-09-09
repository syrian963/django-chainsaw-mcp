# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""How many queries one request to this endpoint will cost.

Every tool that answers this runs the application: the debug toolbar, silk,
django-showmequeries, a test asserting `assertNumQueries`. All of them need
traffic, and all of them tell you afterwards.

The number is derivable before anything runs. A list endpoint costs one query
for the page, plus one per object for every serializer field that crosses a
relation the view did not prefetch, plus that again for each level of nesting.
Everything in that sentence is visible in the source: the view names its
serializer, the serializer names its fields, the model graph says which fields
are relations, and the view's queryset says what was already optimised.

So this multiplies them out and gives a number:

    OrderViewSet          page 50    1 + 50 + 50 + 2500  =  2601 queries
        order.customer       not in select_related      +50
        order.lines          not in prefetch_related    +50
        line.product         nested, per line           +2500

The value is not precision. It is that 2601 is a number somebody can react to
before a customer does, and that the same view with the right two calls on its
queryset costs 4.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

from .discovery import load_serializer_modules, load_view_modules
from .django_env import ensure_django
from .serializer_nplusone import serializer_nplusone

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}


class _QuerysetOptimisation(ast.NodeVisitor):
    """What a view's queryset already prefetches.

    Reads both the `queryset = ...` class attribute and any `get_queryset`
    body, because a project uses one or the other and rarely both.
    """

    def __init__(self) -> None:
        self.select_related: set[str] = set()
        self.prefetch_related: set[str] = set()
        self.select_related_all = False
        self.dynamic = False
        # only() and defer() change the columns rather than the query count,
        # right up until something touches a column that was left out. Then it
        # is one query per object, which is the same shape as an N+1 and is
        # harder to spot because the code looks optimised.
        self.only: set[str] = set()
        self.defer: set[str] = set()
        # Prefetch(..., to_attr="x") loads the relation under another name.
        # The path is known, the read is not, so these are kept apart.
        self.renamed_prefetches: set[str] = set()

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"only", "defer"}:
            target = self.only if func.attr == "only" else self.defer
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    target.add(argument.value)
                else:
                    self.dynamic = True

        if isinstance(func, ast.Attribute) and func.attr in {"select_related", "prefetch_related"}:
            target = self.select_related if func.attr == "select_related" else self.prefetch_related
            if not node.args and func.attr == "select_related":
                # select_related() with no arguments follows every non-null FK.
                self.select_related_all = True
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    target.add(argument.value)
                    continue

                path, renamed = _prefetch_object(argument)
                if path is None:
                    # A path built at runtime, so the real set is unknown.
                    self.dynamic = True
                elif renamed:
                    # `to_attr` moves the result to a different attribute, so
                    # the serializer reads it under that name and nothing
                    # matching the relation path proves anything either way.
                    self.renamed_prefetches.add(path)
                else:
                    target.add(path)
        self.generic_visit(node)


# Calls that put a query on the wire. `values` and `values_list` are here
# because they are terminal in practice, and `count`/`exists` because they are
# the ones people reach for believing they are free.
_QUERY_CALLS = {
    "all", "filter", "exclude", "get", "first", "last", "count", "exists",
    "aggregate", "annotate", "values", "values_list", "latest", "earliest",
    "get_or_create", "update_or_create", "in_bulk", "iterator",
}


class _MethodBody(ast.NodeVisitor):
    """Queries and caching inside one method."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.cached = False

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in _QUERY_CALLS:
                # `.all()` on a prefetched related manager is free, and that is
                # not decidable here, so this is an upper bound and says so.
                self.queries.append(func.attr)
            if func.attr in {"get_or_set", "cache_get", "get_many"}:
                self.cached = True
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in {"cached_property", "cache"}:
            self.cached = True
        self.generic_visit(node)


def _method_field_queries(serializer_cls: Any, field_name: str) -> dict[str, Any] | None:
    """What `get_<field>()` costs, per object.

    Previously this was reported as an unknown and the endpoint was marked
    `at least`. An unknown at the top of a list endpoint is the difference
    between 2 queries and 2000, so it is worth reading the method.
    """
    method = getattr(serializer_cls, f"get_{field_name}", None)
    if method is None:
        return None
    try:
        source, _ = inspect.getsourcelines(method)
        tree = ast.parse(textwrap.dedent("".join(source)))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return {"queries": None, "cached": False, "readable": False}

    body = _MethodBody()
    body.visit(tree)
    return {
        "queries": len(body.queries),
        "calls": sorted(set(body.queries)),
        "cached": body.cached,
        "readable": True,
    }


def _pagination_for(cls: Any, assumed: int) -> dict[str, Any]:
    """How many rows one list response actually returns, and why.

    The first version assumed `page_size` rows for every list endpoint. A view
    with no pagination_class in a project with no DEFAULT_PAGINATION_CLASS
    returns the whole table, and the number 50 was a fiction dressed up as an
    input. The real page size is on the view or in the settings, and "none"
    is an answer that has to be reported as such.
    """
    paginator = getattr(cls, "pagination_class", None)
    if paginator is None:
        return {
            "paginated": False,
            "page_size": None,
            "source": "no pagination_class on the view and no DEFAULT_PAGINATION_CLASS",
            "rows": "every row in the table",
            "assumed_for_estimate": assumed,
        }
    size = getattr(paginator, "page_size", None)
    source = f"{paginator.__name__}.page_size"
    if size is None:
        from rest_framework.settings import api_settings

        size = api_settings.PAGE_SIZE
        source = f"{paginator.__name__} via REST_FRAMEWORK['PAGE_SIZE']"
    if size is None:
        # A paginator with no page size paginates nothing.
        return {
            "paginated": False,
            "page_size": None,
            "source": f"{paginator.__name__} with no page_size anywhere",
            "rows": "every row in the table",
            "assumed_for_estimate": assumed,
        }
    return {
        "paginated": True,
        "page_size": int(size),
        "source": source,
        "rows": f"{size} per page",
        "assumed_for_estimate": int(size),
    }


def _view_classes() -> list[Any]:
    """Every DRF view class Python has imported."""
    try:
        from rest_framework.generics import GenericAPIView
    except ModuleNotFoundError:
        return []

    seen: set[int] = set()
    found: list[Any] = []

    def walk(cls: Any) -> None:
        for subclass in cls.__subclasses__():
            if id(subclass) in seen:
                continue
            seen.add(id(subclass))
            found.append(subclass)
            walk(subclass)

    walk(GenericAPIView)
    return found


def _prefetch_object(node: ast.AST) -> tuple[str | None, bool]:
    """`Prefetch("lines", queryset=...)` -> ("lines", False).

    A Prefetch object still names its path as a literal first argument; only
    the rows it loads are customised. Treating the whole queryset as unknown
    because one of these appeared made every view that uses them invisible,
    and they are the normal way to prefetch anything filtered.

    The second value is True for `to_attr`, which puts the result somewhere
    else entirely.
    """
    if not isinstance(node, ast.Call):
        return None, False
    name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
    if name != "Prefetch" or not node.args:
        return None, False
    first = node.args[0]
    if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
        return None, False
    renamed = any(k.arg == "to_attr" for k in node.keywords)
    return first.value, renamed


def _optimisations(cls: Any) -> _QuerysetOptimisation:
    visitor = _QuerysetOptimisation()
    try:
        source = inspect.getsource(cls)
        visitor.visit(ast.parse(source.lstrip()))
    except (OSError, TypeError, SyntaxError, IndentationError):
        try:
            import textwrap

            visitor.visit(ast.parse(textwrap.dedent(inspect.getsource(cls))))
        except Exception:
            visitor.dynamic = True
    return visitor


def _covered(lookup: str, opt: _QuerysetOptimisation) -> str | None:
    """Is this relation path already optimised, and by which call?"""
    for path in opt.select_related:
        if lookup == path or path.startswith(f"{lookup}__") or lookup.startswith(f"{path}__"):
            return "select_related"
    for path in opt.prefetch_related:
        if lookup == path or path.startswith(f"{lookup}__") or lookup.startswith(f"{path}__"):
            return "prefetch_related"
    return None


def _location(cls: Any) -> str | None:
    try:
        source_file = inspect.getsourcefile(cls)
        _, line = inspect.getsourcelines(cls)
    except (OSError, TypeError):
        return None
    return f"{Path(source_file).name}:{line}" if source_file else None


def endpoint_cost(
    page_size: int = 50,
    nested_fan_out: int = 5,
    list_only: bool = False,
) -> dict[str, Any]:
    """Estimate the queries one request costs, per DRF view.

    Args:
        page_size: how many objects a list response returns.
        nested_fan_out: assumed children per parent one level down. The tool
            cannot know how many lines an order has, and using page_size for
            every level turns a three level nesting into page_size cubed,
            which is arithmetic rather than an estimate.
        list_only: skip views that only ever return a single object.
    """
    config = ensure_django()
    # Django imports models.py, admin.py and whatever the URLconf
    # reaches. A serializer outside that is real, served, and was
    # previously invisible to a __subclasses__() walk.
    discovered = load_serializer_modules(config.project_path)
    # And the views, or a project whose URLconf does not reach them at
    # analysis time reports zero endpoints, which reads as a clean result.
    load_view_modules(config.project_path)

    views = _view_classes()
    if not views:
        return {
            "rest_framework_installed": False,
            "endpoints": [],
            "views_seen": 0,
            "note": (
                "No DRF view classes are importable. If the project uses "
                "DRF, the server is running in the wrong environment; see "
                "docs/usage.md."
            ),
        }

    serializer_report = serializer_nplusone()
    crossings: dict[str, list[dict[str, Any]]] = {}
    for finding in serializer_report.get("findings", []):
        if finding.get("relation"):
            # Grouping on via.split(".")[0] gave the package name, so every
            # nested crossing was filed under "shop" and silently dropped.
            root = finding.get("root_serializer") or finding["serializer"]
            crossings.setdefault(root, []).append(finding)

    endpoints: list[dict[str, Any]] = []
    unpaginated: list[str] = []
    # A project whose views build their responses by hand has nothing here to
    # estimate, and "0 endpoints" reads as a clean result rather than as an
    # empty one. Counting what was looked at is the difference.
    views_seen = 0
    views_without_serializer = 0

    for cls in views:
        views_seen += 1
        serializer_cls = getattr(cls, "serializer_class", None)
        if serializer_cls is None:
            views_without_serializer += 1
            continue

        serializer_name = f"{serializer_cls.__module__}.{serializer_cls.__qualname__}"
        is_list = any(
            hasattr(cls, attr) for attr in ("list", "get_queryset")
        ) and "Detail" not in cls.__name__
        if list_only and not is_list:
            continue

        pagination = _pagination_for(cls, page_size) if is_list else None
        objects = pagination["assumed_for_estimate"] if is_list else 1
        opt = _optimisations(cls)

        base = 1
        breakdown: list[dict[str, Any]] = [
            {"reason": "the page itself", "queries": base, "detail": "one query for the queryset"}
        ]
        total = base
        unbounded = False

        if is_list and pagination["paginated"]:
            base += 1
            total += 1
            breakdown.append(
                {"reason": "pagination count", "queries": 1, "detail": "COUNT(*) for the page count"}
            )
        elif is_list:
            # Every per-object term below is multiplied by page_size as a
            # stated assumption, but the real multiplier is the row count of
            # the table, which nothing here can know. So the total is a floor.
            unbounded = True
            unpaginated.append(f"{cls.__module__}.{cls.__qualname__}")
            breakdown.append(
                {
                    "reason": f"no pagination: every row, not {page_size}",
                    "queries": None,
                    "detail": (
                        f"{pagination['source']}. The per-object terms below assume "
                        f"{page_size} rows; the response actually carries the whole "
                        "table, so this estimate is a floor that grows with the data"
                    ),
                }
            )

        for finding in crossings.get(serializer_name, []):
            lookup = finding["lookup"]
            covered_by = _covered(lookup, opt)
            depth = finding.get("depth", 0)

            if covered_by:
                breakdown.append(
                    {
                        "reason": f"{lookup} (already {covered_by})",
                        "queries": 0,
                        "detail": "optimised by the view's queryset",
                    }
                )
                continue

            if opt.select_related_all and not finding["relation"]["many"]:
                breakdown.append(
                    {
                        "reason": f"{lookup} (select_related() with no arguments)",
                        "queries": 0,
                        "detail": "covered by the unrestricted call",
                    }
                )
                continue

            # Level 0 runs once per object on the page. Each level below that
            # runs once per child, and the number of children is a property of
            # the data, not of the code, so it is a parameter with a stated
            # default rather than a guess dressed up as a measurement.
            multiplier = objects * (nested_fan_out ** depth)
            queries = multiplier
            total += queries
            breakdown.append(
                {
                    "reason": lookup,
                    "queries": queries,
                    "detail": (
                        f"{finding['relation']['kind']} crossed per object"
                        + (f", nested {depth} level(s) deep" if depth else "")
                        + f"; add {finding['relation']['hint']}('{lookup}')"
                    ),
                }
            )

        method_fields = [
            f for f in serializer_report.get("findings", [])
            if f["serializer"] == serializer_name and f.get("type") == "SerializerMethodField"
        ]
        # Read the method rather than shrugging at it. An unknown at the top of
        # a list endpoint is the difference between 2 queries and 2000.
        method_field_detail: list[dict[str, Any]] = []
        for field in method_fields:
            cost = _method_field_queries(serializer_cls, field["field"])
            if cost is None:
                continue
            entry = {"field": field["field"], **cost}
            method_field_detail.append(entry)

            if not cost["readable"]:
                unbounded = True
                breakdown.append({
                    "reason": f"get_{field['field']}() could not be read",
                    "queries": None,
                    "detail": "source unavailable, so the real total is higher than this",
                })
                continue

            if cost["cached"]:
                breakdown.append({
                    "reason": f"get_{field['field']}() touches a cache",
                    "queries": 0,
                    "detail": (
                        "the query count depends on the hit rate, which no amount "
                        "of reading the code reveals"
                    ),
                })
                continue

            if cost["queries"]:
                per_object = cost["queries"] * objects
                total += per_object
                breakdown.append({
                    "reason": f"get_{field['field']}() -> {', '.join(cost['calls'])}",
                    "queries": per_object,
                    "detail": (
                        f"{cost['queries']} query call(s) in the method body, once "
                        f"per object. An upper bound: .all() on an already "
                        f"prefetched manager costs nothing, and that is not "
                        f"decidable from here"
                    ),
                })

        # only() and defer() look like optimisation and can be the opposite.
        # A field the serializer renders that only() left behind is fetched
        # one row at a time, which is an N+1 wearing a performance hat.
        if opt.only:
            # Only concrete columns count. A SerializerMethodField is not
            # something only() could have fetched, and a relation is a
            # different problem with a different fix, so naming either here
            # would be a fabricated finding dressed up as a measurement.
            model = getattr(getattr(serializer_cls, "Meta", None), "model", None)
            concrete = {
                f.name for f in model._meta.get_fields()
                if getattr(f, "concrete", False) and not f.is_relation
            } if model is not None else set()
            # Ask the serializer what it renders. The N+1 report only lists
            # fields that cross a relation, so reading `rendered` off it would
            # miss every plain column - which is exactly what only() drops.
            try:
                rendered = set(serializer_cls().get_fields())
            except Exception:
                rendered = set()
            missed = sorted((rendered & concrete) - opt.only - opt.defer - {"id", "pk"})
            if missed:
                cost = len(missed) * objects
                total += cost
                breakdown.append({
                    "reason": f"only() omits {', '.join(missed)}",
                    "queries": cost,
                    "detail": (
                        "the serializer renders these and only() did not fetch "
                        "them, so each one is loaded per object on access"
                    ),
                })

        endpoints.append(
            {
                "view": f"{cls.__module__}.{cls.__qualname__}",
                "location": _location(cls),
                "serializer": serializer_name,
                "kind": "list" if is_list else "detail",
                "objects_assumed": objects,
                "pagination": pagination,
                "estimated_queries": total,
                "at_least": unbounded,
                "method_fields": method_field_detail,
                "queryset_optimises": {
                    "select_related": sorted(opt.select_related),
                    "prefetch_related": sorted(opt.prefetch_related),
                    "select_related_all": opt.select_related_all,
                    "only": sorted(opt.only),
                    "defer": sorted(opt.defer),
                    "renamed_prefetches": sorted(opt.renamed_prefetches),
                    "dynamic": opt.dynamic,
                },
                "breakdown": breakdown,
                "severity": (
                    "high" if total > objects
                    else "medium" if total > 2
                    else "low"
                ),
            }
        )

    endpoints.sort(key=lambda e: -e["estimated_queries"])
    worst = endpoints[0] if endpoints else None

    return {
        "rest_framework_installed": True,
        "page_size": page_size,
        "nested_fan_out": nested_fan_out,
        "endpoint_count": len(endpoints),
        "views_seen": views_seen,
        "views_without_serializer_class": views_without_serializer,
        "unpaginated_list_endpoints": sorted(unpaginated),
        "worst": worst["view"] if worst else None,
        "worst_estimate": worst["estimated_queries"] if worst else 0,
        "endpoints": endpoints,
        "note": (
            "An estimate, and deliberately a rough one. It assumes every "
            "serializer field is rendered, that a paginated list returns its "
            "real page size and an unpaginated one returns page_size as a stated "
            "floor - it actually returns every row in the table, which is a "
            "finding in itself. An unpaginated list endpoint returns "
            "objects, and that a relation not named in select_related or "
            "prefetch_related costs one query per object. It cannot see inside "
            "a SerializerMethodField, cannot follow a queryset built at "
            "runtime, and does not know about caching. The point is not the "
            "exact figure: it is that a four figure number and a single digit "
            "one are different kinds of endpoint, and which one you have is "
            "knowable before anybody sends a request. Nested levels assume "
            f"{nested_fan_out} child objects per parent, which is a property of "
            "your data that no amount of reading the code reveals. The ratio "
            "between an optimised and an unoptimised view is reliable; the "
            "absolute number is only as good as that assumption."
            # The case that actually happens on a large codebase: plenty of
            # views, almost none of them declaring a serializer_class.
            # Without this sentence a near-empty result reads as a clean one.
            + (
                f"\n\n{views_without_serializer} of the {views_seen} view(s) found declare no "
                "serializer_class. This estimate works from a view's serializer "
                "and its queryset, so a view that builds its response by hand "
                "is invisible here: for those an empty result means 'could not "
                "look', not 'nothing to find'."
                if views_without_serializer else ""
            )
            + (
                f"\n\n{len(discovered['failed'])} module(s) declaring a serializer could "
                "not be imported, so nothing in them was read: "
                + ", ".join(entry["module"] for entry in discovered["failed"][:5])
                if discovered["failed"] else ""
            )
        ),
    }
