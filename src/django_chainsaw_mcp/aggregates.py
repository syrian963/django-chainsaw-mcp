# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Aggregates whose numbers are wrong because a join multiplied the rows.

    Order.objects.annotate(
        lines=Count("lines"),
        shipments=Count("shipments"),
    )

Both counts are wrong. Joining two multi-valued relations in one query gives
the cartesian product of them, so an order with 3 lines and 2 shipments
produces 6 rows: `lines` comes back as 6 and `shipments` as 6. No error, no
warning, two plausible-looking numbers that are both the product of the two.

This is documented in Django's own aggregation topic guide and it is one of the
most reliably re-encountered bugs in the ORM, because nothing about the code
looks wrong and the numbers only look wrong if somebody already knows the real
answer. It reaches production through a dashboard, and a dashboard is exactly
the place where a number nobody can check by hand goes unquestioned for years.

## What fixes it, and what does not

`Count(..., distinct=True)` collapses the duplicate rows and is correct. `Sum`
has no such option: the duplicates are real rows as far as it is concerned, and
the only correct spelling is a `Subquery` per aggregate. So a query with two
distinct-Counts is fine and the same query with one `Sum` in it is not, which is
not a distinction anybody remembers under deadline.

## Which aggregates a join actually breaks

Only `Count` and `Sum`. A join repeats each row uniformly within its group, so
`Min` and `Max` return the same value they would have anyway, and `Avg` divides
a multiplied total by a multiplied count. The first version of this check
included them and reported a correct `Min("items__begin")` on a real project as
a defect - the number was right. A check that flags a correct query is worse
than one that reports less.

## The same multiplication from a filter

    Order.objects.annotate(lines=Count("lines")).filter(shipments__carrier="dhl")

The filter joins a second multi-valued relation, and the count is multiplied by
however many shipments matched. Same mechanism, same silence, and it is
reported separately because the fix is different - the filter usually wants to
become a `Subquery` or an `Exists`.

## Prior art

Django's documentation warns about it. No linter checks for it: `flake8-django`
and ruff's `DJ` rules never resolve a field path against the model registry,
and nothing else reads which relations are multi-valued.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from . import querysets
from .django_env import ensure_django
from .project import parse_file, read_source

# Aggregate callables whose value a row multiplication actually corrupts.
#
# Not every aggregate is. A join repeats each row of one relation once per
# matching row of the other, uniformly within each group, and:
#
#   Count  counts the duplicates            -> wrong
#   Sum    adds each value once per copy    -> wrong
#   Min    the smallest value is unchanged  -> correct
#   Max    likewise                         -> correct
#   Avg    the same value repeated the same number of times has the same
#          mean                             -> correct
#
# The first version included Min, Max and Avg and reported a real
# `Min("items__begin")` beside a join on a second relation as a defect. The
# number was right. A check that flags a correct query is worse than one that
# reports less, so the set is the two that are genuinely wrong.
_AGGREGATES = {"Count", "Sum"}
# Only Count can be de-duplicated with distinct=True. The rest need a Subquery.
_FIXABLE_WITH_DISTINCT = {"Count"}

_LOOKUP_METHODS = {"filter", "exclude"}

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}


def _multi_valued_root(model: Any, path: str, cache: dict[tuple[str, str], str | None]) -> str | None:
    """The first segment of `path` if it crosses a multi-valued relation.

    `lines__product__name` starts at `lines`, and if that is a reverse foreign
    key or a many-to-many then every row of the parent is multiplied by it.
    A forward foreign key adds at most one row and cannot multiply anything.
    """
    root = path.split("__", 1)[0]
    key = (model._meta.label, root)
    if key not in cache:
        try:
            field = model._meta.get_field(root)
        except Exception:
            cache[key] = None
        else:
            multi = bool(getattr(field, "many_to_many", False)
                         or getattr(field, "one_to_many", False))
            cache[key] = root if multi else None
    return cache[key]


