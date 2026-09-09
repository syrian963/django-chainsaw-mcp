# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Datetime handling that goes wrong twice a year.

With `USE_TZ = True` Django stores UTC and hands back aware objects. Code that
builds its own datetimes with `datetime.now()` or `datetime(2026, 3, 29, 2, 30)`
produces naive ones, and mixing the two either raises or, worse, silently
compares against the wrong instant.

The bug is invisible for ten months. It shows up on the two nights a year when
the clock moves, when a day has 23 or 25 hours, and by then nobody connects the
report to the code that wrote it.

None of this needs a database or a running app: it is all visible in the source
and in the field definitions.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from .django_env import ensure_django
from .project import parse_file, read_source

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

_NAIVE_CALLS = {
    "now": (
        "datetime.now() returns a naive datetime unless a tz is passed",
        "django.utils.timezone.now()",
    ),
    "utcnow": (
        "datetime.utcnow() returns a naive datetime holding UTC, which is the "
        "worst combination: it looks right and compares wrong",
        "django.utils.timezone.now()",
    ),
    "today": (
        "datetime.today() is datetime.now() without a timezone",
        "django.utils.timezone.localdate() for a date, timezone.now() for an instant",
    ),
    "fromtimestamp": (
        "datetime.fromtimestamp() without tz uses the server's local zone",
        "datetime.fromtimestamp(value, tz=datetime.timezone.utc)",
    ),
    "utcfromtimestamp": (
        "datetime.utcfromtimestamp() returns a naive datetime holding UTC",
        "datetime.fromtimestamp(value, tz=datetime.timezone.utc)",
    ),
}


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.findings: list[dict[str, Any]] = []
        self.aware_lines: set[int] = set()
        self._timezone_names = {"timezone"}

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        if isinstance(func, ast.Attribute) and func.attr in _NAIVE_CALLS:
            owner = self._owner(func.value)
            # timezone.now() is the correct call and shares the name.
            if owner not in self._timezone_names:
                has_tz = any(kw.arg in {"tz", "tzinfo"} for kw in node.keywords)
                if not has_tz and not (func.attr == "fromtimestamp" and len(node.args) > 1):
                    why, fix = _NAIVE_CALLS[func.attr]
                    self.findings.append(
                        {
                            "line": node.lineno,
                            "kind": "naive_now",
                            "call": f"{owner}.{func.attr}()" if owner else f"{func.attr}()",
                            "why": why,
                            "suggested": fix,
                            "severity": "high" if func.attr in {"now", "utcnow"} else "medium",
                        }
                    )

        # A literal built from parts, written either as `datetime(...)` after
        # `from datetime import datetime`, or as `datetime.datetime(...)`.
        is_literal = (isinstance(func, ast.Name) and func.id == "datetime") or (
            isinstance(func, ast.Attribute) and func.attr == "datetime"
        )
        if is_literal and len(node.args) >= 3:
            if not any(kw.arg == "tzinfo" for kw in node.keywords):
                self.findings.append(
                    {
                        "line": node.lineno,
                        "kind": "naive_literal",
                        "call": "datetime(...)",
                        "why": (
                            "a datetime built from parts without tzinfo is naive, "
                            "and the wall clock it names may not exist or may "
                            "happen twice on a DST boundary"
                        ),
                        "suggested": "pass tzinfo=, or build it with timezone.make_aware()",
                        "severity": "medium",
                    }
                )

        # A literal wrapped in make_aware is correct, and flagging it would be
        # the noise that gets a check switched off. Record the line so the
        # finding above can be dropped once the whole file is walked.
        if isinstance(func, ast.Attribute) and func.attr in {"make_aware", "localize"}:
            self.aware_lines.add(node.lineno)
            for argument in ast.walk(node):
                if isinstance(argument, ast.Call):
                    self.aware_lines.add(argument.lineno)

        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # `from django.utils import timezone as tz` must still be recognised.
        if node.module and node.module.startswith("django.utils"):
            for alias in node.names:
                if alias.name == "timezone":
                    self._timezone_names.add(alias.asname or alias.name)
        self.generic_visit(node)

    def _owner(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return None


def _field_findings(model: Any) -> list[dict[str, Any]]:
    """Model fields whose defaults or options make timezones ambiguous."""
    out: list[dict[str, Any]] = []
    for field in model._meta.get_fields():
        if not getattr(field, "concrete", False):
            continue
        name = type(field).__name__
        if name not in {"DateTimeField", "DateField"}:
            continue

        default = getattr(field, "default", None)
        has_default = field.has_default()
        auto_now = getattr(field, "auto_now", False)
        auto_now_add = getattr(field, "auto_now_add", False)

        if has_default and callable(default):
            module = getattr(default, "__module__", "") or ""
            qualname = getattr(default, "__qualname__", getattr(default, "__name__", "")) or ""
            if module.startswith("datetime") or qualname.startswith("datetime."):
                out.append(
                    {
                        "field": field.name,
                        "kind": "naive_default",
                        "detail": f"default={qualname or default!r} produces a naive value",
                        "suggested": "default=django.utils.timezone.now",
                        "severity": "high",
                    }
                )
        elif has_default and default is not None and not callable(default):
            out.append(
                {
                    "field": field.name,
                    "kind": "frozen_default",
                    "detail": (
                        f"default={default!r} is evaluated once at import time, so "
                        "every row gets the moment the process started"
                    ),
                    "suggested": "pass the callable itself, not a call: default=timezone.now",
                    "severity": "high",
                }
            )

        if auto_now and auto_now_add:
            out.append(
                {
                    "field": field.name,
                    "kind": "conflicting_auto",
                    "detail": "auto_now and auto_now_add together: auto_now wins silently",
                    "suggested": "pick one",
                    "severity": "medium",
                }
            )
    return out


def datetime_audit(search_path: str | None = None) -> dict[str, Any]:
    """Find naive datetimes in code and ambiguous defaults on model fields.

    Args:
        search_path: directory to scan. Defaults to the project path.
    """
    config = ensure_django()
    from django.apps import apps
    from django.conf import settings

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    use_tz = bool(getattr(settings, "USE_TZ", False))

    code_findings: list[dict[str, Any]] = []
    files_scanned = 0

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue

        files_scanned += 1
        lines = source.splitlines()
        visitor = _Visitor()
        visitor.visit(tree)

        for finding in visitor.findings:
            if finding["kind"] == "naive_literal" and finding["line"] in visitor.aware_lines:
                continue
            finding["file"] = str(path.relative_to(root))
            finding["code"] = (
                lines[finding["line"] - 1].strip()[:140]
                if finding["line"] <= len(lines) else ""
            )
            code_findings.append(finding)

    model_findings: list[dict[str, Any]] = []
    for model in apps.get_models():
        for entry in _field_findings(model):
            entry["model"] = model._meta.label
            model_findings.append(entry)

    code_findings.sort(key=lambda f: (f["severity"] != "high", f["file"], f["line"]))
    model_findings.sort(key=lambda f: (f["severity"] != "high", f["model"], f["field"]))

    return {
        "use_tz": use_tz,
        "search_path": str(root),
        "files_scanned": files_scanned,
        "code_findings": code_findings,
        "model_findings": model_findings,
        "total": len(code_findings) + len(model_findings),
        "high_severity_count": sum(
            1 for f in code_findings + model_findings if f["severity"] == "high"
        ),
        "note": (
            "With USE_TZ off, naive datetimes are the project's convention and "
            "these findings are informational. With it on, mixing naive and "
            "aware values either raises or compares against the wrong instant. "
            "This reads the source and the field definitions: a datetime "
            "produced by a library, by a parser, or by arithmetic on a naive "
            "value is not visible here."
            if use_tz else
            "USE_TZ is False for this project, so naive datetimes are the "
            "convention and the code findings below are informational rather "
            "than defects. The model-level findings still apply: a default "
            "evaluated once at import time is wrong either way."
        ),
    }
