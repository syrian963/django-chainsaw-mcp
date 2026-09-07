# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Find relation traversals in templates that will each cost a query.

The classic N+1 in Django is a template looping over a queryset and reaching
across a relation inside the loop: for one hundred orders, `{{ order.customer }}`
is one hundred extra queries unless the queryset says select_related.

Attribute chains are resolved against the real model graph while walking the
parsed template, so loop variables carry a model type rather than a string.
That is what makes nested loops work: `{% for line in order.lines.all %}` binds
`line` to the element model of the reverse relation, and the traversals inside
that inner loop are resolved from OrderLine, not from Order.

It reports *candidates*: whether a crossing actually costs a query depends on
the queryset that feeds the template, which lives in the view.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .django_env import ensure_django

# Manager and queryset calls that appear in templates and are not model fields.
_MANAGER_SUFFIXES = {"all", "count", "first", "last", "exists"}


def _strip_manager_call(parts: list[str]) -> list[str]:
    return parts[:-1] if parts and parts[-1] in _MANAGER_SUFFIXES else parts


def _resolve(model: Any, parts: list[str]) -> dict[str, Any] | None:
    """Follow an attribute chain from a model across the graph.

    Returns the relation hops taken and the model the chain ends on, or None if
    the chain never leaves the current model (a plain field, a property, a
    method, an annotation).
    """
    hops: list[dict[str, Any]] = []
    current = model

    for index, part in enumerate(parts):
        try:
            field = current._meta.get_field(part)
        except Exception:
            break

        if not field.is_relation:
            break

        target = field.related_model
        if target is None:
            break

        many = bool(field.many_to_many or field.one_to_many)
        hops.append(
            {
                "step": part,
                "to": target._meta.label,
                "multi": many,
                "prefetch_required": many,
                "position": index,
            }
        )
        current = target

    if not hops:
        return None
    return {"hops": hops, "ends_on": current}


def _record(
    findings: list[dict[str, Any]],
    expression: str,
    model: Any,
    chain: dict[str, Any],
    inside_loop: bool,
) -> None:
    needs_prefetch = any(hop["prefetch_required"] for hop in chain["hops"])
    lookup = "__".join(hop["step"] for hop in chain["hops"])
    findings.append(
        {
            "expression": expression,
            "root_model": model._meta.label,
            "inside_loop": inside_loop,
            "relation_hops": chain["hops"],
            "lookup": lookup,
            "suggested": (
                f"prefetch_related('{lookup}')" if needs_prefetch else f"select_related('{lookup}')"
            ),
            "severity": "high" if inside_loop else "low",
            "why": (
                "crosses a relation inside a loop: one query per row unless prefetched"
                if inside_loop
                else "crosses a relation outside a loop: one extra query"
            ),
        }
    )


def _walk(
    nodelist: Any,
    scope: dict[str, Any],
    in_loop: bool,
    findings: list[dict[str, Any]],
    depth: int = 0,
    seen: frozenset[str] = frozenset(),
) -> None:
    from django.template.base import VariableNode
    from django.template.defaulttags import ForNode
    from django.template.loader_tags import IncludeNode

    for node in nodelist:
        if isinstance(node, VariableNode):
            expression = str(node.filter_expression.var)
            parts = _strip_manager_call(expression.split("."))
            if len(parts) >= 2 and parts[0] in scope:
                chain = _resolve(scope[parts[0]], parts[1:])
                if chain:
                    _record(findings, expression, scope[parts[0]], chain, in_loop)
            continue

        if isinstance(node, ForNode):
            sequence = str(node.sequence.var) if hasattr(node.sequence, "var") else ""
            parts = _strip_manager_call(sequence.split("."))
            element_model = None

            if len(parts) >= 2 and parts[0] in scope:
                chain = _resolve(scope[parts[0]], parts[1:])
                if chain:
                    # Iterating a relation is itself a traversal, and this is
                    # the one that usually needs prefetch_related.
                    _record(findings, sequence, scope[parts[0]], chain, in_loop)
                    element_model = chain["ends_on"]
            elif len(parts) == 1 and parts[0] in scope:
                element_model = scope[parts[0]]

            inner = dict(scope)
            if element_model is not None:
                for target in node.loopvars:
                    inner[target] = element_model

            for child_list in node.child_nodelists:
                child = getattr(node, child_list, None)
                if child:
                    _walk(child, inner, True, findings, depth, seen)
            continue

        if isinstance(node, IncludeNode):
            # A partial rendered inside a loop is the N+1 nobody sees: the
            # loop is in one file and the relation traversal is in another,
            # and neither file is suspicious on its own.
            _walk_include(node, scope, in_loop, findings, depth, seen)
            continue

        for child_list in getattr(node, "child_nodelists", ()):
            child = getattr(node, child_list, None)
            if child:
                _walk(child, scope, in_loop, findings, depth, seen)


_MAX_INCLUDE_DEPTH = 5

# Directories to try when the configured engine cannot find an included
# template. A project with a misconfigured or empty TEMPLATES setting would
# otherwise follow no includes at all and say nothing about it.
_SEARCH_DIRS: list[Path] = []