def _string_arg(call: ast.Call) -> str | None:
    for argument in call.args:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
    return None


def _is_distinct(call: ast.Call) -> bool:
    return any(
        keyword.arg == "distinct"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )


def _name_of(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _aggregates_in(call: ast.Call) -> list[tuple[str, str, bool]]:
    """(function, field path, distinct) for each aggregate handed to this call."""
    found: list[tuple[str, str, bool]] = []
    candidates = list(call.args) + [keyword.value for keyword in call.keywords]
    for node in candidates:
        if not isinstance(node, ast.Call):
            continue
        function = _name_of(node.func)
        if function not in _AGGREGATES:
            continue
        path = _string_arg(node)
        if path is None:
            continue
        found.append((function, path, _is_distinct(node)))
    return found


def multiplied_aggregates(search_path: str | None = None) -> dict[str, Any]:
    """Aggregates over more than one multi-valued relation in one query.

    Args:
        search_path: directory to scan. Defaults to the project path.
    """
    config = ensure_django()
    from django.apps import apps

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    by_class: dict[str, Any] = {}
    for model in apps.get_models():
        by_class.setdefault(model.__name__, model)

    relation_cache: dict[tuple[str, str], str | None] = {}
    findings: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    files_scanned = 0
    querysets_read = 0
    aggregates_seen = 0
    aggregates_in_source = 0
    annotate_calls_seen = 0
    model_names = set(by_class)

    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue

        files_scanned += 1
        lines = source.splitlines()
        relative = str(path.relative_to(root))
        reported: set[int] = set()

        # Locals and `self.model`, resolved per scope by `querysets` so that
        # two functions using `qs` for two different models stay separate.
        context = querysets.scopes(tree, model_names)

        for node in ast.walk(tree):
            # Only a chain that bottoms out at a name or at `self` can be
            # changed by the scope, and the lookup was being paid for every
            # call node in the project - hundreds of thousands of them,
            # almost none a queryset. That was 30 of the 51 seconds
            # `aggregates` took on a real codebase.
            names, class_models = (
                querysets.context_for(context, node)
                if querysets.needs_context(node) else ({}, {})
            )
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "annotate", "aggregate", "alias",
            }:
                aggregates_in_source += 1
            unwound = querysets.unwind(node, model_names, names, class_models)
            if unwound is None:
                continue
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "annotate", "aggregate", "alias",
            }:
                annotate_calls_seen += 1
            class_name, chain = unwound
            model = by_class.get(class_name)
            if model is None:
                continue
            querysets_read += 1

            # Aggregates anywhere in the chain, and the relations they cross.
            crossing: dict[str, list[tuple[str, str, bool]]] = {}
            for method, call in chain:
                if method not in {"annotate", "aggregate", "alias"}:
                    continue
                for function, field_path, distinct in _aggregates_in(call):
                    aggregates_seen += 1
                    root_name = _multi_valued_root(model, field_path, relation_cache)
                    if root_name is None:
                        continue
                    crossing.setdefault(root_name, []).append(
                        (function, field_path, distinct)
                    )

            if not crossing or node.lineno in reported:
                continue

            code = lines[node.lineno - 1].strip()[:140] if node.lineno <= len(lines) else ""

            if len(crossing) > 1:
                # Two multi-valued relations joined in one query. Every Count
                # that is not distinct, and every Sum/Avg/Min/Max at all, is
                # multiplied by the other relation's row count.
                unfixed = [
                    (function, field_path)
                    for entries in crossing.values()
                    for function, field_path, distinct in entries
                    if not (function in _FIXABLE_WITH_DISTINCT and distinct)
                ]
                if unfixed:
                    reported.add(node.lineno)
                    needs_subquery = sorted(
                        {f for f, _ in unfixed if f not in _FIXABLE_WITH_DISTINCT}
                    )
                    findings.append({
                        "file": relative,
                        "line": node.lineno,
                        "code": code,
                        "model": model._meta.label,
                        "relations": sorted(crossing),
                        "aggregates": [f"{f}({p})" for f, p in unfixed],
                        "severity": "high",
                        "why": (
                            f"this query joins {len(crossing)} multi-valued "
                            f"relations ({', '.join(sorted(crossing))}), so the "
                            "rows are the cartesian product of them and each "
                            "aggregate counts the other one's rows as well. No "
                            "error is raised and every Count and Sum returned "
                            "is multiplied by the other relation rows"
                        ),
                        "fix": "; ".join(
                            part for part in (
                                ("add distinct=True to each Count"
                                 if any(f in _FIXABLE_WITH_DISTINCT
                                        for f, _ in unfixed) else ""),
                                (f"{' and '.join(needs_subquery)} cannot be "
                                 "de-duplicated that way and needs its own "
                                 "Subquery with OuterRef and an empty order_by"
                                 if needs_subquery else ""),
                            ) if part
                        ),
                    })
                    continue

            # One aggregate, and a filter that joins a different multi-valued
            # relation: the same multiplication, arriving from the other side.
            joined_by_filter: set[str] = set()
            for method, call in chain:
                if method not in _LOOKUP_METHODS:
                    continue
                for keyword in call.keywords:
                    if not keyword.arg:
                        continue
                    root_name = _multi_valued_root(model, keyword.arg, relation_cache)
                    if root_name is not None and root_name not in crossing:
                        joined_by_filter.add(root_name)

            unfixed = [
                (function, field_path)
                for entries in crossing.values()
                for function, field_path, distinct in entries
                if not (function in _FIXABLE_WITH_DISTINCT and distinct)
            ]
            if joined_by_filter and unfixed:
                reported.add(node.lineno)
                filtered.append({
                    "file": relative,
                    "line": node.lineno,
                    "code": code,
                    "model": model._meta.label,
                    "aggregated": sorted(crossing),
                    "filtered_on": sorted(joined_by_filter),
                    "aggregates": [f"{f}({p})" for f, p in unfixed],
                    "severity": "high",
                    "why": (
                        f"the aggregate crosses {', '.join(sorted(crossing))} "
                        f"and the filter joins {', '.join(sorted(joined_by_filter))}, "
                        "which is a second multi-valued relation. The join "
                        "multiplies the rows the aggregate is counting"
                    ),
                    "fix": (
                        "add distinct=True to each Count, or move the condition "
                        "into an Exists()/Subquery so it does not join"
                    ),
                })

    findings.sort(key=lambda f: (f["file"], f["line"]))
    filtered.sort(key=lambda f: (f["file"], f["line"]))

    return {
        "findings": findings,
        "finding_count": len(findings),
        "multiplied_by_a_filter": filtered,
        "filter_count": len(filtered),
        "querysets_read": querysets_read,
        "aggregates_seen": aggregates_seen,
        "annotate_calls_in_source": aggregates_in_source,
        # How many of those a model could be resolved for. `querysets_read`
        # counts every chain, not only the annotating ones, so it is the wrong
        # denominator for this question.
        "annotate_calls_seen": annotate_calls_seen,
        "files_scanned": files_scanned,
        "note": (
            "Aggregating across two multi-valued relations in one query "
            "returns the cartesian product of them, so every number is the "
            "product of both. Django's own documentation warns about it and "
            "nothing raises. Count(distinct=True) fixes a Count and is treated "
            "as correct here; Sum, Avg, Min and Max have no equivalent and "
            "need a Subquery, so a query mixing them is reported even when "
            "every Count in it is distinct. Only paths that resolve to a "
            "many-to-many or a reverse foreign key count as multi-valued: a "
            "forward foreign key adds at most one row and cannot multiply "
            "anything. A path that does not resolve to a field - an annotation "
            "alias, a transform - is skipped rather than guessed at."
        ),
    }
