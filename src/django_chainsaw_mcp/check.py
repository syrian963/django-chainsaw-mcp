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

import time
from collections.abc import Callable
from typing import Any

from .aggregates import multiplied_aggregates
from .asyncio_blocking import blocking_in_async
from .bypass import bypassed_effects
from .celery_tasks import celery_arguments
from .choices import choice_typos
from .concurrency import race_conditions
from .dangling import dangling_references
from .datetimes import datetime_audit
from .deploy_safety import deploy_safety
from .django_env import DjangoBootError, ensure_django
from .exposure_auth import open_endpoints
from .fastapi_exposure import fastapi_exposure
from .indexes import missing_indexes
from .loop_queries import queries_in_loops
from .migrations import migration_risk
from .money import money_precision
from .on_commit import escaping_side_effects
from .project import enable_source_cache, get_profile, project_root
from .scan import scan_templates
from .serializer_nplusone import serializer_nplusone
from .serializers import serializer_exposure
from .sqlalchemy_nplusone import sqlalchemy_nplusone
from .tenancy import find_unscoped_queries

# Severity ordering, and the gate boundary.
_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}
GATE_DEFAULT = "high"


def _finding(check: str, severity: str, title: str, location: str | None,
             detail: str, fix: str | None = None) -> dict[str, Any]:
    """One finding in the merged shape.

    `location` is whatever the check found natural - a file and line, an app
    and migration, a serializer class. When it is a file and line, the two are
    also given separately: `--since` compares paths against a git diff and
    cannot do that with "app/x.py:12", and every consumer that wanted the file
    was parsing the string itself.
    """
    file_name: str | None = None
    line: int | None = None
    if location and ":" in location:
        head, _, tail = location.rpartition(":")
        if tail.isdigit() and head.endswith(".py"):
            file_name, line = head, int(tail)

    return {
        "check": check,
        "severity": severity,
        "title": title,
        "location": location,
        "file": file_name,
        "line": line,
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
    out = []
    for f in report.get("findings", []):
        if not f.get("relation"):
            continue
        title = f"{f['serializer']}.{f['field']} crosses a relation"
        root = f.get("root_serializer")
        # The same nested field is reported once per root serializer that
        # reaches it, because the prefetch belongs on each root's queryset and
        # they are different fixes. Dropping the root made three separate
        # findings look like the same line printed three times.
        if root and root != f["serializer"]:
            title += f", served through {root}"
        out.append(
            _finding(
                "n+1-serializer", f["severity"], title,
                f.get("location"), f["why"], f.get("suggested"),
            )
        )
    return out


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


def _from_bypass(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "bypass", f["severity"],
            f"{f['model']}.{f['method']}() skips the save() chain",
            f"{f['file']}:{f['line']}",
            "not fired: " + ", ".join(f["receivers_not_fired"] + f["overrides_not_run"]),
            f["fix"],
        )
        for f in report.get("findings", [])
    ]


