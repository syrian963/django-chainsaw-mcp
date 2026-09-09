# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Eager loading nobody asked for.

Every tool in this space looks in one direction: a relation the serializer
touches that the queryset did not prefetch, which is the N+1. The other
direction costs money too and almost nothing looks at it.

    class OrderViewSet(ReadOnlyModelViewSet):
        serializer_class = OrderSummarySerializer     # id, placed_at, total
        queryset = Order.objects.select_related("customer", "invoice") \\
                                .prefetch_related("lines__product__category")

Nothing in that serializer renders a customer, an invoice or a product
category. So every request pays for two JOINs it will not read a column from,
plus three extra queries and the memory to hold every product and category on
the page. It is invisible because it looks like an optimisation, and it usually
became one - the field it was added for was removed a year ago and nobody
takes the `select_related` out, because taking one out feels risky in a way
that leaving it in does not.

The two costs are different and worth separating:

    an unused select_related       a JOIN on every row, and wider rows
    an unused prefetch_related     an entire extra query, plus the objects

`nplusone` detects this, at runtime, by watching which loaded objects are never
accessed. That needs the code path to actually run, so it covers what your
tests happen to exercise. Both halves are already known here statically: the
queryset says what it loads and the serializer says what it reads.

## When this stays quiet

A `SerializerMethodField` can touch anything, and so can a `to_representation`
override or a custom `list()`. Where one of those exists the finding is
reported at low confidence and hidden by default, because "delete this
`select_related`" is a destructive suggestion and a wrong one reintroduces the
N+1 the line was added to fix.
"""

from __future__ import annotations

from typing import Any

from .discovery import load_serializer_modules, load_view_modules
from .django_env import ensure_django

_OPAQUE_METHODS = ("list", "retrieve", "get_queryset", "to_representation")


def _covers(declared: str, used: set[str]) -> bool:
    """Is this declared path read by anything the serializer renders?

    `select_related("customer")` is earned by a read of `customer` or of
    anything below it, so `customer__country` counts. The reverse also counts:
    `prefetch_related("lines__product")` is earned by a read of `lines`,
    because the deeper path implies the shallower one was traversed.
    """
    for lookup in used:
        if lookup == declared:
            return True
        if lookup.startswith(f"{declared}__"):
            return True
        if declared.startswith(f"{lookup}__"):
            return True
    return False


def unused_eager_loading(include_low_confidence: bool = False) -> dict[str, Any]:
    """select_related and prefetch_related the serializer never reads.

    Args:
        include_low_confidence: also report views whose serializer has a
            SerializerMethodField, or which override list/retrieve/
            to_representation, where the relation may be read somewhere this
            cannot see.
    """
    config = ensure_django()

    try:
        # The import has to run, not merely resolve: DRF needs Django
        # configured, and find_spec would say yes to a broken install.
        import rest_framework  # noqa: F401
    except ModuleNotFoundError:
        return {
            "rest_framework_installed": False,
            "findings": [],
            "note": "djangorestframework is not importable; there are no querysets to check.",
        }

    load_serializer_modules(config.project_path)
    load_view_modules(config.project_path)

    from .endpoint_cost import _optimisations, _view_classes
    from .serializer_nplusone import serializer_nplusone

    report = serializer_nplusone()
    read_by: dict[str, set[str]] = {}
    method_fields: set[str] = set()
    for finding in report.get("findings", []):
        root = finding.get("root_serializer") or finding.get("serializer")
        if not root:
            continue
        if finding.get("type") == "SerializerMethodField":
            method_fields.add(root)
        lookup = finding.get("lookup")
        if lookup:
            read_by.setdefault(root, set()).add(lookup)
        else:
            # A finding with no relation path still proves the serializer was
            # examined; without recording it, an empty read set would make
            # every declared path look unused.
            read_by.setdefault(root, set())

    findings: list[dict[str, Any]] = []
    views_checked = 0
    skipped_dynamic = 0

    for cls in _view_classes():
        serializer_cls = getattr(cls, "serializer_class", None)
        if serializer_cls is None:
            continue
        views_checked += 1

        opt = _optimisations(cls)
        if opt.dynamic:
            # A path built at runtime means the declared set is unknown, and
            # calling something unused when the list is incomplete is the one
            # way this check could cause an N+1.
            skipped_dynamic += 1
            continue

        serializer_name = f"{serializer_cls.__module__}.{serializer_cls.__qualname__}"
        used = read_by.get(serializer_name, set())

        opaque = sorted(
            name for name in _OPAQUE_METHODS
            if name in cls.__dict__ or name in serializer_cls.__dict__
        )
        if serializer_name in method_fields:
            opaque.append("SerializerMethodField")
        confidence = "low" if opaque else "high"
        if confidence == "low" and not include_low_confidence:
            continue

        for kind, declared_paths, cost in (
            ("select_related", opt.select_related,
             "a JOIN on every row of every request, and every column of the "
             "joined table carried back with it"),
            ("prefetch_related", opt.prefetch_related,
             "one extra query per request, plus every object it returns held "
             "in memory for the response"),
        ):
            for declared in sorted(declared_paths):
                if _covers(declared, used):
                    continue
                if declared in opt.renamed_prefetches:
                    # Loaded under a to_attr name, so a read of the relation
                    # path proves nothing and its absence proves nothing.
                    continue
                findings.append({
                    "view": f"{cls.__module__}.{cls.__qualname__}",
                    "serializer": serializer_name,
                    "kind": kind,
                    "path": declared,
                    "confidence": confidence,
                    "unreadable_because": opaque,
                    "serializer_reads": sorted(used),
                    "severity": "medium" if kind == "prefetch_related" else "low",
                    "why": f"{serializer_name.rsplit('.', 1)[-1]} renders nothing under "
                           f"'{declared}', and it costs {cost}",
                    "fix": f"drop .{kind}(\"{declared}\") from the queryset, unless "
                           "something outside the serializer reads it",
                })

    order = {"medium": 0, "low": 1}
    findings.sort(key=lambda f: (f["confidence"] != "high", order.get(f["severity"], 9), f["view"]))

    return {
        "rest_framework_installed": True,
        "views_checked": views_checked,
        "views_with_runtime_paths": skipped_dynamic,
        "finding_count": len(findings),
        "high_confidence_count": sum(1 for f in findings if f["confidence"] == "high"),
        "findings": findings,
        "note": (
            "The opposite of an N+1: a relation the queryset loads and the "
            "serializer never reads. An unused select_related is a JOIN on "
            "every row; an unused prefetch_related is a whole extra query and "
            "the objects it returns. nplusone finds this at runtime, by "
            "watching which loaded objects go untouched, so it covers the paths "
            "the tests exercise; both halves are already in the source.\n\n"
            "A view whose serializer has a SerializerMethodField, or which "
            "overrides list, retrieve, get_queryset or to_representation, is "
            "reported only with include_low_confidence: those can read the "
            "relation somewhere this cannot see, and 'delete this "
            "select_related' is a destructive suggestion that reintroduces an "
            "N+1 when it is wrong. A queryset whose paths are built at runtime "
            "is skipped entirely and counted. A Prefetch(...) object names its "
            "path as a literal and is read normally, but one carrying to_attr "
            "loads the relation under another name, so it is passed over: a "
            "read of the relation path proves nothing there, and neither does "
            "its absence."
        ),
    }
