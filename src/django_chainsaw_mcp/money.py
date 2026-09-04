# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Money that stops being exact.

A `DecimalField` exists so that 0.1 + 0.2 is 0.3. Every route out of `Decimal`
and back gives that up, and the loss is a fraction of a cent, so it survives
code review, passes the tests, and turns up as an invoice total that is one
cent off a sum nobody can reproduce.

There are four ways out, and they are not equally bad:

    Decimal(0.1)                     wrong from birth: 0.1 has no exact binary
                                     form, so this is 0.1000000000000000055...
    float(invoice.amount)            a one-way door; every later operation is
                                     approximate
    round(amount, 2)                 exact, but banker's rounding - 0.125
                                     rounds to 0.12, and an invoice expects 0.13
    FloatField(name="price")         the column itself cannot hold money

`Decimal(0.0)` and `Decimal(0.5)` are **fine**: those floats are exactly
representable, so nothing is lost. A check that flags every `Decimal(<float>)`
is wrong about most of them, so this one computes whether the value survives
the round trip and says which case it is.

Nothing in the linter ecosystem looks at this. The advice is everywhere - use
DecimalField for money - and the tooling stops at the model definition, while
every one of these happens somewhere else.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from typing import Any

from .django_env import ensure_django

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
}

# Names that mean money in the projects this is aimed at, English and German.
# Only used for the FloatField check, where there is no Decimal to key off.
_MONEY_WORDS = (
    "price", "amount", "total", "cost", "fee", "betrag", "preis", "brutto",
    "netto", "balance", "tax", "discount", "commission", "margin", "revenue",
    "payment", "salary", "surcharge", "deposit", "refund",
)


def _exactly_representable(value: float) -> bool:
    """Does this float hold exactly the decimal number that was written?

    `Decimal(0.5)` is exactly `0.5`. `Decimal(0.1)` is
    `0.1000000000000000055511151231257827021181583404541015625`.
    """
    return Decimal(value) == Decimal(repr(value))


def _decimal_field_names() -> set[str]:
    """Every field name that is a DecimalField on any model in the project.

    Resolving what `invoice.amount` refers to would need type inference. This
    is the cheaper substitute: if `amount` is a DecimalField somewhere, then
    `float(x.amount)` is money often enough to be worth a look, and the output
    says which model made the name count.
    """
    from django.apps import apps
    from django.db import models

    names: set[str] = set()
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            if isinstance(field, models.DecimalField):
                names.add(field.name)
    return names


