# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Turn findings into code, and be honest about which ones can be applied.

A report that ends in "add an ownership filter" leaves the actual work to the
reader. The interesting question is which fixes a machine can write correctly
and which it cannot, and the answer is not the same for every check.

Three classes, and the distinction is the whole design:

**mechanical**
    One correct answer, derivable from the code alone.
    `datetime.now()` becomes `timezone.now()`. There is no judgement in it and
    no way for it to be wrong. Safe to apply automatically.

**generated**
    A machine can write the artefact, but a human decides whether it should
    exist. A migration adding an index is exactly right as text and entirely
    wrong if that table is written far more than it is read. Produced as a file
    to review, never applied.

**advisory**
    Real code, in the right place, with the right names resolved, but the
    decision belongs to somebody who knows the system. Which user owns a row is
    not a question the AST can answer. Shown as a diff, never applied.

Everything defaults to printing a diff. Writing requires asking for it.
"""

from __future__ import annotations

import ast
import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MECHANICAL = "mechanical"
GENERATED = "generated"
ADVISORY = "advisory"


@dataclass
class Fix:
    kind: str
    check: str
    title: str
    path: str | None = None
    line: int | None = None
    old: str | None = None
    new: str | None = None
    extra_import: str | None = None
    new_file: str | None = None
    new_file_content: str | None = None
    why: str = ""
    caution: str | None = None

    def diff(self, root: Path) -> str:
        if self.new_file and self.new_file_content:
            body = "".join(f"+{line}\n" for line in self.new_file_content.splitlines())
            return f"--- /dev/null\n+++ {self.new_file}\n{body}"
        if not (self.path and self.old is not None and self.new is not None):
            return ""
        return "".join(
            difflib.unified_diff(
                [self.old + "\n"],
                [self.new + "\n"],
                fromfile=f"a/{self.path}",
                tofile=f"b/{self.path}",
                lineterm="\n",
                n=0,
            )
        )


@dataclass
class FixSet:
    fixes: list[Fix] = field(default_factory=list)

    def by_kind(self, kind: str) -> list[Fix]:
        return [f for f in self.fixes if f.kind == kind]


# --------------------------------------------------------------------------- datetimes

_DT_REWRITES = [
    (re.compile(r"\bdatetime\.datetime\.utcnow\(\)"), "timezone.now()"),
    (re.compile(r"\bdatetime\.utcnow\(\)"), "timezone.now()"),
    (re.compile(r"\bdt\.utcnow\(\)"), "timezone.now()"),
    (re.compile(r"\bdatetime\.datetime\.now\(\)"), "timezone.now()"),
    (re.compile(r"\bdatetime\.now\(\)"), "timezone.now()"),
    (re.compile(r"\bdatetime\.datetime\.today\(\)"), "timezone.localdate()"),
]

_TZ_IMPORT = "from django.utils import timezone"


def _datetime_fixes(report: dict[str, Any], root: Path) -> list[Fix]:
    if not report.get("use_tz"):
        return []

    out: list[Fix] = []

    for finding in report.get("code_findings", []):
        if finding["kind"] != "naive_now":
            continue
        path = root / finding["file"]
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        index = finding["line"] - 1
        if index >= len(lines):
            continue
        original = lines[index]
        replaced = original
        for pattern, replacement in _DT_REWRITES:
            replaced = pattern.sub(replacement, replaced)
        if replaced == original:
            continue

        source = path.read_text(encoding="utf-8")
        out.append(
            Fix(
                kind=MECHANICAL,
                check="datetimes",
                title=f"{finding['call']} becomes an aware call",
                path=finding["file"],
                line=finding["line"],
                old=original,
                new=replaced,
                extra_import=None if _TZ_IMPORT in source else _TZ_IMPORT,
                why=(
                    "timezone.now() returns an aware datetime, which is what the "
                    "ORM compares against. There is one correct replacement and "
                    "no judgement in it."
                ),
            )
        )

    for finding in report.get("model_findings", []):
        if finding["kind"] not in {"naive_default", "frozen_default"}:
            continue
        out.append(
            Fix(
                kind=ADVISORY if finding["kind"] == "frozen_default" else MECHANICAL,
                check="datetimes",
                title=f"{finding['model']}.{finding['field']}: {finding['detail'][:60]}",
                why=finding["suggested"],
                caution=(
                    "Changing a default does not change rows already written with "
                    "the old one. A data migration may be needed as well."
                ),
            )
        )

    return out


# --------------------------------------------------------------------------- serializers


def _serializer_fixes(report: dict[str, Any], root: Path) -> list[Fix]:
    out: list[Fix] = []
    for finding in report.get("findings", []):
        mode = str(finding.get("mode", ""))
        if not (mode == "__all__" or mode.startswith("exclude")):
            continue

        suggested = finding.get("suggested", "")
        match = re.search(r"fields = \[(.*)\]", suggested, re.S)
        if not match:
            continue

        sensitive = {entry["field"] for entry in finding.get("sensitive", [])}
        out.append(
            Fix(
                kind=ADVISORY if sensitive else GENERATED,
                check="serializers",
                title=f"{finding['serializer']}: replace {mode} with an explicit list",
                old=f"fields = \"__all__\"" if mode == "__all__" else f"{mode}",
                new=suggested.replace("list the fields explicitly: ", ""),
                why=(
                    "An explicit list stops the next migration widening the API "
                    "with no diff on this file."
                ),
                caution=(
                    "The generated list is every field the model has today, "
                    f"including {', '.join(sorted(sensitive))}. Remove what should "
                    "not be public before applying."
                    if sensitive else
                    "This lists what is exposed today, which preserves current "
                    "behaviour exactly. Narrow it further if some of it was never "
                    "meant to be public."
                ),
            )
        )
    return out


# --------------------------------------------------------------------------- indexes


def _index_migration(findings: list[dict[str, Any]], app_label: str) -> str:
    """A real migration, using the concurrent operation on purpose."""
    operations = []
    for finding in findings:
        model = finding["model"].split(".")[-1].lower()
        field_name = finding["field"]
        operations.append(
            f"        AddIndexConcurrently(\n"
            f"            model_name=\"{model}\",\n"
            f"            index=models.Index(\n"
            f"                fields=[\"{field_name}\"],\n"
            f"                name=\"{model}_{field_name}_idx\"[:30],\n"
            f"            ),\n"
            f"        ),"
        )

    return (
        '"""Add indexes for fields the code filters or sorts on.\n\n'
        "Generated by django-chainsaw. Review before applying: an index costs\n"
        "write throughput and disk on every insert and update, and this was\n"
        "written from the code, not from your row counts.\n"
        '"""\n\n'
        "from django.contrib.postgres.operations import AddIndexConcurrently\n"
        "from django.db import migrations, models\n\n\n"
        "class Migration(migrations.Migration):\n"
        "    # Concurrent index creation cannot run inside a transaction, and\n"
        "    # without it the build locks the table against writes.\n"
        "    atomic = False\n\n"
        "    dependencies = [\n"
        f'        ("{app_label}", "REPLACE_WITH_LATEST"),\n'
        "    ]\n\n"
        "    operations = [\n"
        + "\n".join(operations)
        + "\n    ]\n"
    )


