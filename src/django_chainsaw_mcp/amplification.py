# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Endpoints anyone can call that cost a great deal to answer.

Two facts, each of which is somebody else's finding:

    GET /orders has no authentication.
    GET /orders issues about 2852 queries per request.

The first is an authorisation question and, on a public catalogue, the right
answer. The second is a performance question and, behind a login, an
engineering backlog item.

Together they are a different thing entirely: **one HTTP request, from anyone,
costs the database roughly three thousand queries.** A single client at ten
requests a second is thirty thousand queries a second, from a laptop, with no
credentials, against an endpoint that returns HTTP 200 every time. Nothing in
the logs looks like an attack.

This is the amplification factor, and neither check finds it alone, because
neither is wrong on its own. The security scanners that look for it are DAST
tools: they need the service running, reachable and populated with enough rows
for the cost to show up. All of it is derivable from the source.

## The unbounded list is the other half

    GET /customers has no authentication.
    GET /customers has no pagination.

That one is not a load problem. It is the whole table, to anyone, in one
request - a slower and much quieter way to lose a database than an injection.

## What the number is worth

The query estimate carries a stated assumption about how many child rows a
parent has, so the absolute figure is only as good as that. What survives every
choice of assumption is the **ratio**: an endpoint costing four figures next to
one costing two is a different kind of thing, and it is reachable without a
password.
"""

from __future__ import annotations

from typing import Any

from .project import get_profile, resolve_root

# Above this, one request is worth reporting on its own.
_EXPENSIVE = 100
_VERY_EXPENSIVE = 1000


def _django_side(tenant_hint: str | None = None) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Open views and per-view cost, or empty if this is not a Django project."""
    problems: list[str] = []
    open_views: dict[str, Any] = {}
    costs: dict[str, Any] = {}

    try:
        from .exposure_auth import open_endpoints

        report = open_endpoints()
        if report.get("rest_framework_installed"):
            for label in report.get("open_views", []):
                open_views[label] = {"reason": "no permission class narrows it"}
            for finding in report.get("findings", []):
                open_views.setdefault(finding["view"], {})["exposes"] = finding.get(
                    "sensitive_fields"
                ) or []
    except Exception as exc:
        problems.append(f"open_endpoints: {type(exc).__name__}: {exc}")

    try:
        from .endpoint_cost import endpoint_cost

        report = endpoint_cost()
        for endpoint in report.get("endpoints", []):
            costs[endpoint["view"]] = endpoint
    except Exception as exc:
        problems.append(f"endpoint_cost: {type(exc).__name__}: {exc}")

    return open_views, costs, problems


def _fastapi_side(root: Any) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], list[str]]:
    """Unauthenticated routes and the relationships they walk per row."""
    problems: list[str] = []
    routes: dict[str, Any] = {}
    per_row: dict[str, list[dict[str, Any]]] = {}

    try:
        from .fastapi_exposure import fastapi_exposure

        report = fastapi_exposure(search_path=str(root))
        # The inventory, not the findings: a route with a perfectly narrow
        # response_model produces no exposure finding and is still reachable
        # by anyone, which is exactly the half that matters here.
        for entry in report.get("routes", []):
            if entry["authenticated"]:
                continue
            routes[entry["endpoint"]] = entry
    except Exception as exc:
        problems.append(f"fastapi_exposure: {type(exc).__name__}: {exc}")

    try:
        from .sqlalchemy_nplusone import sqlalchemy_nplusone

        report = sqlalchemy_nplusone(search_path=str(root))
        for finding in report.get("in_loops", []) + report.get("in_response_models", []):
            per_row.setdefault(finding["function"], []).append(finding)
    except Exception as exc:
        problems.append(f"sqlalchemy_nplusone: {type(exc).__name__}: {exc}")

    return routes, per_row, problems


