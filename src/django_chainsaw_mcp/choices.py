# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""String literals compared against a field whose `choices` do not contain them.

    STATUS = [("canceled", "Canceled"), ("shipped", "Shipped")]
    ...
    Order.objects.filter(status="cancelled")

Two Ls. The query is valid SQL, returns zero rows, raises nothing, and is
wrong forever. Nothing in Django objects: `choices` is validated by
`full_clean()`, which a queryset never calls and `create()` never calls either,
so the write side is worse than the read side - `create(status="cancelled")`
puts a value in the column that the application does not believe exists.

The failure is silent in the way that matters: an empty result looks exactly
like "no orders are cancelled", which is a sentence a reviewer will read past
and a test will agree with unless it happens to cover that branch with real
data.

## Why no existing tool finds it

`django-stubs` types a `CharField` as `str`, not as a `Literal` union of its
choices, so mypy sees `filter(status=str)` and is satisfied. `flake8-django`
and ruff's `DJ` rules do not read the model registry at all. Packages like
`django-choices-field` fix it going forward by making the field an enum -
which is the right answer for new code and does nothing about the literals
already in the codebase.

## What was tried and thrown away

`order.status == "cancelled"` names no model, and the obvious move is to judge
the literal against every model that has a field of that name, reporting it
only when none of them allows it. That was built, and then measured against a
real codebase of 424 models: **five findings, five false positives.** Every one
was an attribute on an object that is not a model at all - a plain `Header`
class holding a string, and `datetime.date.month`.

Tightening it to require every same-named field to carry choices took it from
59 findings to 5 and did not change the ratio. There is no rule available here
that decides whether `obj.status` is a model field, so the route is gone rather
than shipped behind a caveat. What remains only ever names a model, because the
model is named in the code.

"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from . import querysets
from .django_env import ensure_django
from .project import parse_file, read_source

# Keyword arguments to these carry field values.
_QUERY_METHODS = {"filter", "exclude", "get", "get_or_create", "update_or_create"}
_WRITE_METHODS = {"create", "update", "bulk_create", "get_or_create", "update_or_create"}

# Lookups whose right-hand side is compared for equality against the column.
# `icontains` matches a substring and `iexact` ignores case, so a literal that
# differs from every choice can still legitimately match one; neither is
# evidence of a typo.
_EXACT = {None, "exact", "in"}

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
    # Historical models in a migration legitimately carry the choices of their
    # own time, and a data migration rewriting an old value to a new one has
    # to name the old one.
    "migrations",
}


def _split(key: str) -> tuple[str, str | None]:
    """`status__in` -> ("status", "in"); `status` -> ("status", None)."""
    head, _, tail = key.rpartition("__")
    if not head:
        return key, None
    return (head, tail) if tail in {"exact", "in", "iexact", "contains",
                                    "icontains", "startswith", "gt", "gte",
                                    "lt", "lte", "isnull", "regex"} else (key, None)


def _choices_of(model: Any) -> dict[str, set[Any]]:
    """Field name -> the values its `choices` allow, flattened past any groups."""
    out: dict[str, set[Any]] = {}
    for field in model._meta.concrete_fields:
        raw = getattr(field, "flatchoices", None) or getattr(field, "choices", None)
        if not raw:
            continue
        values = {value for value, _label in raw}
        if values:
            out[field.name] = values
            if field.attname != field.name:
                out[field.attname] = values
    return out