def set_include_search_dirs(dirs: list[Path]) -> None:
    """Where to look for an included template the engine could not resolve."""
    global _SEARCH_DIRS
    _SEARCH_DIRS = [d for d in dirs if d.is_dir()]


def _load_included(name: str) -> Any:
    try:
        return _project_engine().get_template(name)
    except Exception:
        pass
    for directory in _SEARCH_DIRS:
        candidate = directory / name
        if candidate.is_file():
            try:
                return _project_engine().from_string(
                    candidate.read_text(encoding="utf-8", errors="replace")
                )
            except Exception:
                return None
    return None


def _walk_include(
    node: Any,
    scope: dict[str, Any],
    in_loop: bool,
    findings: list[dict[str, Any]],
    depth: int,
    seen: frozenset[str],
) -> None:
    """Follow `{% include "partial.html" %}` with the caller's context.

    Only a literal template name can be followed; `{% include chosen %}`
    picks its target at runtime. `{% include ... only %}` gets an empty
    scope, because that is exactly what `only` means, and `with x=y` rebinds
    what it names.
    """
    from django.template.base import Variable

    if depth >= _MAX_INCLUDE_DEPTH:
        return
    try:
        name = node.template.var
    except AttributeError:
        return
    if not isinstance(name, str):
        # A FilterExpression whose var is a Variable resolves at runtime.
        return
    if name in seen:
        # A partial that includes itself, directly or in a ring.
        return

    inner = {} if getattr(node, "isolated_context", False) else dict(scope)
    for key, value in (getattr(node, "extra_context", None) or {}).items():
        source = getattr(value, "var", None)
        if isinstance(source, Variable):
            parts = _strip_manager_call(str(source).split("."))
            if len(parts) == 1 and parts[0] in scope:
                inner[key] = scope[parts[0]]
            elif len(parts) >= 2 and parts[0] in scope:
                chain = _resolve(scope[parts[0]], parts[1:])
                if chain:
                    inner[key] = chain["ends_on"]

    if not inner:
        return

    included = _load_included(name)
    if included is None:
        return

    nodelist = getattr(included, "nodelist", None)
    if nodelist is None:
        nodelist = getattr(getattr(included, "template", None), "nodelist", None)
    if nodelist is None:
        return

    _walk(nodelist, inner, in_loop, findings, depth + 1, seen | {name})


def _project_engine() -> Any:
    """The engine the project actually renders with.

    A bare `Engine()` has no `libraries` and no `DIRS`, so the first
    `{% load app_tags %}` raises TemplateSyntaxError and the first
    `{% extends %}` cannot find its parent. Every project of any size has a
    custom tag library, which made this analysis useless on all of them while
    passing on a demo that has none.
    """
    from django.template import Engine
    from django.template.backends.django import DjangoTemplates
    from django.template.loader import engines

    for backend in engines.all():
        if isinstance(backend, DjangoTemplates):
            return backend.engine
    try:
        return Engine.get_default()
    except Exception:
        return Engine(debug=False)


def analyse_template(template_path: str, root_models: dict[str, str]) -> dict[str, Any]:
    """Report relation crossings in one template.

    Args:
        template_path: path to the template file.
        root_models: maps a context variable to a model label, e.g.
            {"orders": "shop.Order"}. A name bound here is treated as the model
            itself when used directly, and as the element type when looped over.
    """
    ensure_django()
    from django.apps import apps

    path = Path(template_path).expanduser()
    if not path.is_file():
        raise ValueError(f"Template not found: {path}")

    scope: dict[str, Any] = {}
    for name, label in root_models.items():
        try:
            scope[name] = apps.get_model(label)
        except (LookupError, ValueError) as exc:
            raise ValueError(f"Unknown model '{label}' for context variable '{name}'") from exc

    template = _project_engine().from_string(path.read_text(encoding="utf-8"))

    findings: list[dict[str, Any]] = []
    _walk(template.nodelist, scope, False, findings)

    # The same expression can appear more than once in a template.
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for finding in findings:
        key = (finding["expression"], finding["severity"])
        unique.setdefault(key, finding)
    findings = sorted(unique.values(), key=lambda f: (f["severity"] != "high", f["expression"]))

    high = [f for f in findings if f["severity"] == "high"]
    select_related = sorted(
        {f["lookup"] for f in high if f["suggested"].startswith("select_related")}
    )
    prefetch_related = sorted(
        {f["lookup"] for f in high if f["suggested"].startswith("prefetch_related")}
    )

    return {
        "template": str(path),
        "candidate_count": len(findings),
        "high_severity_count": len(high),
        "findings": findings,
        "suggested_queryset": {
            "select_related": select_related,
            "prefetch_related": prefetch_related,
        },
        "note": (
            "These are candidates, not confirmed N+1 queries. Whether a "
            "crossing costs a query depends on the queryset in the view: if it "
            "already uses select_related or prefetch_related for that path, "
            "there is no extra query. This tool reads the template and the "
            "model graph, not the view."
        ),
    }
