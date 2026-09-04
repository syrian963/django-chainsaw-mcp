# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One command that runs the useful checks and returns one answer.

Twelve subcommands is twelve things to learn before the tool does anything.
Nobody reads a manual to try something; they run the obvious command and decide
in thirty seconds whether it was worth installing.

So `check` runs the analyses that make sense on any project, merges them into
one list sorted by severity, and returns one exit code. The individual
subcommands stay for when somebody wants depth on one dimension.

Checks are selected on merit: `deploy_safety` and `tenancy` are in because a
finding there is a bug, `list_models` is out because it is a description, not a
finding.
"""

from __future__ import annotations

from typing import Any, Callable

from .datetimes import datetime_audit
from .deploy_safety import deploy_safety
from .django_env import ensure_django
from .indexes import missing_indexes
from .migrations import migration_risk
from .on_commit import escaping_side_effects
from .scan import scan_templates
from .serializer_nplusone import serializer_nplusone
from .serializers import serializer_exposure
from .tenancy import find_unscoped_queries

# Severity ordering, and the gate boundary.
_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
GATE_DEFAULT = "high"


def _finding(check: str, severity: str, title: str, location: str | None,
             detail: str, fix: str | None = None) -> dict[str, Any]:
    return {
        "check": check,
        "severity": severity,
        "title": title,
        "location": location,
        "detail": detail,
        "fix": fix,
    }


def _from_deploy_safety(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "deploy-safety", "critical",
            f"{entry['operation']} drops {entry['symbol']!r} while code still uses it",
            f"{entry['app']}/{entry['migration']}",
            entry["explanation"],
            entry.get("safer"),
        )
        for entry in report.get("blocking", [])
    ]


def _from_tenancy(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "tenancy", f["severity"],
            f"{f['model']} read without an ownership filter",
            f"{f['file']}:{f['line']}",
            f["why"], f["suggested"],
        )
        for f in report.get("findings", [])
    ]


def _from_templates(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for result in report.get("results", []):
        for f in result.get("findings", []):
            if f["severity"] != "high":
                continue
            out.append(
                _finding(
                    "n+1-template", "medium",
                    f"{f['expression']} crosses a relation inside a loop",
                    result["template"], f["why"], f["suggested"],
                )
            )
    return out


def _from_serializer_nplusone(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "n+1-serializer", f["severity"],
            f"{f['serializer']}.{f['field']} crosses a relation",
            f.get("location"), f["why"], f.get("suggested"),
        )
        for f in report.get("findings", [])
        if f.get("relation")
    ]


def _from_exposure(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "serializers", f["severity"],
            f"{f['serializer']} exposes {f['model']} with mode {f['mode']}",
            f["serializer"], f["why"], f["suggested"],
        )
        for f in report.get("findings", [])
    ]


def _from_indexes(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "indexes", f["severity"],
            f"{f['model']}.{f['field']} is filtered or sorted without an index",
            f["used_at"][0]["file"] if f.get("used_at") else None,
            f["why"], f["suggested_model_change"],
        )
        for f in report.get("findings", [])
    ]


def _from_datetimes(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = [
        _finding(
            "datetimes", f["severity"],
            f"{f['model']}.{f['field']}: {f['kind'].replace('_', ' ')}",
            f["model"], f["detail"], f["suggested"],
        )
        for f in report.get("model_findings", [])
    ]
    if report.get("use_tz"):
        out += [
            _finding(
                "datetimes", f["severity"], f"{f['call']} produces a naive datetime",
                f"{f['file']}:{f['line']}", f["why"], f["suggested"],
            )
            for f in report.get("code_findings", [])
        ]
    return out


def _from_on_commit(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "on-commit", f["severity"],
            f"{f['call']} escapes the transaction around it",
            f"{f['file']}:{f['line']}", f["consequence"], f["fix"],
        )
        for f in report.get("findings", [])
    ]


def _from_migrations(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for entry in report.get("migrations", []):
        if entry["worst_risk"] == "safe":
            continue
        worst = next(op for op in entry["operations"] if op["risk"] != "safe")
        out.append(
            _finding(
                "migrations", "medium",
                f"{entry['app']}.{entry['name']}: {entry['worst_risk'].replace('_', ' ')}",
                f"{entry['app']}/{entry['name']}",
                worst["detail"], worst.get("safer"),
            )
        )
    return out


# check name -> (callable, kwargs builder, adapter)
_CHECKS: dict[str, tuple[Callable[..., Any], Callable[[dict], dict], Callable]] = {
    "deploy-safety": (deploy_safety, lambda o: {}, _from_deploy_safety),
    "tenancy": (find_unscoped_queries, lambda o: {"tenant_root": o["tenant_root"]}, _from_tenancy),
    "n+1-serializer": (serializer_nplusone, lambda o: {}, _from_serializer_nplusone),
    "n+1-template": (scan_templates, lambda o: {}, _from_templates),
    "serializers": (serializer_exposure, lambda o: {}, _from_exposure),
    "indexes": (missing_indexes, lambda o: {}, _from_indexes),
    "datetimes": (datetime_audit, lambda o: {}, _from_datetimes),
    "on-commit": (escaping_side_effects, lambda o: {}, _from_on_commit),
    "migrations": (migration_risk, lambda o: {}, _from_migrations),
}

ALL_CHECKS = tuple(_CHECKS)


def run_all(
    tenant_root: str = "auth.User",
    only: list[str] | None = None,
    skip: list[str] | None = None,
) -> dict[str, Any]:
    """Run the analyses and merge them into one severity-sorted list.

    Args:
        tenant_root: the model that owns data, for the ownership check.
        only: run just these checks.
        skip: run everything except these.
    """
    ensure_django()

    unknown = set(only or []) | set(skip or [])
    unknown -= set(_CHECKS)
    if unknown:
        raise ValueError(
            f"Unknown check(s): {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(ALL_CHECKS)}"
        )

    selected = [
        name for name in ALL_CHECKS
        if (not only or name in only) and name not in (skip or [])
    ]

    options = {"tenant_root": tenant_root}
    findings: list[dict[str, Any]] = []
    ran: dict[str, Any] = {}

    for name in selected:
        fn, build_kwargs, adapt = _CHECKS[name]
        try:
            report = fn(**build_kwargs(options))
            produced = adapt(report)
            findings.extend(produced)
            ran[name] = {"ok": True, "findings": len(produced)}
        except Exception as exc:  # noqa: BLE001 - surfaced, not hidden
            ran[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    findings.sort(key=lambda f: (_ORDER.get(f["severity"], 9), f["check"], f["location"] or ""))

    by_severity: dict[str, int] = {}
    for finding in findings:
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1

    failed = [name for name, state in ran.items() if not state["ok"]]
    return {
        "checks_run": ran,
        "checks_failed": failed,
        "finding_count": len(findings),
        "by_severity": by_severity,
        "findings": findings,
        "note": (
            "Merged output from several candidate-producing analyses. Each "
            "carries its own limits, documented with the individual tool. A "
            "check that could not run is listed in checks_failed rather than "
            "being counted as clean."
            + (f" {len(failed)} check(s) failed to run." if failed else "")
        ),
    }


def gate(report: dict[str, Any], threshold: str = GATE_DEFAULT) -> bool:
    """True when anything at or above the threshold was found."""
    limit = _ORDER.get(threshold, 1)
    return any(_ORDER.get(f["severity"], 9) <= limit for f in report["findings"])