def amplification(search_path: str | None = None) -> dict[str, Any]:
    """Endpoints that are both reachable without credentials and expensive.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    root = resolve_root(search_path)
    profile = get_profile(root)

    findings: list[dict[str, Any]] = []
    problems: list[str] = []
    # A half that ran and found nothing to work with is not the same as a
    # half that found nothing wrong, and reporting zero findings without
    # saying which one happened is the failure this check was built to avoid
    # in other people's tools.
    empty: list[str] = []
    considered = 0

    if profile.is_django:
        open_views, costs, django_problems = _django_side()
        problems.extend(django_problems)
        considered += len(open_views)
        if not costs:
            empty.append(
                "no Django endpoint could be costed: endpoint_cost works from a "
                "view's serializer_class and queryset, and none of this "
                "project's views declare one, so the expensive half of the "
                "question has no data"
            )
        if not open_views:
            empty.append("no Django view was found to be reachable without a permission class")

        for label, info in sorted(open_views.items()):
            endpoint = costs.get(label)
            if endpoint is None:
                continue
            queries = endpoint["estimated_queries"]
            pagination = endpoint.get("pagination") or {}
            unpaginated = endpoint["kind"] == "list" and not pagination.get("paginated", True)

            if queries < _EXPENSIVE and not unpaginated:
                continue

            # The two halves are different problems and combining them into one
            # severity buried the interesting case: on the demo project this
            # reported thirteen criticals, of which one cost 2851 queries and
            # eight cost one. Critical is reserved for both at once, or for a
            # cost high enough to matter on its own.
            expensive = queries >= _EXPENSIVE
            very_expensive = queries >= _VERY_EXPENSIVE
            if very_expensive or (unpaginated and expensive):
                severity = "critical"
            else:
                severity = "high"
            reasons = []
            if queries >= _EXPENSIVE:
                reasons.append(f"about {queries} queries per request")
            if unpaginated:
                reasons.append("no pagination, so the response is the whole table")

            findings.append({
                "framework": "django",
                "endpoint": label,
                "location": endpoint.get("location"),
                "estimated_queries": queries,
                "at_least": endpoint.get("at_least", False),
                "paginated": pagination.get("paginated"),
                "exposes": info.get("exposes") or [],
                "severity": severity,
                "why": (
                    "nothing narrows who can call this, and "
                    + " and ".join(reasons)
                    + ". One client at ten requests a second is "
                    + (f"{queries * 10} queries a second" if expensive
                       else "ten full copies of the table a second")
                    + ", from a laptop, with no credentials"
                ),
                "fix": (
                    "give the view a permission class, or make the request cheap: "
                    + ("add pagination, and " if unpaginated else "")
                    + "select_related/prefetch_related what the serializer reads"
                ),
            })

    if profile.is_async_web:
        routes, per_row, fastapi_problems = _fastapi_side(root)
        problems.extend(fastapi_problems)
        considered += len(routes)
        if not per_row:
            empty.append(
                "no SQLAlchemy relationship was found to be crossed per row, so "
                "the expensive half of the FastAPI question has no data"
            )
        if not routes:
            empty.append("no FastAPI route was found without an authenticating dependency")

        for name, route in sorted(routes.items()):
            crossings = per_row.get(name) or []
            if not crossings:
                continue
            findings.append({
                "framework": "fastapi",
                "endpoint": f"{route['method']} {route['path']}",
                "function": name,
                "location": f"{route['file']}:{route['line']}",
                "relationships_per_row": [
                    f"{c['model']}.{c['attribute']}" for c in crossings
                ],
                "severity": "critical",
                "why": (
                    "nothing establishes who is calling this, and it walks "
                    + ", ".join(f"{c['model']}.{c['attribute']}" for c in crossings)
                    + " once per row, so the cost of one request grows with the "
                    "table and the caller needs no credentials"
                ),
                "fix": (
                    "add a dependency that authenticates the caller, and load the "
                    "relationship eagerly with selectinload so the cost stops "
                    "growing per row"
                ),
            })

    order = {"critical": 0, "high": 1}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), -f.get("estimated_queries", 0)))

    return {
        "search_path": str(root),
        "frameworks": dict(profile.frameworks),
        "open_endpoints_considered": considered,
        "finding_count": len(findings),
        "critical_count": sum(1 for f in findings if f["severity"] == "critical"),
        "findings": findings,
        "checks_that_could_not_run": problems,
        "sides_with_no_data": empty,
        "answerable": not empty or bool(findings),
        "note": (
            "Each half of this is somebody else's finding and neither is wrong "
            "alone: an endpoint with no authentication is correct on a public "
            "catalogue, and an expensive endpoint behind a login is a backlog "
            "item. Together they are one request, from anyone, that costs the "
            "database a great deal - and every one of those requests returns "
            "200, so nothing in the logs looks like an attack.\n\n"
            "The tools that look for this are DAST scanners: they need the "
            "service running, reachable, and holding enough rows for the cost "
            "to show. All of it is in the source.\n\n"
            "A result of zero findings is only good news when both halves had "
            "something to work with. When one of them did not, that is listed "
            "under sides_with_no_data and the answer is 'could not look', not "
            "'nothing to find'.\n\n"
            "The query estimate carries a stated assumption about how many "
            "child rows a parent has, so the absolute number is only as good as "
            "that assumption. The ratio is what survives it. An unpaginated "
            "public list is reported whatever the number, because that one is "
            "not a load problem - it is the whole table, to anyone, in one "
            "request."
        ),
    }