def _trailing_attribute(node: ast.AST) -> str | None:
    """`self.invoice.amount` -> "amount"; a bare name or a call -> None."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, decimal_names: set[str]) -> None:
        self.decimal_names = decimal_names
        self.findings: list[dict[str, Any]] = []

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")

        if name == "Decimal" and node.args:
            self._decimal_from_float(node)
        elif name == "float" and node.args:
            self._float_of_money(node)
        elif name == "round" and node.args:
            self._round_of_money(node)

        self.generic_visit(node)

    def _decimal_from_float(self, node: ast.Call) -> None:
        argument = node.args[0]
        literal = None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, float):
            literal = argument.value
        elif isinstance(argument, ast.UnaryOp) and isinstance(argument.op, ast.USub):
            inner = argument.operand
            if isinstance(inner, ast.Constant) and isinstance(inner.value, float):
                literal = -inner.value

        if literal is None:
            if isinstance(argument, ast.Call) and getattr(argument.func, "id", "") == "float":
                self.findings.append({
                    "kind": "decimal_from_float_call",
                    "line": node.lineno,
                    "severity": "high",
                    "detail": "Decimal(float(...)) throws the precision away and then asks for it back",
                    "fix": "keep the value a Decimal, or build it from a string: Decimal(str(value))",
                })
            return

        if _exactly_representable(literal):
            self.findings.append({
                "kind": "decimal_from_exact_float",
                "line": node.lineno,
                "severity": "low",
                "detail": f"Decimal({literal!r}) happens to be exact, so nothing is lost here",
                "fix": f'Decimal("{literal}") anyway, so the next edit to this literal is safe',
            })
        else:
            self.findings.append({
                "kind": "decimal_from_inexact_float",
                "line": node.lineno,
                "severity": "high",
                "detail": (
                    f"Decimal({literal!r}) is actually {Decimal(literal)}, because "
                    f"{literal!r} has no exact binary form. It is wrong before "
                    "anything is done with it"
                ),
                "fix": f'Decimal("{literal}")',
            })

    def _float_of_money(self, node: ast.Call) -> None:
        field = _trailing_attribute(node.args[0])
        if field is None or field not in self.decimal_names:
            return
        self.findings.append({
            "kind": "float_of_decimal",
            "line": node.lineno,
            "field": field,
            "severity": "high",
            "detail": (
                f"'{field}' is a DecimalField, and float() is a one-way door: "
                "every operation after this one is approximate"
            ),
            "fix": "keep it a Decimal; if a float is genuinely needed, convert at "
                   "the last possible moment and never convert back",
        })

    def _round_of_money(self, node: ast.Call) -> None:
        field = _trailing_attribute(node.args[0])
        if field is None or field not in self.decimal_names:
            return
        self.findings.append({
            "kind": "round_of_decimal",
            "line": node.lineno,
            "field": field,
            "severity": "medium",
            "detail": (
                f"round() on a Decimal keeps precision but uses banker's rounding: "
                f"round(Decimal('0.125'), 2) is 0.12, and an invoice expects 0.13. "
                f"'{field}' is a DecimalField"
            ),
            "fix": 'value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)',
        })


def _model_findings(decimal_names: set[str]) -> list[dict[str, Any]]:
    from django.apps import apps
    from django.db import models

    out: list[dict[str, Any]] = []
    for model in apps.get_models():
        for field in model._meta.concrete_fields:
            label = f"{model._meta.label}.{field.name}"
            lowered = field.name.lower()

            if isinstance(field, models.FloatField) and any(w in lowered for w in _MONEY_WORDS):
                out.append({
                    "kind": "float_column_for_money",
                    "target": label,
                    "severity": "high",
                    "detail": (
                        "a FloatField cannot hold money exactly, and the column "
                        "itself is the problem, so no amount of care at the call "
                        "sites fixes it"
                    ),
                    "fix": "DecimalField(max_digits=..., decimal_places=2), with a migration",
                })

            if isinstance(field, models.DecimalField) and isinstance(field.default, float):
                exact = _exactly_representable(field.default)
                out.append({
                    "kind": "decimal_column_with_float_default",
                    "target": label,
                    "severity": "low" if exact else "high",
                    "detail": (
                        f"default={field.default!r} is a float on a DecimalField"
                        + ("" if exact else f", and it stores as {Decimal(field.default)}")
                    ),
                    "fix": f'default=Decimal("{field.default}")',
                })
    return out


def money_precision(search_path: str | None = None) -> dict[str, Any]:
    """Places where a decimal amount stops being exact.

    Args:
        search_path: directory to scan. Defaults to the project root.
    """
    config = ensure_django()

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    decimal_names = _decimal_field_names()
    findings: list[dict[str, Any]] = _model_findings(decimal_names)
    files_scanned = 0

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        files_scanned += 1
        lines = source.splitlines()

        visitor = _Visitor(decimal_names)
        visitor.visit(tree)
        for finding in visitor.findings:
            finding["file"] = str(path.relative_to(root))
            finding["code"] = (
                lines[finding["line"] - 1].strip()[:140]
                if finding["line"] <= len(lines) else ""
            )
            findings.append(finding)

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f.get("file", ""), f.get("line", 0)))

    by_kind: dict[str, int] = {}
    for finding in findings:
        by_kind[finding["kind"]] = by_kind.get(finding["kind"], 0) + 1

    return {
        "search_path": str(root),
        "files_scanned": files_scanned,
        "decimal_field_names": sorted(decimal_names),
        "finding_count": len(findings),
        "high_severity_count": sum(1 for f in findings if f["severity"] == "high"),
        "by_kind": by_kind,
        "findings": findings,
        "note": (
            "A DecimalField exists so that money is exact, and each of these "
            "gives that up somewhere the model definition cannot see. "
            "Decimal(0.5) is not reported as a defect: 0.5 is exactly "
            "representable and nothing is lost, so it is separated from "
            "Decimal(0.1), which is wrong before anything is done with it. "
            "round() on a Decimal keeps the precision and changes the rounding "
            "rule, which is a different and quieter problem.\n\n"
            "float() and round() are matched by attribute name against every "
            "DecimalField name in the project, because resolving what "
            "`invoice.amount` refers to would need type inference. So a "
            "variable that merely shares a name with a decimal column is "
            "reported, and one reached through a name no model uses is not."
        ),
    }