def _literals(node: ast.AST) -> list[Any] | None:
    """The literal value(s) in this node, or None if it is not literal.

    A name, an attribute (`Status.CANCELLED`) or a call is not a literal, and
    those are the spellings that cannot go wrong in the first place.
    """
    if isinstance(node, ast.Constant):
        return None if node.value is None or isinstance(node.value, bool) else [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        out = []
        for element in node.elts:
            if not isinstance(element, ast.Constant):
                return None
            out.append(element.value)
        return out or None
    return None


def _unwind(
    call: ast.Call,
    models: set[str],
    locals_: dict[str, str],
    class_models: dict[str, str],
) -> tuple[str, list[tuple[str, str, ast.AST]]] | None:
    """A queryset chain as (model class name, [(method, key, value node)]).

    The receiver is resolved by `querysets`, which understands a custom
    manager, a local variable and `self.model` as well as `Model.objects`.
    """
    unwound = querysets.unwind(call, models, locals_, class_models)
    if unwound is None:
        return None
    model, chain = unwound
    used: list[tuple[str, str, ast.AST]] = []
    for method, node in chain:
        if method not in _QUERY_METHODS | _WRITE_METHODS:
            continue
        for keyword in node.keywords:
            if keyword.arg:
                used.append((method, keyword.arg, keyword.value))
    return (model, used) if used else None


def _constructor(call: ast.Call) -> tuple[str, list[tuple[str, str, ast.AST]]] | None:
    """`Order(status="...")` - a model instantiated directly."""
    if not isinstance(call.func, ast.Name):
        return None
    used = [
        ("__init__", keyword.arg, keyword.value)
        for keyword in call.keywords
        if keyword.arg
    ]
    return (call.func.id, used) if used else None


def _line(lines: list[str], number: int) -> str:
    return lines[number - 1].strip()[:140] if 0 < number <= len(lines) else ""


def choice_typos(
    search_path: str | None = None,
    include_tests: bool = True,
) -> dict[str, Any]:
    """Literals that no `choices` on the field will ever match.

    Args:
        search_path: directory to scan. Defaults to the project path.
        include_tests: also scan test files. A test asserting on a misspelled
            status is the same defect wearing a different hat.
    """
    config = ensure_django()
    from django.apps import apps

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    by_class: dict[str, Any] = {}
    for model in apps.get_models():
        by_class.setdefault(model.__name__, model)
    model_names = set(by_class)

    choices: dict[str, dict[str, set[Any]]] = {}
    fields_with_choices = 0
    for name, model in by_class.items():
        found = _choices_of(model)
        if found:
            choices[name] = found
            fields_with_choices += len(found)

    findings: list[dict[str, Any]] = []
    files_scanned = 0
    literals_checked = 0

    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not include_tests and ("test" in path.name or "tests" in path.parts):
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue

        files_scanned += 1
        lines = source.splitlines()
        relative = str(path.relative_to(root))

        context = querysets.scopes(tree, model_names)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # Only a chain that bottoms out at a name or at `self` can be
                # changed by the scope, and the lookup was being paid for every
                # call node in the project - hundreds of thousands of them,
                # almost none a queryset. That was 30 of the 51 seconds
                # `aggregates` took on a real codebase.
                names, class_models = (
                    querysets.context_for(context, node)
                    if querysets.needs_context(node) else ({}, {})
                )
                result = (_unwind(node, model_names, names, class_models)
                          or _constructor(node))
                if result is None:
                    continue
                class_name, used = result
                allowed = choices.get(class_name)
                model = by_class.get(class_name)
                if not allowed or model is None:
                    continue

                for method, key, value_node in used:
                    field, lookup = _split(key)
                    if field not in allowed or lookup not in _EXACT:
                        continue
                    values = _literals(value_node)
                    if values is None:
                        continue
                    literals_checked += len(values)
                    # Django coerces the value to the field's type before it
                    # reaches the database, so `filter(currency="1")` on an
                    # IntegerField whose choices are 1 and 2 is correct code.
                    # Comparing the string forms is what actually happens at
                    # runtime; comparing the objects reported it as a typo.
                    permitted = {str(v) for v in allowed[field]}
                    wrong = [v for v in values if str(v) not in permitted]
                    if not wrong:
                        continue
                    writes = method in _WRITE_METHODS or method == "__init__"
                    findings.append({
                        "file": relative,
                        "line": node.lineno,
                        "code": _line(lines, node.lineno),
                        "model": model._meta.label,
                        "field": field,
                        "value": wrong[0] if len(wrong) == 1 else wrong,
                        "allowed": sorted(map(str, allowed[field]))[:20],
                        "method": method,
                        "severity": "high",
                        "why": (
                            f"{model._meta.label}.{field} allows "
                            f"{sorted(map(str, allowed[field]))[:6]} and this "
                            f"{'writes' if writes else 'compares against'} "
                            f"{wrong[0]!r}"
                            + (
                                ". create() and update() never call full_clean(), "
                                "so the value goes into the column and the "
                                "application does not believe it exists"
                                if writes else
                                ". The query is valid SQL, returns zero rows and "
                                "raises nothing"
                            )
                        ),
                        "fix": (
                            "correct the literal, or reference the choice by "
                            "name so the next typo is an AttributeError"
                        ),
                    })

    # `filter(status="draft").update(status="placed")` is one chain, and
    # ast.walk hands it over once per call in it, so each keyword was unwound
    # as many times as there were links to its left.
    seen: set[tuple[Any, ...]] = set()
    deduped = []
    for finding in findings:
        key = (finding["file"], finding["line"], finding["field"],
               str(finding["value"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(finding)
    findings = deduped

    findings.sort(key=lambda f: (f["file"], f["line"]))

    return {
        "findings": findings,
        "finding_count": len(findings),
        "models_with_choices": len(choices),
        "fields_with_choices": fields_with_choices,
        "literals_checked": literals_checked,
        "files_scanned": files_scanned,
        "note": (
            "A literal compared against a field whose choices do not contain "
            "it. The read side returns zero rows and raises nothing; the write "
            "side is worse, because create() and update() never call "
            "full_clean() and the value ends up in the column. Only literals "
            "are checked: a name or an enum member is the spelling that cannot "
            "go wrong. Only equality and `in` are checked, because `iexact` "
            "and `icontains` can legitimately match a value that is spelled "
            "differently. Migrations are skipped, since a historical model "
            "carries the choices of its own time and a data migration has to "
            "name the value it is replacing. A comparison like "
            "Only expressions that name a model are checked. Judging "
            "`obj.status == '...'` against every model with a `status` field "
            "was built and measured on a real codebase: five findings, five "
            "false positives, every one an attribute on something that is not "
            "a model. Nothing decides that statically, so it is not here."
        ),
    }
