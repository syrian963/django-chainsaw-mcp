# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""N+1 queries in DRF serializers, which is where they live in an API project.

The template checker answers the same question for server-rendered pages. Most
Django written today renders JSON, and there the N+1 comes from a serializer
field that crosses a relation:

    class OrderSerializer(ModelSerializer):
        customer = CustomerSerializer()          # one query per order
        lines = LineSerializer(many=True)        # one query per order
        total = SerializerMethodField()          # whatever it does, per order

A list endpoint returning a hundred orders runs a hundred extra queries per
nested field, and nothing on the page says so. The fix is the same shape as in
templates, `select_related` for forward single relations and
`prefetch_related` for the rest, but it belongs on the view's queryset, which
is a different file from the one with the problem.

Serializers are inspected as live classes rather than parsed, because DRF has
already resolved inheritance, `Meta.fields`, and declared fields by the time
the app registry is populated. Reading the source would mean reimplementing
that.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from .discovery import load_serializer_modules
from .django_env import ensure_django


def _location(cls: Any) -> str | None:
    try:
        source_file = inspect.getsourcefile(cls)
        _, line = inspect.getsourcelines(cls)
    except (OSError, TypeError):
        return None
    return f"{Path(source_file).name}:{line}" if source_file else None


def _relation_for(model: Any, name: str) -> dict[str, Any] | None:
    """Is this serializer field name a relation on the model?"""
    try:
        field = model._meta.get_field(name)
    except Exception:
        return None
    if not field.is_relation or field.related_model is None:
        return None
    many = bool(field.many_to_many or field.one_to_many)
    return {
        "to": field.related_model._meta.label,
        "kind": (
            "ManyToMany" if field.many_to_many
            else "OneToMany" if field.one_to_many
            else "OneToOne" if field.one_to_one
            else "ManyToOne"
        ),
        "many": many,
        "hint": "prefetch_related" if many else "select_related",
    }


def _walk_serializer(cls: Any, depth: int, max_depth: int, seen: set[int]) -> list[dict[str, Any]]:
    """Relation-crossing fields on one serializer, following nested ones."""
    from rest_framework import serializers as drf

    if depth > max_depth or id(cls) in seen:
        return []
    seen.add(id(cls))

    meta = getattr(cls, "Meta", None)
    model = getattr(meta, "model", None)
    if model is None:
        return []

    try:
        fields = cls().get_fields()
    except Exception as exc:  # a serializer that needs context to instantiate
        return [
            {
                "serializer": f"{cls.__module__}.{cls.__qualname__}",
                "unreadable": f"{type(exc).__name__}: {exc}",
                "depth": depth,
            }
        ]

    out: list[dict[str, Any]] = []

    for name, field in fields.items():
        source = getattr(field, "source", None) or name
        entry_base = {
            "serializer": f"{cls.__module__}.{cls.__qualname__}",
            "model": model._meta.label,
            "field": name,
            "source": source,
            "depth": depth,
            "location": _location(cls),
        }

        # A method field can do anything, including a query per row, and there
        # is no way to know from here.
        if isinstance(field, drf.SerializerMethodField):
            out.append(
                {
                    **entry_base,
                    "type": "SerializerMethodField",
                    "severity": "medium",
                    "why": (
                        "a method field runs once per object; if it touches a "
                        "relation or queries, that is one query per row and this "
                        "cannot see inside it"
                    ),
                    "suggested": "check the method body, and prefetch what it reads",
                }
            )
            continue

        relation = _relation_for(model, source)
        if relation is None:
            continue

        nested = getattr(field, "child", None) or field
        nested_class = type(nested)
        is_nested_serializer = isinstance(nested, drf.BaseSerializer)

        lookup = source
        out.append(
            {
                **entry_base,
                "type": type(field).__name__,
                "relation": relation,
                "lookup": lookup,
                "severity": "high" if relation["many"] else "medium",
                "why": (
                    f"{relation['kind']} to {relation['to']}: one query per object "
                    "in a list response unless the view's queryset prefetches it"
                ),
                "suggested": f"{relation['hint']}('{lookup}')",
            }
        )

        if is_nested_serializer and getattr(nested_class, "Meta", None) is not None:
            for child in _walk_serializer(nested_class, depth + 1, max_depth, seen):
                if "lookup" in child:
                    child["lookup"] = f"{lookup}__{child['lookup']}"
                    child["suggested"] = (
                        f"{child['relation']['hint']}('{child['lookup']}')"
                        if child.get("relation") else child.get("suggested")
                    )
                child["via"] = f"{entry_base['serializer']}.{name}"
                out.append(child)

    return out


def serializer_nplusone(max_depth: int = 3) -> dict[str, Any]:
    """Relation crossings in DRF serializers, with the queryset fix for each.

    Args:
        max_depth: how far to follow nested serializers.
    """
    config = ensure_django()
    # Django imports models.py, admin.py and whatever the URLconf
    # reaches. A serializer outside that is real, served, and was
    # previously invisible to a __subclasses__() walk.
    discovered = load_serializer_modules(config.project_path)

    try:
        from rest_framework import serializers as drf
    except ModuleNotFoundError:
        return {
            "rest_framework_installed": False,
            "findings": [],
            "note": (
                "djangorestframework is not importable from this interpreter. "
                "If the project uses DRF, the server is running in the wrong "
                "environment; see docs/usage.md."
            ),
        }

    roots: list[Any] = []
    seen_classes: set[int] = set()

    def collect(cls: Any) -> None:
        for subclass in cls.__subclasses__():
            if id(subclass) in seen_classes:
                continue
            seen_classes.add(id(subclass))
            roots.append(subclass)
            collect(subclass)

    collect(drf.ModelSerializer)

    findings: list[dict[str, Any]] = []
    unreadable: list[dict[str, Any]] = []

    for cls in roots:
        root_name = f"{cls.__module__}.{cls.__qualname__}"
        for entry in _walk_serializer(cls, 0, max_depth, set()):
            entry.setdefault("root_serializer", root_name)
            if entry.get("unreadable"):
                unreadable.append(entry)
            else:
                findings.append(entry)

    # Group the queryset advice per serializer, which is the form somebody can
    # actually paste into a view.
    advice: dict[str, dict[str, list[str]]] = {}
    for finding in findings:
        if not finding.get("relation"):
            continue
        root = finding.get("root_serializer") or finding["serializer"]
        bucket = advice.setdefault(root, {"select_related": [], "prefetch_related": []})
        target = bucket[finding["relation"]["hint"]]
        if finding["lookup"] not in target:
            target.append(finding["lookup"])

    for bucket in advice.values():
        bucket["select_related"].sort()
        bucket["prefetch_related"].sort()

    findings.sort(key=lambda f: (f["severity"] != "high", f["serializer"], f["field"]))
    return {
        "rest_framework_installed": True,
        "serializer_count": len(roots),
        "finding_count": len(findings),
        "high_severity_count": sum(1 for f in findings if f["severity"] == "high"),
        "findings": findings,
        "queryset_advice": advice,
        "unreadable_serializers": unreadable,
        "note": (
            "Candidates. Whether a crossing costs a query depends on the "
            "queryset in the view, which this does not read: a view that "
            "already prefetches the path has no problem. Serializers in modules "
            "nothing imports at startup are invisible, and a SerializerMethodField "
            "is reported without knowing what it does, because there is no way "
            "to know from here."
        ),
    }
