# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""MCP server exposing read-only introspection and analysis of a Django project.

MCPServer is the decorator API of the official MCP Python SDK. In SDK v1 the
same class was called FastMCP; it was renamed in v2. Neither has anything to do
with FastAPI, despite the old name.

Tools are for actions and for anything parameterised. Resources are for stable,
addressable data: the model graph does not change between calls, so it is
exposed as a resource rather than as another tool.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .aggregates import multiplied_aggregates as _multiplied_aggregates
from .amplification import amplification as _amplification
from .api_contract import CONTRACT_FILE as _CONTRACT_FILE
from .api_contract import contract as _contract
from .api_contract import diff as _contract_diff
from .api_contract import load_snapshot as _load_contract
from .api_contract import write_snapshot as _write_contract
from .asyncio_blocking import blocking_in_async as _blocking_in_async
from .bypass import bypassed_effects as _bypassed_effects
from .cascade import delete_impact as _delete_impact
from .celery_tasks import celery_arguments as _celery_arguments
from .check import run_all as _run_all
from .choices import choice_typos as _choice_typos
from .concurrency import race_conditions as _race_conditions
from .dangling import dangling_references as _dangling_references
from .datetimes import datetime_audit as _datetime_audit
from .deploy_safety import deploy_safety as _deploy_safety
from .django_env import DjangoBootError, ensure_django
from .endpoint_cost import endpoint_cost as _endpoint_cost
from .explain import explain_model as _explain_model
from .exposure_auth import open_endpoints as _open_endpoints
from .fastapi_exposure import fastapi_exposure as _fastapi_exposure
from .impact import impact as _impact
from .indexes import missing_indexes as _missing_indexes
from .introspect import list_models as _list_models
from .loop_queries import queries_in_loops as _queries_in_loops
from .migrations import migration_risk as _migration_risk
from .money import money_precision as _money_precision
from .nplusone import analyse_template as _analyse_template
from .on_commit import escaping_side_effects as _escaping_side_effects
from .overfetch import unused_eager_loading as _unused_eager_loading
from .project import get_profile as _get_profile
from .project import resolve_root as _resolve_root
from .scan import scan_templates as _scan_templates
from .serializer_nplusone import serializer_nplusone as _serializer_nplusone
from .serializers import serializer_exposure as _serializer_exposure
from .signals import what_happens_on as _what_happens_on
from .sqlalchemy_nplusone import sqlalchemy_nplusone as _sqlalchemy_nplusone
from .suggest import suggest_fixes as _suggest_fixes
from .tenancy import find_unscoped_queries as _find_unscoped_queries

# What a client is told about this server before it calls anything.
#
# An assistant handed 36 tools with no ordering picks by name, and the names
# do not say which question each answers. These instructions are the ordering:
# where to start when something is broken, which tool answers which question,
# and - the part that matters most - that an empty result from this server
# means "could not look" as often as it means "nothing to find", and that the
# reports say which.
INSTRUCTIONS = """Read-only analysis of a Django project. Nothing here runs the application,
changes data, or reaches the network.

**Start with `project_info`** whenever anything looks wrong or empty. Almost
every "it found nothing" is a settings module the server could not import, and
that one call says so.

Which tool answers which question:

- *What will break when I deploy?* `deploy_safety`, then `migration_risk`.
- *What does this delete take with it?* `delete_impact`.
- *Why is this endpoint slow?* `endpoint_cost`, `serializer_nplusone`,
  `queries_in_loops`, `unused_eager_loading`.
- *Where do I start on a long list?* `request_impact` groups every finding by
  the endpoints that reach it.
- *What can a stranger reach?* `open_endpoints`, then `amplification`.
- *Which query returns nothing forever?* `choice_typos`.
- *Which link, template, receiver or scheduled task is already dead?*
  `dangling_references`.
- *Everything at once, with one verdict:* `check`.

Two things to carry into how you read the answers.

**An empty result is not the same as a clean one.** Every report says how much
it could see: how many views it found, how many literals it checked, which
checks could not run. Quote that alongside a "nothing found", because a check
that could not import the project reports zero findings too.

**Every check states what it cannot see, in its own output.** Those sentences
are not disclaimers to skip - they are the boundary of the answer, and passing
them on is the difference between a useful report and a misleading one.
"""

mcp = MCPServer(
    "django-chainsaw",
    title="Django Chainsaw",
    version="0.1.0",
    instructions=INSTRUCTIONS,
    website_url="https://github.com/syrian963/django-chainsaw-mcp",
)