def _from_races(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = [
        _finding(
            "races", "high" if f["confidence"] == "high" else "medium",
            f"{f['instance']}.{f['field']} is read, changed in Python and saved",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("races", [])
    ]
    out += [
        _finding(
            "races", "high",
            f"{f['model']}.{f['method']}({', '.join(f['lookup'])}) has no unique constraint behind it",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("unsafe_upserts", [])
    ]
    out += [
        _finding(
            "races", "high",
            "select_for_update() with no transaction to hold the lock",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("locks_outside_transaction", [])
    ]
    return out


def _from_open(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "open", f["severity"],
            f"{f['view'].rsplit('.', 1)[-1]} is public and exposes "
            + (", ".join(f["sensitive_fields"]) if f["sensitive_fields"] else f"every field ({f['mode']})"),
            f["view"], f["why"], f["fix"],
        )
        for f in report.get("findings", [])
    ]


def _from_money(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "money", f["severity"], f["kind"].replace("_", " "),
            f.get("target") or f"{f.get('file')}:{f.get('line')}",
            f["detail"], f["fix"],
        )
        for f in report.get("findings", [])
        if f["severity"] != "low"
    ]


def _from_async(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = [
        _finding(
            "async", "high",
            f"{f['call']} blocks the event loop in async {f['function']}()",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("direct", [])
    ]
    out += [
        _finding(
            "async", "high",
            f"async {f['function']}() reaches {f['call']}, which blocks the loop",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("reached_through_a_call", [])
    ]
    return out


def _from_routes(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "routes", f["severity"],
            f"{f['method']} {f['path']} returns more than it declares",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("findings", [])
    ]


def _from_sqla(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = [
        _finding(
            "sqla", "high",
            f"{f['model']}.{f['attribute']} is loaded once per row in a loop",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("in_loops", [])
    ]
    out += [
        _finding(
            "sqla", "high",
            f"{f['model']}.{f['attribute']} is loaded once per row while serialising",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("in_response_models", [])
    ]
    return out


def _from_celery(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = [
        _finding(
            "celery", "high",
            f"{f['task']} is handed a {f['model']} where {f['parameter']} belongs",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("instance_arguments", [])
    ]
    out += [
        _finding(
            "celery", "high",
            f"{f['task']} is given {f['given']} argument(s) and expects {f['expects']}",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("arity_mismatches", [])
    ]
    return out


def _from_aggregates(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = [
        _finding(
            "aggregates", f["severity"],
            f"{f['model']}: {' and '.join(f['relations'])} joined in one query",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("findings", [])
    ]
    out.extend(
        _finding(
            "aggregates", f["severity"],
            f"{f['model']}: a filter joins {' and '.join(f['filtered_on'])}",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("multiplied_by_a_filter", [])
    )
    return out


def _from_choices(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "choices", f["severity"],
            f"{f['model']}.{f['field']} is never {f['value']!r}",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("findings", [])
    ]
    # The untyped comparisons stay out. They rest on an attribute name, and
    # the object it belongs to may not be a model at all - which is a fine
    # thing to show somebody looking, and not a thing to fail a build on.


def _from_dangling(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            "dangling", f["severity"],
            (f"no URL pattern is named {f['name']!r}" if f["kind"] == "url"
             else f"no template named {f['name']!r} exists"),
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        )
        for f in report.get("findings", [])
    ]


def _from_loops(report: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for bucket in ("per_row", "loop_invariant", "writes_in_loops"):
        for f in report.get(bucket, []):
            out.append(_finding(
                "loops", f["severity"],
                f"{f['call']} runs inside the loop at line {f['loop_at_line']}",
                f"{f['file']}:{f['line']}", f["why"], f["fix"],
            ))
    for f in report.get("through_a_call", []):
        out.append(_finding(
            "loops", f["severity"],
            f"the loop at line {f['loop_at_line']} calls {f['call']}, "
            f"which queries",
            f"{f['file']}:{f['line']}", f["why"], f["fix"],
        ))
    return out


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
    "bypass": (bypassed_effects, lambda o: {}, _from_bypass),
    "races": (race_conditions, lambda o: {}, _from_races),
    "money": (money_precision, lambda o: {}, _from_money),
    "celery": (celery_arguments, lambda o: {}, _from_celery),
    "loops": (queries_in_loops, lambda o: {}, _from_loops),
    "choices": (choice_typos, lambda o: {}, _from_choices),
    "aggregates": (multiplied_aggregates, lambda o: {}, _from_aggregates),
    "dangling": (dangling_references, lambda o: {}, _from_dangling),
    "open": (open_endpoints, lambda o: {}, _from_open),
    "migrations": (migration_risk, lambda o: {}, _from_migrations),
    "async": (blocking_in_async, lambda o: {}, _from_async),
    "routes": (fastapi_exposure, lambda o: {}, _from_routes),
    "sqla": (sqlalchemy_nplusone, lambda o: {}, _from_sqla),
}

# What each check needs before it can say anything. Running a Django check on a
# FastAPI project produces a boot error rather than an answer, and running an
# async check on a project with no async functions produces silence that reads
# like a clean result. Both are worse than saying the check does not apply.
_REQUIRES: dict[str, str] = {
    "deploy-safety": "django",
    "tenancy": "django",
    "n+1-serializer": "drf",
    "n+1-template": "django",
    "serializers": "drf",
    "indexes": "django",
    "datetimes": "django",
    "on-commit": "django",
    "bypass": "django",
    "races": "django",
    "money": "django",
    "celery": "django",
    "loops": "django",
    "choices": "django",
    "aggregates": "django",
    "dangling": "django",
    "open": "drf",
    "migrations": "django",
    "async": "any",
    "routes": "fastapi",
    "sqla": "sqlalchemy",
}


def _applicable(name: str, profile: Any) -> tuple[bool, str]:
    """Can this check say anything about this project?"""
    needs = _REQUIRES.get(name, "any")
    if needs == "any":
        return True, ""
    if needs == "django":
        if profile is None or profile.is_django:
            return True, ""
        return False, "no Django in this project"
    if needs == "fastapi":
        if profile is None or profile.is_async_web:
            return True, ""
        return False, "no FastAPI or Starlette in this project"
    if needs == "drf":
        # All three of these return early with `rest_framework_installed:
        # False` when DRF is absent, and two of them do it without their
        # coverage counters - so the merged report showed `findings: 0` with
        # an empty `examined`, which reads as "this zero cannot be
        # interpreted". It can: there is no DRF here. Saying so alongside
        # FastAPI and SQLAlchemy is what the other two frameworks already get.
        if profile is None or profile.uses("drf"):
            return True, ""
        return False, "no Django REST Framework in this project"
    if needs == "sqlalchemy":
        if profile is None or profile.uses("sqlalchemy") or profile.uses("sqlmodel"):
            return True, ""
        return False, "no SQLAlchemy in this project"
    return True, ""

ALL_CHECKS = tuple(_CHECKS)


# Keys that say what a check looked at, rather than what it found.
#
# `celery` reported nothing on a 2025-file project with Celery in it, and the
# aggregate said `findings: 0` - which reads as "clean" and could equally have
# meant "found no tasks at all". The individual check knew the difference all
# along: 12 tasks, 41 dispatches, none of them handed a model instance. That
# is a clean zero, and the merged report threw the evidence away.
#
# This repository's own instruction to a client is that an empty result is not
# the same as a clean one and that every check says which. That was true of
# the checks and false of the thing that merges them.
_COVERAGE_SUFFIXES = ("_checked", "_scanned", "_seen", "_found")

# Four checks count with `_count` instead, where the same suffix also names
# results. Listing them beats guessing which `_count` is coverage and which is
# a finding.
_COVERAGE_KEYS = {
    "migrations": ("migration_count",),
    "n+1-serializer": ("serializer_count",),
    "serializers": ("serializer_count",),
    "sqla": ("models_with_relationships", "relationship_count"),
}


def _registry_warning() -> str | None:
    """Whether the project that booted is the one the reader thinks it is.

    A settings module can import cleanly and still load nothing: readthedocs
    has a `settings/base.py` that boots and defines no models, and eighteen of
    the twenty-one checks then examine an empty registry and report nothing.
    Every individual number was honest and the run as a whole was worthless,
    which is the shape of answer this repository exists to avoid.
    """
    try:
        from django.apps import apps
    except Exception:
        return None
    try:
        models = apps.get_models()
        labels = [config.label for config in apps.get_app_configs()]
    except Exception:
        return None

    if not models:
        return (
            "This project has no models. Django booted, so the settings "
            "module imports, but nothing in INSTALLED_APPS defines a model - "
            "usually a base or partial settings module rather than the one "
            "the site runs on. Most checks here read the model registry, so "
            "their zeroes mean nothing until this is pointed at the real "
            "settings."
        )
    contrib = {"admin", "auth", "contenttypes", "sessions", "messages",
               "staticfiles", "sites", "humanize", "flatpages", "sitemaps",
               "redirects", "syndication", "postgres", "gis"}
    if not [label for label in labels if label not in contrib]:
        return (
            "Only Django's own contrib apps are installed. The settings "
            "module imports, but none of the project's apps are in "
            "INSTALLED_APPS, so the checks are reading Django rather than "
            "this project."
        )
    return None


def _examined(name: str, report: dict[str, Any]) -> dict[str, int]:
    """What this check looked at, as far as its own report says.

    Empty when the check does not say - which is itself worth seeing, because
    it marks a zero nobody can interpret.
    """
    if not isinstance(report, dict):
        return {}
    explicit = _COVERAGE_KEYS.get(name)
    if explicit:
        return {
            key: report[key]
            for key in explicit
            if isinstance(report.get(key), int) and not isinstance(report.get(key), bool)
        }
    return {
        key: value
        for key, value in report.items()
        if isinstance(value, int)
        and not isinstance(value, bool)
        and key.endswith(_COVERAGE_SUFFIXES)
    }


def run_all(
    tenant_root: str = "auth.User",
    only: list[str] | None = None,
    skip: list[str] | None = None,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """Run the analyses and merge them into one severity-sorted list.

    Args:
        tenant_root: the model that owns data, for the ownership check.
        only: run just these checks.
        skip: run everything except these.
        on_progress: called as (completed, total, next_check) before each
            check starts, and once more when the last one finishes. The
            checks are not equal in cost - `aggregates` and `choices` walk
            every function body - so the count moves unevenly and is a
            position, not an estimate of the time left.
    """
    # Detecting the frameworks first means a FastAPI project gets the checks
    # that apply instead of a settings error, and a Django project is
    # unaffected because everything still applies.
    try:
        profile = get_profile(project_root())
    except Exception:
        profile = None

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
    not_applicable: dict[str, str] = {}

    runnable = []
    for name in selected:
        applies, reason = _applicable(name, profile)
        if applies:
            runnable.append(name)
        else:
            not_applicable[name] = reason

    # DRF checks need the app registry as much as the plain Django ones.
    _NEEDS_BOOT = {"django", "drf"}
    if any(_REQUIRES.get(name) in _NEEDS_BOOT for name in runnable):
        try:
            ensure_django()
        except DjangoBootError as exc:
            for name in list(runnable):
                if _REQUIRES.get(name) in _NEEDS_BOOT:
                    runnable.remove(name)
                    not_applicable[name] = f"Django could not be loaded: {exc}"

    # Twenty analyses over one unchanged tree: parsing it once instead of
    # twenty times is the single biggest saving available here.
    enable_source_cache()
    total = len(runnable)
    for index, name in enumerate(runnable):
        if on_progress is not None:
            on_progress(index, total, name)
        fn, build_kwargs, adapt = _CHECKS[name]
        started = time.perf_counter()
        try:
            report = fn(**build_kwargs(options))
            produced = adapt(report)
            findings.extend(produced)
            ran[name] = {
                "ok": True,
                "findings": len(produced),
                "examined": _examined(name, report),
                "seconds": round(time.perf_counter() - started, 2),
            }
        except Exception as exc:
            ran[name] = {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "seconds": round(time.perf_counter() - started, 2),
            }

    if on_progress is not None:
        on_progress(total, total, "")

    findings.sort(key=lambda f: (_ORDER.get(f["severity"], 9), f["check"], f["location"] or ""))

    by_severity: dict[str, int] = {}
    for finding in findings:
        by_severity[finding["severity"]] = by_severity.get(finding["severity"], 0) + 1

    failed = [name for name, state in ran.items() if not state["ok"]]
    warning = _registry_warning()
    return {
        "registry_warning": warning,
        "frameworks": dict(profile.frameworks) if profile is not None else {},
        "checks_not_applicable": not_applicable,
        "checks_run": ran,
        "checks_failed": failed,
        "finding_count": len(findings),
        "by_severity": by_severity,
        "findings": findings,
        "note": (
            "Merged output from several candidate-producing analyses. Each "
            "carries its own limits, documented with the individual tool. A "
            "check that could not run is listed in checks_failed rather than "
            "being counted as clean. A check that ran and found nothing "
            "carries `examined`: what it looked at, so a zero can be read as "
            "clean rather than blind. An empty `examined` means the check "
            "does not report its own coverage, and that zero cannot be "
            "interpreted either way. Each entry also carries `seconds`: on a "
            "large project a full run is minutes, and the cost is not spread "
            "evenly, so this is what `--skip` should be aimed at."
            + (f" {len(failed)} check(s) failed to run." if failed else "")
        ),
    }


def gate(report: dict[str, Any], threshold: str = GATE_DEFAULT) -> bool:
    """True when anything at or above the threshold was found."""
    limit = _ORDER.get(threshold, 1)
    return any(_ORDER.get(f["severity"], 9) <= limit for f in report["findings"])