def _index_fixes(report: dict[str, Any], root: Path) -> list[Fix]:
    by_app: dict[str, list[dict[str, Any]]] = {}
    for finding in report.get("findings", []):
        if finding["severity"] != "high":
            continue
        by_app.setdefault(finding["model"].split(".")[0], []).append(finding)

    out: list[Fix] = []
    for app_label, findings in sorted(by_app.items()):
        fields = ", ".join(f"{f['model'].split('.')[-1]}.{f['field']}" for f in findings)
        out.append(
            Fix(
                kind=GENERATED,
                check="indexes",
                title=f"Migration adding {len(findings)} index(es) to {app_label}",
                new_file=f"{app_label}/migrations/XXXX_add_chainsaw_indexes.py",
                new_file_content=_index_migration(findings, app_label),
                why=f"Covers {fields}.",
                caution=(
                    "Uses AddIndexConcurrently with atomic = False, which is the "
                    "safe form on PostgreSQL but is PostgreSQL only. Set the "
                    "dependency to your latest migration. An index is not free: "
                    "confirm each of these tables is read more than it is written."
                ),
            )
        )
    return out


# --------------------------------------------------------------------------- tenancy


def _enclosing_request_name(tree: ast.AST, line: int) -> str | None:
    """The name of the request argument in the function containing this line.

    Suggesting `request.user` in a function whose parameter is called `req`
    produces code that does not run, and one of those is enough for somebody to
    stop trusting the suggestions.
    """
    best: tuple[int, str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = getattr(node, "end_lineno", None) or node.lineno
        if not (node.lineno <= line <= end):
            continue
        args = [a.arg for a in node.args.args]
        candidate = next((a for a in args if a in {"request", "req"}), None)
        if candidate is None and args and args[0] == "self" and len(args) > 1:
            candidate = args[1]
        if candidate and (best is None or node.lineno > best[0]):
            best = (node.lineno, candidate)
    return best[1] if best else None


def _tenancy_fixes(report: dict[str, Any], root: Path) -> list[Fix]:
    out: list[Fix] = []
    cache: dict[str, ast.AST] = {}

    for finding in report.get("findings", []):
        path = root / finding["file"]
        try:
            source = path.read_text(encoding="utf-8")
            if finding["file"] not in cache:
                cache[finding["file"]] = ast.parse(source)
            lines = source.splitlines()
        except (OSError, SyntaxError):
            continue

        index = finding["line"] - 1
        if index >= len(lines):
            continue

        request_name = _enclosing_request_name(cache[finding["file"]], finding["line"])
        owner_path = finding["owner_path"]
        original = lines[index]

        if request_name:
            scope = f"{request_name}.user"
            replaced = re.sub(
                r"\.objects\.(all\(\)|filter\(|get\(|exclude\()",
                lambda m: (
                    f".objects.filter({owner_path}={scope})"
                    + ("" if m.group(1) == "all()" else f".{m.group(1)}")
                ),
                original,
                count=1,
            )
            caution = (
                f"'{scope}' is the request argument of the enclosing function, but "
                f"whether {owner_path} points at a user, a profile or an "
                "organisation is a question about your domain, not your syntax."
            )
        else:
            replaced = None
            caution = (
                "No request argument was found in the enclosing function, so there "
                "is nothing to scope by from here. This may be a manager, a task, "
                "or a helper that should take the owner as a parameter."
            )

        out.append(
            Fix(
                kind=ADVISORY,
                check="tenancy",
                title=f"{finding['model']} read without an ownership filter",
                path=finding["file"],
                line=finding["line"],
                old=original,
                new=replaced,
                why=finding["why"],
                caution=caution,
            )
        )
    return out


# --------------------------------------------------------------------------- entry point

_BUILDERS = {
    "datetimes": _datetime_fixes,
    "serializers": _serializer_fixes,
    "indexes": _index_fixes,
    "tenancy": _tenancy_fixes,
}


def build_fixes(reports: dict[str, dict[str, Any]], root: Path) -> FixSet:
    """Turn raw analyser reports into fixes, grouped by how safe they are."""
    fixes: list[Fix] = []
    for name, builder in _BUILDERS.items():
        report = reports.get(name)
        if report:
            fixes.extend(builder(report, root))
    return FixSet(fixes=fixes)


def apply_mechanical(fixes: list[Fix], root: Path) -> dict[str, Any]:
    """Write the mechanical fixes to disk.

    Only ever called for fixes marked mechanical, and only when the caller has
    explicitly asked. Line based, applied bottom up per file so earlier edits do
    not move later line numbers.
    """
    by_file: dict[str, list[Fix]] = {}
    for fix in fixes:
        if fix.kind != MECHANICAL or not (fix.path and fix.new is not None):
            continue
        by_file.setdefault(fix.path, []).append(fix)

    changed: list[str] = []
    skipped: list[dict[str, str]] = []

    for relative, file_fixes in sorted(by_file.items()):
        path = root / relative
        try:
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError as exc:
            skipped.append({"file": relative, "reason": str(exc)})
            continue

        imports = {f.extra_import for f in file_fixes if f.extra_import}

        for fix in sorted(file_fixes, key=lambda f: -(f.line or 0)):
            index = (fix.line or 0) - 1
            if not (0 <= index < len(lines)):
                skipped.append({"file": relative, "reason": f"line {fix.line} is out of range"})
                continue
            # Refuse if the line moved since the analysis.
            if lines[index].rstrip("\n") != fix.old:
                skipped.append(
                    {"file": relative, "reason": f"line {fix.line} changed since the analysis"}
                )
                continue
            ending = "\n" if lines[index].endswith("\n") else ""
            lines[index] = (fix.new or "") + ending

        for statement in sorted(imports):
            if statement not in "".join(lines):
                insert_at = 0
                for number, line in enumerate(lines):
                    if line.startswith(("import ", "from ")):
                        insert_at = number + 1
                lines.insert(insert_at, statement + "\n")

        path.write_text("".join(lines), encoding="utf-8")
        changed.append(relative)

    return {"changed_files": changed, "skipped": skipped}