# Everything here reads. Declaring it lets a client stop asking permission for
# each of 36 calls, which is the difference between a server somebody uses
# while working and one they use once.
#
# `idempotent_hint` is true because the same question about an unchanged tree
# gives the same answer - these are analyses, not requests. `open_world_hint`
# is false because the only thing they touch is the project on disk.
READS_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# The one exception: `update=True` writes the contract snapshot. Writing the
# same capture twice is still idempotent, and it destroys nothing, but a client
# that auto-approves read-only calls should stop and ask about this one.
WRITES_A_SNAPSHOT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def _guard(fn, *args, **kwargs) -> dict[str, Any]:
    """Turn expected failures into readable output instead of a dead server."""
    try:
        return fn(*args, **kwargs)
    except (DjangoBootError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool(annotations=READS_ONLY)
def project_info() -> dict[str, Any]:
    """Check that the target Django project loads, and report what it is.

    Run this first when something is not working. It is the smallest call that
    proves both halves of the setup: the MCP transport and the Django boot.
    """
    try:
        config = ensure_django()
    except DjangoBootError as exc:
        return {"ok": False, "error": str(exc)}

    import django
    from django.apps import apps
    from django.conf import settings

    return {
        "ok": True,
        "django_version": django.get_version(),
        "settings_module": config.settings_module,
        "project_path": str(config.project_path),
        "debug": settings.DEBUG,
        "installed_apps": [cfg.label for cfg in apps.get_app_configs()],
        "database_engines": {
            alias: conf.get("ENGINE", "") for alias, conf in settings.DATABASES.items()
        },
    }


@mcp.tool(annotations=READS_ONLY)
def endpoint_cost(
    page_size: int = 50,
    nested_fan_out: int = 5,
    list_only: bool = False,
) -> dict[str, Any]:
    """How many queries one request to each endpoint will cost.

    Every tool that answers this runs the application and tells you afterwards:
    the debug toolbar, silk, assertNumQueries. The number is derivable before
    anything runs. One query for the page, plus one per object for every
    serializer field crossing a relation the view did not prefetch, plus that
    again per level of nesting.

    On the demo project the same serializer measured 2852 queries behind an
    unoptimised queryset and 2 behind an optimised one. The ratio is the
    reliable part; the absolute number is only as good as nested_fan_out.

    Args:
        page_size: objects a list response returns.
        nested_fan_out: assumed children per parent one level down. A property
            of your data that reading the code cannot reveal.
        list_only: skip views that only ever return a single object.
    """
    return _guard(
        _endpoint_cost,
        page_size=page_size,
        nested_fan_out=nested_fan_out,
        list_only=list_only,
    )


@mcp.tool(annotations=READS_ONLY)
def api_contract(max_depth: int = 3) -> dict[str, Any]:
    """The shape every serializer currently promises its clients.

    Field names, types, whether each is read only, required, nullable, and what
    the nested ones expand to. Resolved from the class definitions, so nothing
    needs to run and no request needs to be sent.

    Capture this on the branch you already shipped, commit the result, and
    api_contract_check will tell a later branch what it broke.

    Args:
        max_depth: how far to expand nested serializers.
    """
    return _guard(_contract, max_depth=max_depth)


@mcp.tool(annotations=WRITES_A_SNAPSHOT)
def api_contract_check(
    snapshot_path: str = _CONTRACT_FILE,
    update: bool = False,
    max_depth: int = 3,
) -> dict[str, Any]:
    """What this branch changes about the API, and who it breaks.

    Removing a serializer field is a one line diff that reads as tidying up.
    The client on a version nobody updated still reads that field. Every tool
    that catches this needs the app running: drf-api-checker records real
    responses during a test run, OpenAPI diffing needs the schema generated.

    Changes come back sorted by who they hurt. Breaking means an existing
    client stops working: a field it reads disappears, a field it omits becomes
    required, a type or a constraint narrows. Risky means it still parses but
    the values may surprise it. Additive means nobody notices.

    Args:
        snapshot_path: the committed contract to compare against.
        update: overwrite the snapshot with the current shape instead of
            comparing. Do this once you have decided a change is intended.
        max_depth: how far to expand nested serializers.
    """

    def run() -> dict[str, Any]:
        captured = _contract(max_depth=max_depth)
        if update:
            return _write_contract(snapshot_path, captured)

        baseline = _load_contract(snapshot_path)
        if baseline is None:
            return {
                "ok": False,
                "snapshot": snapshot_path,
                "error": (
                    "No contract snapshot yet. Run this with update=True on a "
                    "branch whose API shape is the one clients already use, "
                    "then commit the file."
                ),
            }
        return _contract_diff(baseline, captured)

    return _guard(run)


@mcp.tool(annotations=READS_ONLY)
def escaping_side_effects(
    search_path: str | None = None,
    include_low_confidence: bool = False,
) -> dict[str, Any]:
    """Calls inside a transaction whose effect cannot be rolled back.

    A transaction can be rolled back. An email cannot, and neither can a
    webhook or a task a worker has already picked up.

        with transaction.atomic():
            order = Order.objects.create(...)
            send_confirmation.delay(order.pk)

    Two defects, and only one is famous. The race: the broker has the task
    immediately, a worker can start before the commit, and it queries for a row
    that is not there. It passes every test, because tests run in a transaction
    that never commits with a worker that runs eagerly, and it fails under load.
    The quieter one: if anything after that line raises, the order is gone and
    the customer has the email.

    The fix is transaction.on_commit, and calls already deferred that way are
    not reported. The ecosystem's answer to this is runtime wrappers; ruff and
    flake8-django do not look at it.

    Args:
        search_path: directory to scan. Defaults to the project root.
        include_low_confidence: also report calls like `.send()` that are
            guessed from the name, since it is also Signal.send and socket.send.
    """
    return _guard(
        _escaping_side_effects,
        search_path=search_path,
        include_low_confidence=include_low_confidence,
    )


@mcp.tool(annotations=READS_ONLY)
def bypassed_effects(search_path: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Bulk writes that skip everything the model's save() chain promised.

    what_happens_on says saving an Order creates an Invoice. That is true for
    order.save() and false for Order.objects.bulk_create(), .bulk_update() and
    .update(): they go straight to SQL, so no save() override runs and no
    pre_save/post_save receiver fires. Django documents this in one sentence
    per method; nothing at the call site says it.

    The finding is not "bulk_create bypasses signals" but this call, on this
    model, skips these named effects - the receivers, the overridden save(),
    the models that would have been written, transitively. A cache that never
    gets invalidated and a search index that quietly drifts are both this.

    Only models whose chain does something are reported. QuerySet.delete() is
    not listed: Django sends delete signals per object, so that chain fires.

    Args:
        search_path: directory to scan. Defaults to the project root.
        model: restrict to one "app_label.ModelName".
    """
    return _guard(_bypassed_effects, search_path=search_path, model=model)


@mcp.tool(annotations=READS_ONLY)
def race_conditions(search_path: str | None = None, include_parameters: bool = True) -> dict[str, Any]:
    """Read-modify-save races, and row locks taken outside any transaction.

        product = Product.objects.get(pk=pk)
        product.stock -= quantity
        product.save()

    Two requests read 10, both subtract 3, both write 7; one sale is gone. A
    transaction does not help, since neither sees the other's uncommitted
    write. The fix is F("stock") - quantity so the database does the maths,
    or select_for_update() inside atomic() to hold the row - and both of
    those are silent here. Counters, balances, stock, retry counts: the
    fields where off-by-one costs money.

    Also: get_or_create() on a lookup no unique field, unique_together or
    UniqueConstraint covers - two requests miss the get together, both
    create, and the next call raises MultipleObjectsReturned. And
    select_for_update() with no atomic() around it, which is not a race
    but a TransactionManagementError the first time the line is reached.
    Whether a transaction is open is judged with the call graph, so a caller's
    atomic(), a decorator and ATOMIC_REQUESTS on a view all count.

    Args:
        search_path: directory to scan. Defaults to the project root.
        include_parameters: also report mutations of an instance passed in
            as a parameter, at medium confidence (the caller may hold a lock).
    """
    return _guard(_race_conditions, search_path=search_path, include_parameters=include_parameters)


@mcp.tool(annotations=READS_ONLY)
def open_endpoints(include_unbounded: bool = True) -> dict[str, Any]:
    """Endpoints anyone can call, crossed with what their serializer exposes.

    serializer_exposure knows CustomerExportSerializer leaks a password reset
    token; a Semgrep rule knows a view has AllowAny. Each alone is a judgement
    call - maybe the serializer only feeds an admin export, maybe the view
    serves a catalogue. Together there is nothing left to judge, and neither
    check can make the connection alone.

    DRF's own default permission is AllowAny. A project that never configured
    DEFAULT_PERMISSION_CLASSES has every view without explicit
    permission_classes open, and none of them say so; that is volunteered
    first. Views overriding get_permissions() are listed, not judged.

    Args:
        include_unbounded: also report open endpoints whose serializer uses
            fields="__all__" or exclude, even with nothing sensitive on the
            model today. The next migration decides what leaks.
    """
    return _guard(_open_endpoints, include_unbounded=include_unbounded)


@mcp.tool(annotations=READS_ONLY)
def unused_eager_loading(include_low_confidence: bool = False) -> dict[str, Any]:
    """select_related and prefetch_related the serializer never reads.

    Every tool in this space looks the other way: a relation the serializer
    touches that the queryset did not prefetch, which is the N+1. This is the
    opposite, and it costs on every request. An unused select_related is a
    JOIN on every row; an unused prefetch_related is a whole extra query plus
    the objects it returns.

    It is invisible because it looks like an optimisation, and it usually was
    one - the field it was added for was removed and nobody takes the line out,
    because removing one feels riskier than leaving it in.

    nplusone finds this at runtime by watching which loaded objects go
    untouched, so it covers the paths the tests exercise. Both halves are in
    the source: the queryset says what it loads, the serializer what it reads.

    Args:
        include_low_confidence: also report views whose serializer has a
            SerializerMethodField or which override list/retrieve/
            to_representation, where the relation may be read out of sight.
    """
    return _guard(_unused_eager_loading, include_low_confidence=include_low_confidence)


@mcp.tool(annotations=READS_ONLY)
def money_precision(search_path: str | None = None) -> dict[str, Any]:
    """Places where a decimal amount stops being exact.

    A DecimalField exists so that money is exact. Four things give that up,
    and they are not equally bad:

        Decimal(0.1)            wrong from birth - 0.1 has no exact binary
                                form, so this is 0.1000000000000000055...
        float(invoice.amount)   a one-way door; everything after is approximate
        round(amount, 2)        exact, but banker's rounding: 0.125 becomes
                                0.12 where an invoice expects 0.13
        FloatField("price")     the column itself cannot hold money

    Decimal(0.5) is NOT reported as a defect: that float is exactly
    representable and nothing is lost. The check computes the round trip, so
    on a real project 41 of 50 Decimal(<float>) calls came back harmless and
    11 were genuinely wrong. Nothing in the linter ecosystem looks at this;
    the usual advice stops at the model definition and every one of these
    happens somewhere else.

    Args:
        search_path: directory to scan. Defaults to the project root.
    """
    return _guard(_money_precision, search_path=search_path)


@mcp.tool(annotations=READS_ONLY)
def project_profile(search_path: str | None = None) -> dict[str, Any]:
    """What this project is built on, without needing Django to boot.

    Frameworks are counted by how many of the project's own files import
    them, not by what is installed: a package sitting in the virtualenv that
    nothing imports is a fact about the environment, not the code. A project
    can be Django and FastAPI at once and both are reported.

    Use it to find out which checks can say anything here.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """

    def run() -> dict[str, Any]:
        return _get_profile(_resolve_root(search_path)).as_dict()

    return _guard(run)


@mcp.tool(annotations=READS_ONLY)
def blocking_in_async(
    search_path: str | None = None,
    follow_calls: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Synchronous calls that run on the event loop.

    FastAPI runs an `async def` endpoint on the loop itself and a `def`
    endpoint in a threadpool. So a blocking call inside `async def` does not
    slow one request, it stops every request in the process - invisible at one
    request a second, an outage at two hundred.

    ruff's ASYNC rules cover open, time.sleep and subprocess inside an async
    function. This adds the two that matter more: a synchronous database call,
    which is the common one, and a blocking call reached through another
    project function, where nothing at the call site looks blocking. The second
    comes back with the path that reaches it.

    Needs no Django. A `def` endpoint is never reported, because blocking in a
    threadpool is fine and telling somebody to make it async causes the outage.

    Args:
        search_path: directory to scan. Defaults to the configured project.
        follow_calls: also report blocking reached through a project function.
        max_depth: how many calls deep to follow.
    """
    return _guard(
        _blocking_in_async,
        search_path=search_path,
        follow_calls=follow_calls,
        max_depth=max_depth,
    )


@mcp.tool(annotations=READS_ONLY)
def fastapi_exposure(search_path: str | None = None) -> dict[str, Any]:
    """FastAPI endpoints that serialise more than they declare.

        @app.get("/users/{pk}")
        async def get_user(pk: int):
            return session.get(User, pk)

    No response_model and no return annotation, so FastAPI serialises whatever
    it is handed - the whole ORM object, every column, including the ones
    added to the model next month. The absence of one line is the entire bug,
    so there is nothing in the file to read or review.

    Every FastAPI guide says to set response_model and several say a CI rule
    should enforce it. No linter ships one.

    An endpoint returning a dict or a literal is not reported: the author
    decided what goes in it. Unauthenticated is critical, behind a dependency
    is high - it still leaks to everyone who can log in.

    Nothing here imports the project, because a FastAPI app usually wants a
    database URL and a secret before it will import at all, and none of that is
    needed to read a decorator. Needs no Django.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    return _guard(_fastapi_exposure, search_path=search_path)


@mcp.tool(annotations=READS_ONLY)
def sqlalchemy_nplusone(search_path: str | None = None) -> dict[str, Any]:
    """Relationships SQLAlchemy will load one row at a time.

        orders = db.query(Order).all()
        for order in orders:
            print(order.customer.name)      # one query per order

    And the quieter FastAPI shape, where there is no loop to see:

        @app.get("/orders", response_model=list[OrderOut])
        def list_orders(db=Depends(get_db)):
            return db.query(Order).all()

    OrderOut declares `items`, so serialisation walks the relationship once
    per row - after the endpoint has returned, which is why nothing in the
    function body mentions it.

    nplusone finds this at runtime by watching lazy loads happen, and
    lazy="raise" turns it into an exception; both need the code path to run.
    A relationship declared lazy="selectin", "joined" or "raise" is never
    reported here, since the first two are already eager and the third is the
    recommended fix.

    Nothing is imported. Needs no Django.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    return _guard(_sqlalchemy_nplusone, search_path=search_path)


@mcp.tool(annotations=READS_ONLY)
def amplification(search_path: str | None = None) -> dict[str, Any]:
    """Endpoints anyone can call that cost a great deal to answer.

    Two facts, each of which is somebody else's finding and neither of which
    is wrong alone:

        GET /orders has no authentication.
        GET /orders issues about 2852 queries per request.

    The first is right on a public catalogue. The second, behind a login, is a
    backlog item. Together they are one request, from anyone, that costs the
    database three thousand queries - and every one of them returns 200, so
    nothing in the logs looks like an attack.

    The other half is the unbounded list: public, unpaginated, and therefore
    the whole table in one request. That is not load, it is exfiltration.

    The tools that look for this are DAST scanners; they need the service
    running, reachable and holding enough rows for the cost to show. All of it
    is in the source. Works on Django (DRF views) and FastAPI.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    return _guard(_amplification, search_path=search_path)


@mcp.tool(annotations=READS_ONLY)
def celery_arguments(search_path: str | None = None) -> dict[str, Any]:
    """Model instances handed to Celery tasks, and calls whose arity is wrong.

        order = Order.objects.get(pk=pk)
        send_confirmation.delay(order)

    The worker does not get that order. It gets whatever the serialiser made
    of it, rehydrated later on another machine: the row may have changed in
    between, the whole object crosses the broker, and under the JSON
    serialiser - the default since Celery 4 - it may not encode at all. Pass
    the primary key and let the task load it.

    Also reports a dispatch whose argument count cannot match the task.
    Celery's strict_typing catches that at call time, which for a nightly job
    or an error branch means in production, months later.

    flake8-pie has Celery lints for names, crontab arguments and expirations.
    None of them look at what is passed.

    A dispatch is only checked when the name resolves to a task this project
    defines, through the file's own imports - matching on the bare name
    reported unrelated objects of the same name against the task's signature.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    return _guard(_celery_arguments, search_path=search_path)


@mcp.tool(annotations=READS_ONLY)
def queries_in_loops(
    search_path: str | None = None,
    include_writes: bool = True,
) -> dict[str, Any]:
    """Database work written inside a loop, split by what the fix is.

    The template and serializer checks here find the N+1 a framework causes.
    This finds the one somebody wrote by hand, which is where it lives in a
    codebase whose views build their responses themselves.

    Three shapes share one appearance and need three different fixes:

        Customer.objects.get(pk=order.customer_id)   uses the loop variable:
                                                     once per row, needs a bulk
                                                     fetch or a prefetch
        Config.objects.get(key="vat")                does not: the same
                                                     question N times for the
                                                     same answer, move it above
                                                     the loop
        order.save()                                 N round trips; bulk_update
                                                     fixes it and skips signals

    django-check does static N+1 for relation access in a loop and is the
    closest existing tool; nplusone and the debug toolbar find it at runtime.
    The separation is what is added here.

    Args:
        search_path: directory to scan. Defaults to the configured project.
        include_writes: also report save()/delete() inside a loop.
    """
    return _guard(
        _queries_in_loops,
        search_path=search_path,
        include_writes=include_writes,
    )


@mcp.tool(annotations=READS_ONLY)
def request_impact(
    search_path: str | None = None,
    max_depth: int = 8,
    tenant_root: str = "auth.User",
) -> dict[str, Any]:
    """Every finding, grouped by the entry points that actually reach it.

    The other checks answer "where is this defect". A few hundred correct
    entries sorted by severity still does not say where to start, because risk
    is severity times how often the code runs and nothing in the list says
    whether a line is on the path of an endpoint served ten thousand times an
    hour or of a command last run in 2023.

    This runs the checks, maps each finding to the function containing it, and
    walks the call graph backwards to the HTTP routes, Celery tasks, signal
    receivers and management commands that reach it. Each finding carries the
    path taken to it.

    `unattributed` means no entry point this can see reaches the finding. It
    does not mean unreachable and it does not mean safe - a plain Django
    function view carries no decorator and belongs to no view class.

    Args:
        search_path: directory to scan. Defaults to the configured project.
        max_depth: how many callers to walk back through.
        tenant_root: passed to the ownership check when this runs the checks.
    """
    return _guard(
        _impact,
        search_path=search_path,
        max_depth=max_depth,
        tenant_root=tenant_root,
    )


@mcp.tool(annotations=READS_ONLY)
def choice_typos(
    search_path: str | None = None,
    include_tests: bool = True,
) -> dict[str, Any]:
    """Literals compared against a field whose `choices` will never match them.

        STATUS = [("canceled", "Canceled"), ...]
        Order.objects.filter(status="cancelled")

    Two Ls. Valid Python, valid SQL, zero rows, no exception, wrong forever.
    Nothing in Django objects: `choices` is checked by `full_clean()`, which a
    queryset never calls and `create()` never calls either - so the write side
    is worse, and puts a value in the column the application does not believe
    exists.

    No existing tool finds this: django-stubs types the field as `str` rather
    than a Literal union of its choices, so mypy is satisfied, and the DJ rules
    do not read the model registry.

    Only literals are checked - an enum member is the spelling that cannot go
    wrong - and only equality and `in`, because `iexact` can legitimately match
    a differently spelled value. `order.status == "..."` names no model, so it
    is reported only when the literal is wrong for every model with a field of
    that name.

    Args:
        search_path: directory to scan. Defaults to the configured project.
        include_tests: also scan test files.
    """
    return _guard(
        _choice_typos, search_path=search_path, include_tests=include_tests
    )


@mcp.tool(annotations=READS_ONLY)
def dangling_references(
    search_path: str | None = None,
    include_templates: bool = True,
) -> dict[str, Any]:
    """URL names and template names that nothing will resolve.

        return redirect("order-detial")
        return render(request, "shop/order_detial.html", context)
        {% url 'shop:order-detial' order.pk %}

    Each of these is resolved while serving a request and checked by nothing
    before then. Rename a URL pattern or move a template and they keep
    importing, keep passing every test that does not walk that branch, and
    raise NoReverseMatch or TemplateDoesNotExist the first time a real person
    opens the page - on the path nobody was watching.

    Both sides use the project's own machinery: names come from every URLconf
    in the project walked through each include(), so namespaces are real and a
    second URLconf served by host is not mistaken for a missing one; templates
    go through get_template(), so the project's loaders decide.

    `by_name` groups the findings, because one missing name used in
    thirty-five places is one problem.

    Args:
        search_path: directory to scan. Defaults to the configured project.
        include_templates: also read `{% url %}`, `{% include %}` and
            `{% extends %}` out of the templates.
    """
    return _guard(
        _dangling_references,
        search_path=search_path,
        include_templates=include_templates,
    )


@mcp.tool(annotations=READS_ONLY)
def multiplied_aggregates(search_path: str | None = None) -> dict[str, Any]:
    """Aggregates whose numbers are wrong because a join multiplied the rows.

        Order.objects.annotate(lines=Count("lines"), shipments=Count("shipments"))

    Joining two multi-valued relations gives the cartesian product of them: an
    order with 3 lines and 2 shipments produces 6 rows, and both counts come
    back as 6. Nothing raises. Two plausible numbers, both the product of the
    two, usually on a dashboard nobody can check by hand.

    Only Count and Sum are reported. A join repeats rows uniformly within each
    group, so Min and Max return the value they would anyway and Avg divides a
    multiplied total by a multiplied count - including them reported a correct
    query as a defect on the first real project this saw.

    Count(distinct=True) is treated as correct. Sum has no equivalent and needs
    a Subquery, so a query is still reported when every Count in it is distinct
    but a Sum crosses a second relation.

    Django's own documentation warns about this and no linter checks it.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    return _guard(_multiplied_aggregates, search_path=search_path)


@mcp.tool(annotations=READS_ONLY)
def suggest_fixes(tenant_root: str = "auth.User") -> dict[str, Any]:
    """Findings turned into code, grouped by how safe each one is to apply.

    Three classes, and the distinction is the point:

    - **mechanical**: one correct answer derivable from the code alone, such as
      datetime.now() becoming timezone.now(). No judgement in it.
    - **generated**: a machine can write the artefact, a human decides whether
      it should exist. An index migration is exactly right as text and entirely
      wrong if that table is write-heavy.
    - **advisory**: real code with the right names resolved, but the decision
      belongs to somebody who knows the domain. Which user owns a row is not a
      question the AST can answer.

    Nothing is applied. The CLI `fix --write` applies the mechanical class only.

    Args:
        tenant_root: the model that owns data, for the ownership suggestions.
    """
    return _guard(_suggest_fixes, tenant_root=tenant_root)


@mcp.tool(annotations=READS_ONLY)
def check(
    tenant_root: str = "auth.User",
    only: list[str] | None = None,
    skip: list[str] | None = None,
) -> dict[str, Any]:
    """Run every analysis and return one severity-sorted list.

    The single call to reach for on an unfamiliar project. It runs the checks
    whose findings are defects, merges them, and sorts by severity, instead of
    making you know which of a dozen tools to ask for.

    A check that fails to run is listed in `checks_failed` rather than counted
    as clean.

    Args:
        tenant_root: the model that owns data, for the ownership check.
        only: run just these checks.
        skip: run everything except these.
    """
    return _guard(_run_all, tenant_root=tenant_root, only=only, skip=skip)


@mcp.tool(annotations=READS_ONLY)
def serializer_nplusone(max_depth: int = 3) -> dict[str, Any]:
    """N+1 queries in DRF serializers, with the queryset fix for each.

    find_n_plus_one answers this for templates. Most Django written today
    renders JSON, and there the N+1 comes from a nested serializer field: a list
    of a hundred orders runs a hundred extra queries per nested relation.

    Follows nested serializers, so the lookup it suggests is the full path.

    Args:
        max_depth: how far to follow nested serializers.
    """
    return _guard(_serializer_nplusone, max_depth=max_depth)


@mcp.tool(annotations=READS_ONLY)
def explain_model(
    model_label: str,
    tenant_root: str = "auth.User",
    include_raw: bool = False,
) -> dict[str, Any]:
    """Everything known about one model, and the risks only visible combined.

    Start here when meeting a model for the first time. It runs the structural,
    ownership, deletion, signal, exposure, index and datetime checks and returns
    one picture instead of seven reports.

    The part worth reading is `correlated_risks`. Some defects exist only in the
    overlap and no single analyser can see them: a model that is owned, read
    without scoping, and serialised with fields = "__all__" is a complete path
    from a URL to another customer's row, while each of those three alone is
    just a warning.

    Args:
        model_label: "app_label.ModelName".
        tenant_root: the model that owns data, for the ownership half.
        include_raw: attach the full report from each analyser as well.
    """
    return _guard(
        _explain_model,
        model_label=model_label,
        tenant_root=tenant_root,
        include_raw=include_raw,
    )


@mcp.tool(annotations=READS_ONLY)
def list_models(app_label: str | None = None, include_fields: bool = True) -> dict[str, Any]:
    """List the project's models with their fields and relations.

    Args:
        app_label: Restrict to one app, e.g. "shop". Omit for all apps.
        include_fields: Set False for a short overview without field details.
    """
    return _guard(_list_models, app_label=app_label, include_fields=include_fields)


@mcp.tool(annotations=READS_ONLY)
def delete_impact(model_label: str, max_depth: int = 6) -> dict[str, Any]:
    """Show what deleting one row of a model would take with it.

    Follows on_delete across the whole model graph: which models lose rows
    through CASCADE, which PROTECT relations would block the delete, and which
    fields get set to NULL. Reads the graph only, never the database.

    Args:
        model_label: "app_label.ModelName", e.g. "shop.Customer".
        max_depth: how far to follow chained cascades.
    """
    return _guard(_delete_impact, model_label=model_label, max_depth=max_depth)


@mcp.tool(annotations=READS_ONLY)
def find_n_plus_one(template_path: str, root_models: dict[str, str]) -> dict[str, Any]:
    """Find relation traversals in a template that each cost a query.

    Resolves attribute chains against the real model graph and flags the ones
    that cross a relation inside a loop, which is where N+1 queries come from.
    Reports candidates: whether a crossing really costs a query depends on the
    queryset in the view, which this does not read.

    Args:
        template_path: path to the template file.
        root_models: context variable to model label, e.g.
            {"orders": "shop.Order"}. Loop variables inherit from these.
    """
    return _guard(_analyse_template, template_path=template_path, root_models=root_models)


@mcp.tool(annotations=READS_ONLY)
def scan_templates(
    template_root: str | None = None,
    project_root: str | None = None,
    root_models: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the N+1 analysis over every template in a directory.

    find_n_plus_one needs a context map per template. This resolves it instead
    from class-based views that declare template_name together with model or
    queryset, so a whole project can be scanned without typing anything.

    Args:
        template_root: template directory. Defaults to the project path.
        project_root: where to look for views. Defaults to the project path.
        root_models: context applied to every template, for names no view supplies.
    """
    return _guard(
        _scan_templates,
        template_root=template_root,
        project_root=project_root,
        root_models=root_models,
    )


@mcp.tool(annotations=READS_ONLY)
def serializer_exposure(include_safe: bool = False) -> dict[str, Any]:
    """What each DRF ModelSerializer exposes, and what looks unintended.

    `fields = "__all__"` is a decision made once and then re-made silently by
    every migration after it. Add a token column to the model and the API starts
    returning it, with no diff on the serializer for anyone to review.

    Args:
        include_safe: also list serializers with an explicit, clean field list.
    """
    return _guard(_serializer_exposure, include_safe=include_safe)


@mcp.tool(annotations=READS_ONLY)
def datetime_audit(search_path: str | None = None) -> dict[str, Any]:
    """Naive datetimes in code, and ambiguous defaults on model fields.

    With USE_TZ on, code that builds its own datetimes with datetime.now() or
    from parts produces naive values, and mixing them with the aware ones the
    ORM returns either raises or compares against the wrong instant. It is
    invisible for ten months and shows up on the two nights the clock moves.

    Also reports model fields whose default is naive, whose default was
    evaluated once at import time, or that set auto_now and auto_now_add
    together.

    Args:
        search_path: directory to scan. Defaults to the project path.
    """
    return _guard(_datetime_audit, search_path=search_path)


@mcp.tool(annotations=READS_ONLY)
def missing_indexes(search_path: str | None = None, min_occurrences: int = 1) -> dict[str, Any]:
    """Fields the code filters or sorts on that carry no index.

    Runtime tools answer this by watching real traffic, which only ever covers
    the paths traffic reached. Reading the source covers every path in the
    repository, works with no database and no traffic, and can run on a branch
    before it ships.

    The trade is that it cannot weigh anything: a filter on a forty row table
    looks like one on a forty million row table. It reports where an index is
    missing and how often the code asks for it, and leaves the decision to
    somebody who knows the row counts.

    Args:
        search_path: directory to scan. Defaults to the project path.
        min_occurrences: only report a field asked for at least this often.
    """
    return _guard(_missing_indexes, search_path=search_path, min_occurrences=min_occurrences)


@mcp.tool(annotations=READS_ONLY)
def what_happens_on(model_label: str, event: str = "save", max_depth: int = 4) -> dict[str, Any]:
    """Follow the signal chain a save or delete actually triggers.

    Nothing at an `order.save()` call site hints that it also writes an Invoice,
    clears a cache and queues a task, because the receivers live elsewhere and
    were connected in AppConfig.ready(). Tools that list registered receivers
    exist; this follows the chain, because the second hop is where the surprise
    lives.

    It is also the other half of delete_impact, which deliberately ignores
    signals and says so.

    Args:
        model_label: "app_label.ModelName".
        event: "save" or "delete".
        max_depth: how far to follow writes into further signals.
    """
    return _guard(_what_happens_on, model_label=model_label, event=event, max_depth=max_depth)


@mcp.tool(annotations=READS_ONLY)
def find_unscoped_queries(
    tenant_root: str = "auth.User",
    search_path: str | None = None,
    max_depth: int = 4,
    include_exempt: bool = False,
) -> dict[str, Any]:
    """Find querysets that read tenant-scoped data without scoping the query.

    This is the shape behind most IDOR reports: a view loads an object by
    primary key and never checks who owns it. Generic analysers struggle
    because the defect is the absence of a filter, and absence has no syntax.
    The model graph makes it tractable: it knows Order reaches the tenant root
    through 'customer', so it can tell that filtering on pk alone is not enough.

    Candidates, not verdicts. A filter in a base class, a mixin, a custom
    manager or a get_queryset() override is invisible from here.

    Args:
        tenant_root: the model that owns data, e.g. "auth.User" or "shop.Customer".
        search_path: directory to scan. Defaults to the project path.
        max_depth: how many relation hops still count as owned.
        include_exempt: also scan admin, management commands and tests.
    """
    return _guard(
        _find_unscoped_queries,
        tenant_root=tenant_root,
        search_path=search_path,
        max_depth=max_depth,
        include_exempt=include_exempt,
    )


@mcp.tool(annotations=READS_ONLY)
def deploy_safety(search_path: str | None = None, max_hits_per_symbol: int = 25) -> dict[str, Any]:
    """Is a pending destructive migration safe to deploy yet?

    A migration linter says RemoveField is backward incompatible, always. This
    answers the question that actually decides the deploy: has the code caught
    up? For every unapplied migration that removes or renames a field, model,
    index or constraint, the source tree is searched for code that still refers
    to it, and each one comes back either "blocking" with file and line numbers,
    or "clear".

    Args:
        search_path: directory to scan. Defaults to the configured project path.
        max_hits_per_symbol: stop after this many references per symbol.
    """
    return _guard(_deploy_safety, search_path=search_path, max_hits_per_symbol=max_hits_per_symbol)


@mcp.tool(annotations=READS_ONLY)
def migration_risk(include_applied: bool = False) -> dict[str, Any]:
    """Rate migrations by what they do to a live database.

    Flags operations that block writes, rewrite a table, or break the code that
    is still running during a rolling deploy.

    Args:
        include_applied: also classify migrations that already ran.
    """
    return _guard(_migration_risk, include_applied=include_applied)


# Argument completion, for the arguments that are a label from the project.
#
# `what_breaks_if_i_delete` needs a model, and there are 424 of them on a real
# project. Typing one from memory is how you get a LookupError, and a wrong
# label is indistinguishable from a model with nothing attached to it.
@mcp.completion()
async def complete_argument(ref: Any, argument: Any, context: Any) -> Any:
    """Suggest model labels where a prompt asks for one."""
    from mcp.types import Completion

    if getattr(argument, "name", None) not in {"model", "model_label"}:
        return None

    try:
        from django.apps import apps

        from .django_env import ensure_django

        ensure_django()
        labels = sorted(model._meta.label for model in apps.get_models())
    except Exception:
        return None

    typed = (getattr(argument, "value", "") or "").lower()
    matches = [label for label in labels if typed in label.lower()]
    # The protocol caps a completion response at 100 values, and a list that
    # long is not a suggestion anyway.
    return Completion(
        values=matches[:100],
        total=len(matches),
        hasMore=len(matches) > 100,
    )


# Prompts are the part of this server that carries the knowledge a tool
# cannot. A tool answers one question; knowing which three questions to ask in
# which order, and how to read the answer, is a workflow - and a workflow that
# lives only in somebody's head gets used once.
#
# Each of these says what to call, in what order, and what the answer does not
# mean. That last part is the reason they exist: the failure mode of this
# server is an assistant reporting "no findings" from a check that could not
# run.


@mcp.prompt(
    title="Is this safe to deploy?",
    description="The migration and rolling-deploy questions, in the order they matter",
)
def before_deploy() -> str:
    """The pre-deploy walkthrough."""
    return """Work out whether the pending changes are safe to ship, in this order.

1. `project_info` first. If it cannot load the project, stop and say so -
   every "nothing found" below would be meaningless.

2. `deploy_safety`. This is the one that blocks a deploy: a migration that
   drops something the running code still uses. During a rolling deploy the
   old pods keep serving against the new schema, so "the code is updated too"
   is not an answer - the question is whether both versions survive the
   window.

3. `migration_risk` for the rest: a migration that locks a table for minutes,
   or rewrites it, is not unsafe but it is an outage if nobody planned for it.

4. `api_contract_check`. A field that disappeared or became required breaks
   clients that are already deployed, and it does it silently.

Report what would break, for whom, and in which of the two windows: during the
deploy, or after it. If a check could not run, say which and why rather than
counting it as clean."""


@mcp.prompt(
    title="Why is this slow?",
    description="Trace an endpoint's cost from the queryset to the serialiser",
)
def why_is_this_slow(endpoint: str = "") -> str:
    """Walk the performance path for one endpoint, or the whole project."""
    scope = f"the endpoint or view `{endpoint}`" if endpoint else "the project"
    return f"""Find out what {scope} actually costs, and do not guess at it.

1. `endpoint_cost` for the estimate per endpoint, and read the caveat it
   prints: it works from a view's serializer and its queryset, so a view that
   builds its response by hand is invisible to it. The count of those is in
   the report.

2. `serializer_nplusone` and `queries_in_loops` for where the queries come
   from. The first finds what a framework causes, the second what somebody
   wrote by hand - including a loop that calls a function that queries, which
   is the shape that looks clean.

3. `unused_eager_loading` for the other direction: a join or prefetch that
   nothing in the response reads is paid for and thrown away.

4. `request_impact` to see whether this endpoint carries findings from other
   checks too.

Then say which single change would remove the most queries, with the number
next to it. "Add select_related" without a count is not an answer somebody can
prioritise."""


@mcp.prompt(
    title="What breaks if I delete this?",
    description="Cascades, signals and the code that still refers to it",
)
def what_breaks_if_i_delete(model: str) -> str:
    """Everything that follows from deleting one row, or one model."""
    return f"""Work out the full consequence of deleting `{model}`, at both levels.

1. `delete_impact` for `{model}`: what cascades, what blocks with PROTECT,
   what gets nulled. It follows the chain transitively, so read the depth -
   two hops away is still your data.

2. `what_happens_on` for `{model}` with `save` and with `delete`: a receiver
   three hops away may send mail or dispatch a task, and that is not
   reversible by a rollback.

3. If the model itself is going away rather than a row, `deploy_safety` and
   `api_contract_check` as well: the column is referenced by code that is
   still running, and by serializers clients depend on.

Answer with the blast radius, not the mechanism: how many tables, which of
them are irreversible, and what a rollback would not undo."""


@mcp.prompt(
    title="Where do I start?",
    description="Turn a long findings list into the few endpoints that carry it",
)
def triage(severity: str = "high") -> str:
    """Prioritise a full run by what a request can actually reach."""
    return f"""There are more findings than anybody can work through in order. Sort them by
what is reachable rather than by severity alone.

1. `check` for the full list, then `request_impact` to group it. Severity ranks
   the defect; it does not rank the risk, because risk is severity times how
   often the code runs. A medium on a login path outranks a critical in a
   helper nobody has called since 2021.

2. Work from the endpoints that carry the most, at `{severity}` and above.
   `request_impact` gives the call path to each finding, so the fix has a
   location rather than a search.

3. Read the `unattributed` count before concluding. Those are not clean - they
   are findings no entry point this can see reaches, and the report separates
   the ones that are genuinely uncalled from the ones where the caller could
   not be resolved.

Give a shortlist of endpoints in the order you would fix them, each with what
it carries and roughly what the fix is."""


@mcp.prompt(
    title="Review this branch",
    description="Only what changed against a ref, with the caveats intact",
)
def review_this_branch(ref: str = "main") -> str:
    """A findings review scoped to one branch."""
    return f"""Review only what this branch changed against `{ref}`.

1. `check` with the whole project first, so you know the baseline, then
   compare against `{ref}` - the CLI does this with `check --since {ref}`, and
   the merge base is used so a branch that is behind `{ref}` is not blamed for
   what `{ref}` gained.

2. A finding with no file - a migration, a serializer class - cannot be placed
   in a diff. Those are kept deliberately rather than dropped, and the count
   is printed. Do not treat them as noise: a migration is exactly the kind of
   change a branch review is for.

3. `api_contract_check` for what this branch does to clients that are already
   deployed.

Report what this branch introduced, separately from what it merely touched,
and say plainly if a check could not run."""


@mcp.resource("django://models", mime_type="application/json")
def model_graph() -> str:
    """The full model graph. Stable between calls, so a resource, not a tool."""
    return json.dumps(_guard(_list_models, app_label=None, include_fields=True), indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
