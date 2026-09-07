# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Command line entry point, so the analysis can gate a pipeline.

The MCP server is for asking questions while working. CI needs the same checks
with an exit code, which is what turns deploy_safety from something you consult
into something that stops a bad deploy.

Exit codes:
    0  nothing found at or above the configured threshold
    1  findings at or above the threshold
    2  the project could not be loaded, or the arguments were wrong
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

from . import baseline as _baseline
from . import config as _config
from . import fixes as _fixes
from . import gitdiff as _gitdiff
from . import sarif as _sarif
from .aggregates import multiplied_aggregates
from .amplification import amplification
from .api_contract import CONTRACT_FILE, contract, load_snapshot, write_snapshot
from .api_contract import diff as contract_diff
from .asyncio_blocking import blocking_in_async
from .bypass import bypassed_effects
from .cascade import delete_impact
from .celery_tasks import celery_arguments
from .check import ALL_CHECKS, GATE_DEFAULT, gate, run_all
from .choices import choice_typos
from .concurrency import race_conditions
from .dangling import dangling_references
from .datetimes import datetime_audit
from .deploy_safety import deploy_safety
from .django_env import (
    PROJECT_PATH_VAR,
    SETTINGS_MODULE_VAR,
    BootConfig,
    DjangoBootError,
    ensure_django,
)
from .endpoint_cost import endpoint_cost
from .explain import explain_model
from .exposure_auth import open_endpoints
from .fastapi_exposure import fastapi_exposure
from .impact import impact
from .indexes import missing_indexes
from .introspect import list_models
from .loop_queries import queries_in_loops
from .migrations import migration_risk
from .money import money_precision
from .on_commit import escaping_side_effects
from .overfetch import unused_eager_loading
from .scan import scan_templates
from .serializer_nplusone import serializer_nplusone
from .serializers import serializer_exposure
from .signals import what_happens_on
from .sqlalchemy_nplusone import sqlalchemy_nplusone
from .tenancy import find_unscoped_queries

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


# Commands that read source and nothing else. Booting Django for these turned
# a question about Python into a question about settings, and refused to
# analyse a FastAPI project for reasons that had nothing to do with the ask.
_FRAMEWORK_FREE = {"async", "profile", "routes", "sqla"}

# `check` runs whatever applies. On a project with no Django it still has the
# framework-independent checks to run, so a missing settings module drops
# those checks rather than the whole command - run_all reports each one it
# could not run and why.
_DEGRADES_WITHOUT_DJANGO = {"check"}


def _bootstrap(args: argparse.Namespace) -> None:
    if args.project_path:
        os.environ[PROJECT_PATH_VAR] = args.project_path
    if args.settings:
        os.environ[SETTINGS_MODULE_VAR] = args.settings

    if getattr(args, "command", None) in _FRAMEWORK_FREE:
        from .project import project_root

        args._project_config = _config.load(project_root())
        return

    if getattr(args, "command", None) in _DEGRADES_WITHOUT_DJANGO:
        from .project import project_root

        try:
            config = ensure_django(BootConfig.from_env())
        except DjangoBootError:
            args._project_config = _config.load(project_root())
            return
    else:
        config = ensure_django(BootConfig.from_env())

    # pyproject.toml supplies what the flags did not. A flag always wins,
    # because "why is it using the wrong tenant root" is a bad afternoon and
    # the answer must be visible in the command that was typed.
    project = _config.load(config.project_path)
    args._project_config = project

    if project.source:
        if getattr(args, "tenant_root", None) == "auth.User" and project.tenant_root != "auth.User":
            args.tenant_root = project.tenant_root
        if getattr(args, "fail_on", None) == "high" and project.fail_on != "high":
            args.fail_on = project.fail_on
        if getattr(args, "baseline", None) is None and project.baseline:
            args.baseline = project.baseline
        if project.skip and not getattr(args, "skip", None):
            args.skip = list(project.skip)


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))


def _restrict_to_changes(
    args: argparse.Namespace,
    findings: list[dict[str, Any]],
    base_dir: Path,
    key: str = "file",
) -> tuple[list[dict[str, Any]], bool]:
    """Narrow findings to files this branch touched.

    Returns the findings to act on and whether narrowing happened, so the
    caller can say so instead of silently reporting a smaller number.
    """
    ref = getattr(args, "since", None)
    if not ref:
        return findings, False

    try:
        diff = _gitdiff.changed_files(ref, base_dir)
    except _gitdiff.GitError as exc:
        print(f"warning: --since {ref} ignored, git said: {exc}", file=sys.stderr)
        return findings, False

    root = Path(diff["repo_root"])
    matched, unmatchable = _gitdiff.filter_findings(
        findings, set(diff["changed_files"]), base_dir, root, key=key
    )

    print(f"Since {ref} ({diff['strategy']}): "
          f"{diff['changed_count']} changed file(s), "
          f"{len(matched)} of {len(findings)} finding(s) fall in them")
    if unmatchable:
        print(f"  {len(unmatchable)} finding(s) could not be matched to a path and are kept:")
        for finding in unmatchable[:5]:
            print(f"    {finding.get(key, '?')}")
    print()
    return matched + unmatchable, True


def _apply_baseline(check: str, report: dict[str, Any], args: argparse.Namespace) -> int | None:
    """Handle --baseline and --update-baseline for one check.

    Returns an exit code when the baseline decided the outcome, or None when
    the caller should fall back to its own gate.
    """
    path = getattr(args, "baseline", None)
    update = getattr(args, "update_baseline", False)

    if update:
        written = _baseline.write(path or _baseline.DEFAULT_BASELINE, check, report)
        print()
        print(f"Baseline written to {written['baseline']}: "
              f"{written['recorded']} finding(s) recorded for '{check}'.")
        print("Commit it. From now on the gate fails on new findings only.")
        return EXIT_OK

    if not path:
        return None

    diff = _baseline.compare(check, report, _baseline.load(path))
    print()
    if not diff["has_baseline"]:
        print(f"No baseline recorded for '{check}' in {path}.")
        print(f"Run again with --update-baseline to record today's "
              f"{diff['new_count']} finding(s).")
        return EXIT_OK

    print(f"Against {path} (recorded {diff['baseline_recorded_at']}): "
          f"{diff['new_count']} new, {diff['unchanged_count']} known, "
          f"{diff['fixed_count']} fixed")

    if diff["fixed"]:
        print()
        print("Fixed since the baseline, regenerate it to lock this in:")
        for entry in diff["fixed"][:20]:
            print(f"  - {entry['file']}  {entry['summary']}")

    if diff["new"]:
        print()
        print("NEW findings, not in the baseline:")
        for entry in diff["new"]:
            location = f"{entry['file']}:{entry['line']}" if entry.get("line") else entry["file"]
            print(f"  {entry['severity']:<7} {location}")
            print(f"          {entry['summary']}")
        return EXIT_FINDINGS

    return EXIT_OK


def _cmd_cost(args: argparse.Namespace) -> int:
    report = endpoint_cost(
        page_size=args.page_size,
        nested_fan_out=args.fan_out,
        list_only=args.list_only,
    )
    _emit(report, args.json)

    if not args.json:
        if not report["rest_framework_installed"]:
            print("No DRF views are importable, nothing to estimate.")
            return EXIT_OK
        print(f"{report['endpoint_count']} endpoint(s), page size "
              f"{report['page_size']}, assuming {report['nested_fan_out']} "
              "child object(s) per parent")
        print()
        for endpoint in report["endpoints"]:
            prefix = "at least " if endpoint["at_least"] else ""
            print(f"  {endpoint['estimated_queries']:>8}  {prefix}"
                  f"{endpoint['view']}  ({endpoint['kind']})")
            if endpoint["location"]:
                print(f"            {endpoint['location']}")
            for item in endpoint["breakdown"]:
                if item["queries"] == 0:
                    continue
                count = "?" if item["queries"] is None else item["queries"]
                print(f"            {count:>8}  {item['reason']}")
            print()
        if report["unpaginated_list_endpoints"]:
            print(f"{len(report['unpaginated_list_endpoints'])} list endpoint(s) with no pagination "
                  "return the whole table; their estimates above are floors:")
            for view in report["unpaginated_list_endpoints"]:
                print(f"    {view}")
            print()
        print(report["note"])

    if args.max_queries is not None and report["worst_estimate"] > args.max_queries:
        return EXIT_FINDINGS
    return EXIT_OK


_KIND_LABEL = {
    "breaking": "BREAKS CLIENTS",
    "unreadable": "COULD NOT BE READ",
    "risky": "risky",
    "additive": "safe",
    "neutral": "no effect",
}


def _cmd_contract(args: argparse.Namespace) -> int:
    captured = contract(max_depth=args.max_depth)

    if not captured.get("rest_framework_installed"):
        _emit(captured, args.json)
        if not args.json:
            print(captured["note"])
        return EXIT_OK

    if args.update:
        written = write_snapshot(args.snapshot, captured)
        _emit(written, args.json)
        if not args.json:
            print(f"Wrote {written['serializers']} serializer(s) to {written['snapshot']}")
            print(written["note"])
        return EXIT_OK

    baseline = load_snapshot(args.snapshot)
    if baseline is None:
        message = {
            "ok": False,
            "snapshot": args.snapshot,
            "error": (
                "No contract snapshot yet. Run with --update on a branch whose "
                "API shape is the one clients already use, then commit the file."
            ),
        }
        _emit(message, args.json)
        if not args.json:
            print(message["error"])
        return EXIT_ERROR

    report = contract_diff(baseline, captured)
    _emit(report, args.json)

    if not args.json:
        if not report["change_count"]:
            print("The API shape is unchanged.")
            return EXIT_OK

        summary = ", ".join(
            f"{count} {_KIND_LABEL.get(kind, kind).lower()}"
            for kind, count in report["by_kind"].items()
        )
        print(f"{report['change_count']} change(s) since the snapshot: {summary}")
        print()
        current_kind = None
        for change in report["changes"]:
            if change["kind"] != current_kind:
                current_kind = change["kind"]
                print(f"  {_KIND_LABEL.get(current_kind, current_kind)}")
            print(f"    {change['path']}")
            print(f"        {change['change']} - {change['why']}")
        print()
        print(report["note"])

    # A serializer that no longer resolves also fails the gate. Its effect on
    # clients is unknown, and an unknown is not a pass.
    if args.fail_on_breaking and (report["breaking_count"] or report["unreadable_count"]):
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_oncommit(args: argparse.Namespace) -> int:
    report = escaping_side_effects(
        search_path=args.search_path,
        include_low_confidence=args.include_low_confidence,
    )
    _emit(report, args.json)

    if not args.json:
        if report["atomic_requests_databases"]:
            print("ATOMIC_REQUESTS is on for: "
                  + ", ".join(report["atomic_requests_databases"]))
            print("Every view runs inside a transaction, with no atomic() block")
            print("anywhere in sight.")
            print()
            if report["atomic_request_view_count"]:
                print(f"{report['atomic_request_view_count']} send(s) inside a view, "
                      "wrapped by that setting alone:")
                for entry in report["atomic_request_views"]:
                    print(f"    {entry['file']}:{entry['line']}  {entry['kind']}")
                    print(f"        {entry['code']}")
                    print(f"        via {entry['view']}")
                print()

        if not report["finding_count"]:
            print(f"No escaping side effects in {report['files_scanned']} file(s).")
            return EXIT_OK

        print(f"{report['finding_count']} call(s) inside a transaction that "
              "cannot be rolled back")
        print()
        for finding in report["findings"]:
            flag = " (low confidence)" if finding["confidence"] == "low" else ""
            print(f"  {finding['severity'].upper():<6} {finding['file']}:{finding['line']}"
                  f"  {finding['kind']}{flag}")
            print(f"         {finding['code']}")
            print(f"         inside {finding['inside']}")
            if finding.get("reached_through"):
                # The path is the actionable part. A finding in services.py is
                # unactionable without the caller that made it a problem.
                print("         path:  " + " -> ".join(finding["reached_through"]))
            print(f"         {finding['consequence']}")
            print(f"         fix: {finding['fix']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["high_severity_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_bypass(args: argparse.Namespace) -> int:
    report = bypassed_effects(search_path=args.search_path, model=args.model)
    _emit(report, args.json)

    if not args.json:
        if not report["finding_count"]:
            print(f"No bulk write skips a save() chain "
                  f"({report['bulk_writes_seen']} bulk write(s) seen, all on models "
                  "with nothing to skip).")
        else:
            print(f"{report['finding_count']} bulk write(s) that skip a save() chain")
            print()
            for f in report["findings"]:
                print(f"  {f['severity'].upper():<6} {f['file']}:{f['line']}  "
                      f"{f['model']} via .{f['method']}()")
                print(f"         {f['code']}")
                print(f"         {f['what_is_skipped']}")
                if f["overrides_not_run"]:
                    print(f"         save() not run   : {', '.join(f['overrides_not_run'])}")
                if f["receivers_not_fired"]:
                    print(f"         receivers skipped: {', '.join(f['receivers_not_fired'])}")
                if f["models_not_written"]:
                    print(f"         never written    : {', '.join(f['models_not_written'])}")
                print()
        if report["bulk_writes_on_unresolved_model"]:
            print(f"{report['bulk_writes_on_unresolved_model']} bulk write(s) on a queryset "
                  "held in a variable: model unknown, not reported.")
        print()
        print(report["note"])

    if args.fail_on_findings and report["high_severity_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_races(args: argparse.Namespace) -> int:
    report = race_conditions(
        search_path=args.search_path,
        include_parameters=not args.no_parameters,
    )
    _emit(report, args.json)

    if not args.json:
        if not report["finding_count"]:
            print(f"No read-modify-save races, no unprotected row locks and no unsafe upserts in "
                  f"{report['files_scanned']} file(s).")
        if report["races"]:
            print(f"{report['race_count']} read-modify-save race(s)")
            print()
            for f in report["races"]:
                flag = "" if f["confidence"] == "high" else f" ({f['confidence']} confidence: {f['instance_origin']})"
                print(f"  {f['file']}:{f['line']}  {f['instance']}.{f['field']} in {f['function']}{flag}")
                print(f"         {f['code']}")
                print(f"         saved at line {f['saved_at_line']}; {f['why']}")
                print(f"         fix: {f['fix']}")
                print()
        if report["unsafe_upserts"]:
            print(f"{report['unsafe_upsert_count']} get_or_create/update_or_create on a lookup nothing makes unique")
            print()
            for f in report["unsafe_upserts"]:
                print(f"  {f['file']}:{f['line']}  {f['model']}.{f['method']}({', '.join(f['lookup'])}=...)")
                print(f"         {f['code']}")
                unique = ", ".join("+".join(g) for g in f["unique_on_model"]) or "nothing"
                print(f"         unique on {f['model'].split('.')[-1]}: {unique}")
                print(f"         {f['why']}")
                print(f"         fix: {f['fix']}")
                print()
        if report["locks_outside_transaction"]:
            print(f"{report['unlocked_lock_count']} select_for_update() with no transaction to hold the lock")
            print()
            for f in report["locks_outside_transaction"]:
                print(f"  {f['file']}:{f['line']}  in {f['function']}")
                print(f"         {f['code']}")
                print(f"         {f['why']}")
                print(f"         fix: {f['fix']}")
                print()
        print(report["note"])

    if args.fail_on_findings and report["high_confidence_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_open(args: argparse.Namespace) -> int:
    report = open_endpoints(include_unbounded=not args.sensitive_only)
    _emit(report, args.json)

    if not args.json:
        if not report["rest_framework_installed"]:
            print(report["note"])
            return EXIT_OK
        print(f"Default permission: {', '.join(report['default_permission_classes'])} "
              f"({report['default_permission_source']})")
        if report["default_is_open"]:
            print("  -> every view without its own permission_classes is public.")
        print(f"{report['views_checked']} view(s): {report['open_view_count']} open, "
              f"{report['protected_view_count']} protected, "
              f"{len(report['views_deciding_at_runtime'])} deciding at runtime")
        print()
        if not report["finding_count"]:
            print("No open endpoint exposes anything sensitive or unbounded.")
        for f in report["findings"]:
            print(f"  {f['severity'].upper():<9} {f['view']}")
            print(f"            permission: {', '.join(f['permission_classes'])} ({f['permission_source']})")
            print(f"            serializer: {f['serializer']}")
            print(f"            {f['why']}")
            print(f"            fix: {f['fix']}")
            print()
        if report["views_deciding_at_runtime"]:
            print("Deciding permissions at runtime (get_permissions), not judged:")
            for v in report["views_deciding_at_runtime"]:
                print(f"    {v}")
            print()
        failed = report["view_discovery"].get("failed", [])
        if failed:
            print("View modules that could not be imported, so their views are not here:")
            for entry in failed:
                print(f"    {entry['module']}: {entry['error']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["critical_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_overfetch(args: argparse.Namespace) -> int:
    report = unused_eager_loading(include_low_confidence=args.include_low_confidence)
    _emit(report, args.json)

    if not args.json:
        if not report["rest_framework_installed"]:
            print(report["note"])
            return EXIT_OK
        if not report["finding_count"]:
            print(f"Nothing loaded and unread across {report['views_checked']} view(s).")
        else:
            print(f"{report['finding_count']} relation(s) loaded and never read")
            print()
            for f in report["findings"]:
                flag = "" if f["confidence"] == "high" else f"  (low confidence: {', '.join(f['unreadable_because'])})"
                print(f"  {f['severity'].upper():<6} {f['view']}{flag}")
                print(f"         .{f['kind']}(\"{f['path']}\")")
                print(f"         {f['why']}")
                print(f"         reads: {', '.join(f['serializer_reads']) or 'no relation at all'}")
                print(f"         fix: {f['fix']}")
                print()
        if report["views_with_runtime_paths"]:
            print(f"{report['views_with_runtime_paths']} view(s) build their paths at runtime "
                  "and were skipped.")
            print()
        print(report["note"])

    if args.fail_on_findings and report["high_confidence_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_money(args: argparse.Namespace) -> int:
    report = money_precision(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        if not report["finding_count"]:
            print(f"No decimal amount loses exactness in {report['files_scanned']} file(s).")
        else:
            print(f"{report['finding_count']} place(s) where a decimal amount stops being exact "
                  f"({report['high_severity_count']} high)")
            print()
            for finding in report["findings"]:
                if finding["severity"] == "low" and not args.include_low:
                    continue
                where = finding.get("target") or f"{finding.get('file')}:{finding.get('line')}"
                print(f"  {finding['severity'].upper():<6} {where}  [{finding['kind']}]")
                if finding.get("code"):
                    print(f"         {finding['code']}")
                print(f"         {finding['detail']}")
                print(f"         fix: {finding['fix']}")
                print()
            low = sum(1 for f in report["findings"] if f["severity"] == "low")
            if low and not args.include_low:
                print(f"{low} low-severity finding(s) hidden; --include-low shows them. "
                      "Those are floats that happen to be exactly representable, so "
                      "nothing is lost today.")
                print()
        print(report["note"])

    if args.fail_on_findings and report["high_severity_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_profile(args: argparse.Namespace) -> int:
    from .project import get_profile, resolve_root

    report = get_profile(resolve_root(args.search_path)).as_dict()
    _emit(report, args.json)
    if not args.json:
        print(f"{report['files_scanned']} Python file(s) under {report['root']}")
        print(f"{report['async_functions']} async function(s), "
              f"{report['sync_functions']} sync")
        print()
        if not report["frameworks"]:
            print("No known framework imported by this project's own files.")
        for name, count in report["frameworks"].items():
            print(f"  {name:<14} imported by {count} file(s)")
        print()
        print(report["note"])
    return EXIT_OK


def _cmd_async(args: argparse.Namespace) -> int:
    report = blocking_in_async(
        search_path=args.search_path,
        follow_calls=not args.no_follow,
        max_depth=args.max_depth,
    )
    _emit(report, args.json)

    if not args.json:
        frameworks = ", ".join(report["frameworks"]) or "none detected"
        print(f"{report['async_functions_seen']} async function(s) in "
              f"{report['files_scanned']} file(s)  [{frameworks}]")
        print()
        if not report["finding_count"]:
            print("Nothing blocking runs on the event loop.")
        for f in report["direct"]:
            print(f"  HIGH   {f['file']}:{f['line']}  in async {f['function']}()  [{f['kind']}]")
            print(f"         {f['code']}")
            print(f"         {f['why']}")
            print(f"         fix: {f['fix']}")
            print()
        for f in report["reached_through_a_call"]:
            print(f"  HIGH   {f['file']}:{f['line']}  [{f['kind']}]  {f['call']}")
            print(f"         reached from async {f['function']}()")
            print("         path:  " + " -> ".join(f["reached_through"]))
            print(f"         {f['why']}")
            print(f"         fix: {f['fix']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_routes(args: argparse.Namespace) -> int:
    report = fastapi_exposure(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['routes_found']} route(s), {report['models_found']} model(s)")
        print()
        if not report["finding_count"]:
            print("Every route declares what it returns.")
        for f in report["findings"]:
            auth = "authenticated" if f["authenticated"] else "no authentication"
            print(f"  {f['severity'].upper():<9} {f['method']} {f['path']}  "
                  f"-> {f['endpoint']}()  [{auth}]")
            print(f"            {f['file']}:{f['line']}")
            print(f"            {f['why']}")
            print(f"            fix: {f['fix']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["critical_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_sqla(args: argparse.Namespace) -> int:
    report = sqlalchemy_nplusone(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['relationship_count']} relationship(s) across "
              f"{report['models_with_relationships']} model(s)")
        print()
        if not report["finding_count"]:
            print("No relationship is loaded one row at a time.")
        if report["in_loops"]:
            print(f"{report['in_loop_count']} in a loop:")
            print()
            for f in report["in_loops"]:
                print(f"  HIGH   {f['file']}:{f['line']}  in {f['function']}()")
                print(f"         {f['code']}")
                print(f"         {f['why']}")
                print(f"         fix: {f['fix']}")
                print()
        if report["in_response_models"]:
            print(f"{report['in_response_model_count']} during response serialisation:")
            print()
            for f in report["in_response_models"]:
                print(f"  HIGH   {f['file']}:{f['line']}  in {f['function']}()")
                print(f"         {f['response_model']} -> {f['model']}.{f['attribute']}")
                print(f"         {f['why']}")
                print(f"         fix: {f['fix']}")
                print()
        print(report["note"])

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_amplification(args: argparse.Namespace) -> int:
    report = amplification(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['open_endpoints_considered']} endpoint(s) reachable without "
              "credentials were considered")
        print()
        if not report["finding_count"]:
            print("None of them is expensive enough to be worth abusing.")
        for f in report["findings"]:
            cost = (f"~{f['estimated_queries']} queries" if f.get("estimated_queries")
                    else ", ".join(f.get("relationships_per_row", [])) + " per row")
            print(f"  {f['severity'].upper():<9} {f['endpoint']}  ({cost})")
            if f.get("location"):
                print(f"            {f['location']}")
            print(f"            {f['why']}")
            print(f"            fix: {f['fix']}")
            print()
        if report["checks_that_could_not_run"]:
            print("Half of this question could not be answered:")
            for problem in report["checks_that_could_not_run"]:
                print(f"    {problem}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["critical_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_celery(args: argparse.Namespace) -> int:
    report = celery_arguments(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['tasks_found']} task(s), {report['dispatches_checked']} dispatch(es) "
              "resolved to one of them")
        print()
        if not report["finding_count"]:
            print("Every dispatch passes what its task asked for.")
        for f in report["instance_arguments"]:
            print(f"  HIGH   {f['file']}:{f['line']}  {f['task']}({f['parameter']})")
            print(f"         {f['code']}")
            print(f"         {f['why']}")
            print(f"         fix: {f['fix']}")
            print()
        for f in report["arity_mismatches"]:
            print(f"  HIGH   {f['file']}:{f['line']}  {f['task']} given {f['given']}, "
                  f"expects {f['expects']}")
            print(f"         {f['code']}")
            print(f"         {f['why']}")
            print(f"         fix: {f['fix']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_dangling(args: argparse.Namespace) -> int:
    report = dangling_references(
        search_path=args.search_path,
        include_templates=not args.skip_templates,
    )
    _emit(report, args.json)

    if not args.json:
        print(f"{report['url_names_registered']} URL name(s) across "
              f"{report['urlconfs_read']} URLconf(s), "
              f"{report['task_names_known']} task name(s).")
        print(f"Checked {report['url_names_checked']} URL reference(s), "
              f"{report['template_names_checked']} template name(s), "
              f"{report['signal_senders_checked']} signal sender(s), "
              f"{report['task_names_checked']} task name(s).")
        print()
        if not report["finding_count"]:
            print("Every name resolves.")
        for group in report["by_name"]:
            print(f"  {group['severity'].upper():<9} {group['kind']:<9} "
                  f"{group['name']!r}  ({group['uses']} use(s))")
            for where in group["files"]:
                print(f"            {where}")
            if group["uses"] > len(group["files"]):
                print(f"            ... and {group['uses'] - len(group['files'])} more")
            print(f"            {group['why']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_aggregates(args: argparse.Namespace) -> int:
    report = multiplied_aggregates(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['annotate_calls_in_source']} annotate()/aggregate() call(s) "
              f"in {report['files_scanned']} file(s); "
              f"{report['aggregates_seen']} Count/Sum over a multi-valued "
              "relation resolved to a model.")
        print()
        if not report["finding_count"] and not report["filter_count"]:
            print("No query joins two multi-valued relations.")
        for finding in report["findings"]:
            print(f"  {finding['severity'].upper():<9} {finding['file']}:{finding['line']}"
                  f"  {finding['model']}")
            print(f"            {', '.join(finding['aggregates'])}")
            print(f"            {finding['why']}")
            print(f"            fix: {finding['fix']}")
            print()
        for finding in report["multiplied_by_a_filter"]:
            print(f"  {finding['severity'].upper():<9} {finding['file']}:{finding['line']}"
                  f"  {finding['model']} (multiplied by a filter)")
            print(f"            {finding['why']}")
            print(f"            fix: {finding['fix']}")
            print()
        print(report["note"])

    if args.fail_on_findings and (report["finding_count"] or report["filter_count"]):
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_choices(args: argparse.Namespace) -> int:
    report = choice_typos(
        search_path=args.search_path,
        include_tests=not args.skip_tests,
    )
    _emit(report, args.json)

    if not args.json:
        if not report["finding_count"]:
            print(f"Every literal matches its field's choices "
                  f"({report['literals_checked']} checked across "
                  f"{report['fields_with_choices']} field(s)).")
        for finding in report["findings"]:
            print(f"  {finding['severity'].upper():<9} {finding['file']}:{finding['line']}"
                  f"  {finding['model']}.{finding['field']}")
            print(f"            {finding['code']}")
            print(f"            {finding['why']}")
            print(f"            fix: {finding['fix']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_routes(args: argparse.Namespace) -> int:
    report = fastapi_exposure(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['routes_found']} route(s), {report['models_found']} model(s)")
        print()
        if not report["finding_count"]:
            print("Every route declares what it returns.")
        for f in report["findings"]:
            auth = "authenticated" if f["authenticated"] else "no authentication"
            print(f"  {f['severity'].upper():<9} {f['method']} {f['path']}  "
                  f"-> {f['endpoint']}()  [{auth}]")
            print(f"            {f['file']}:{f['line']}")
            print(f"            {f['why']}")
            print(f"            fix: {f['fix']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["critical_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_sqla(args: argparse.Namespace) -> int:
    report = sqlalchemy_nplusone(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['relationship_count']} relationship(s) across "
              f"{report['models_with_relationships']} model(s)")
        print()
        if not report["finding_count"]:
            print("No relationship is loaded one row at a time.")
        if report["in_loops"]:
            print(f"{report['in_loop_count']} in a loop:")
            print()
            for f in report["in_loops"]:
                print(f"  HIGH   {f['file']}:{f['line']}  in {f['function']}()")
                print(f"         {f['code']}")
                print(f"         {f['why']}")
                print(f"         fix: {f['fix']}")
                print()
        if report["in_response_models"]:
            print(f"{report['in_response_model_count']} during response serialisation:")
            print()
            for f in report["in_response_models"]:
                print(f"  HIGH   {f['file']}:{f['line']}  in {f['function']}()")
                print(f"         {f['response_model']} -> {f['model']}.{f['attribute']}")
                print(f"         {f['why']}")
                print(f"         fix: {f['fix']}")
                print()
        print(report["note"])

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_amplification(args: argparse.Namespace) -> int:
    report = amplification(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['open_endpoints_considered']} endpoint(s) reachable without "
              "credentials were considered")
        print()
        if not report["finding_count"]:
            print("None of them is expensive enough to be worth abusing.")
        for f in report["findings"]:
            cost = (f"~{f['estimated_queries']} queries" if f.get("estimated_queries")
                    else ", ".join(f.get("relationships_per_row", [])) + " per row")
            print(f"  {f['severity'].upper():<9} {f['endpoint']}  ({cost})")
            if f.get("location"):
                print(f"            {f['location']}")
            print(f"            {f['why']}")
            print(f"            fix: {f['fix']}")
            print()
        if report["checks_that_could_not_run"]:
            print("Half of this question could not be answered:")
            for problem in report["checks_that_could_not_run"]:
                print(f"    {problem}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["critical_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_celery(args: argparse.Namespace) -> int:
    report = celery_arguments(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['tasks_found']} task(s), {report['dispatches_checked']} dispatch(es) "
              "resolved to one of them")
        print()
        if not report["finding_count"]:
            print("Every dispatch passes what its task asked for.")
        for f in report["instance_arguments"]:
            print(f"  HIGH   {f['file']}:{f['line']}  {f['task']}({f['parameter']})")
            print(f"         {f['code']}")
            print(f"         {f['why']}")
            print(f"         fix: {f['fix']}")
            print()
        for f in report["arity_mismatches"]:
            print(f"  HIGH   {f['file']}:{f['line']}  {f['task']} given {f['given']}, "
                  f"expects {f['expects']}")
            print(f"         {f['code']}")
            print(f"         {f['why']}")
            print(f"         fix: {f['fix']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_choices(args: argparse.Namespace) -> int:
    report = choice_typos(
        search_path=args.search_path,
        include_tests=not args.skip_tests,
    )
    _emit(report, args.json)

    if not args.json:
        if not report["finding_count"]:
            print(f"Every literal matches its field's choices "
                  f"({report['literals_checked']} checked across "
                  f"{report['fields_with_choices']} field(s)).")
        for finding in report["findings"]:
            print(f"  {finding['severity'].upper():<9} {finding['file']}:{finding['line']}"
                  f"  {finding['model']}.{finding['field']}")
            print(f"            {finding['code']}")
            print(f"            {finding['why']}")
            print(f"            fix: {finding['fix']}")
            print()
        for finding in report["untyped_comparisons"]:
            print(f"  {finding['severity'].upper():<9} {finding['file']}:{finding['line']}"
                  f"  .{finding['field']} (model unknown)")
            print(f"            {finding['code']}")
            print(f"            {finding['why']}")
            print()
        print(report["note"])

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_impact(args: argparse.Namespace) -> int:
    report = impact(
        search_path=args.search_path,
        max_depth=args.max_depth,
        tenant_root=args.tenant_root,
    )
    _emit(report, args.json)

    if not args.json:
        kinds = ", ".join(f"{n} {k}" for k, n in report["entry_points_by_kind"].items())
        print(f"{report['entry_points_found']} entry point(s): {kinds or 'none'}")
        print(f"{report['findings_considered']} finding(s) considered, "
              f"{report['entry_points_with_findings']} entry point(s) carry one.")
        print()
        for entry in report["entry_points"][:args.top]:
            print(f"  {entry['worst'].upper():<9} {entry['finding_count']:>3}  "
                  f"{entry['label']}")
            for finding in entry["findings"][:args.per_entry]:
                print(f"            {finding['severity']:<9} {finding['check']:<16} "
                      f"{finding['location']}")
                print(f"                      {finding['title']}")
                if len(finding["through"]) > 1:
                    hops = " -> ".join(q.rsplit(".", 1)[-1] for q in finding["through"])
                    print(f"                      via {hops}")
            extra = entry["finding_count"] - args.per_entry
            if extra > 0:
                print(f"            ... and {extra} more")
            print()
        unknown = report["unattributed_because_the_receiver_is_unknown"]
        print(f"{report['unattributed_count']} finding(s) reached by no entry point "
              f"this can see, {report['without_a_location_count']} with no file and "
              "line to attribute.")
        if unknown:
            print(f"  of those, {unknown} "
                  + ("sits" if unknown == 1 else "sit")
                  + " in a method that something calls by name, on an object "
                    "this cannot identify: not unreached, unresolved.")
        print()
        print(report["note"])

    if args.fail_on_findings and any(
        e["worst"] in ("critical", "high") for e in report["entry_points"]
    ):
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_loops(args: argparse.Namespace) -> int:
    report = queries_in_loops(
        search_path=args.search_path,
        include_writes=not args.no_writes,
        follow_calls=not args.no_follow_calls,
        max_depth=args.max_depth,
    )
    _emit(report, args.json)

    if not args.json:
        if not report["finding_count"]:
            print(f"No database work inside a loop in {report['files_scanned']} file(s).")
        for bucket, heading in (
            (report["per_row"], "once per row"),
            (report["loop_invariant"], "the same query every iteration"),
            (report["writes_in_loops"], "a write per row"),
            (report["through_a_call"], "a query in a function the loop calls"),
        ):
            if not bucket:
                continue
            print(f"{len(bucket)} {heading}:")
            print()
            for f in bucket:
                print(f"  {f['severity'].upper():<9} {f['file']}:{f['line']}  "
                      f"(loop at line {f['loop_at_line']})")
                print(f"            {f['code']}")
                print(f"            {f['why']}")
                if f.get("reached_through"):
                    print(f"            path: {' -> '.join(f['reached_through'])}")
                print(f"            fix: {f['fix']}")
                print()
        print(report["note"])

    if args.fail_on_findings and report["high_severity_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_fix(args: argparse.Namespace) -> int:
    from .django_env import ensure_django

    config = ensure_django()
    root = Path(config.project_path)

    reports = {}
    if "datetimes" not in (args.skip or []):
        reports["datetimes"] = datetime_audit()
    if "serializers" not in (args.skip or []):
        reports["serializers"] = serializer_exposure()
    if "indexes" not in (args.skip or []):
        reports["indexes"] = missing_indexes()
    if "tenancy" not in (args.skip or []):
        reports["tenancy"] = find_unscoped_queries(tenant_root=args.tenant_root)

    fixset = _fixes.build_fixes(reports, root)

    mechanical = fixset.by_kind(_fixes.MECHANICAL)
    generated = fixset.by_kind(_fixes.GENERATED)
    advisory = fixset.by_kind(_fixes.ADVISORY)

    if args.json:
        print(json.dumps(
            {
                "mechanical": [f.__dict__ for f in mechanical],
                "generated": [f.__dict__ for f in generated],
                "advisory": [f.__dict__ for f in advisory],
            },
            indent=2, default=str,
        ))
    else:
        print(f"{len(mechanical)} mechanical, {len(generated)} generated, "
              f"{len(advisory)} advisory")
        print()

        if mechanical:
            print("MECHANICAL  one correct answer, safe to apply")
            print("-" * 60)
            for fix in mechanical:
                print(f"  [{fix.check}] {fix.title}")
                if fix.path:
                    print(f"      {fix.path}:{fix.line}")
                diff = fix.diff(root)
                for line in diff.splitlines():
                    if line.startswith(("+++", "---", "@@")):
                        continue
                    print(f"      {line}")
                if fix.extra_import:
                    print(f"      + {fix.extra_import}")
                print()

        if generated:
            print("GENERATED  a machine can write it, a human decides if it should exist")
            print("-" * 60)
            for fix in generated:
                print(f"  [{fix.check}] {fix.title}")
                if fix.new_file:
                    print(f"      would create {fix.new_file}")
                if fix.why:
                    print(f"      {fix.why}")
                if fix.caution:
                    print(f"      caution: {fix.caution}")
                print()

        if advisory:
            print("ADVISORY  real code, but the decision is yours")
            print("-" * 60)
            for fix in advisory:
                print(f"  [{fix.check}] {fix.title}")
                if fix.path:
                    print(f"      {fix.path}:{fix.line}")
                if fix.old and fix.new:
                    print(f"      - {fix.old.strip()}")
                    print(f"      + {fix.new.strip()}")
                elif fix.new:
                    print(f"      {fix.new}")
                if fix.caution:
                    print(f"      caution: {fix.caution}")
                print()

    if args.write_generated and generated:
        written = []
        for fix in generated:
            if not (fix.new_file and fix.new_file_content):
                continue
            target = root / fix.new_file
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fix.new_file_content, encoding="utf-8")
            written.append(str(fix.new_file))
        if written and not args.json:
            print(f"Wrote {len(written)} generated file(s):")
            for name in written:
                print(f"  {name}")
            print("Review them. Nothing here was applied to your existing code.")

    if args.write:
        result = _fixes.apply_mechanical(mechanical, root)
        if not args.json:
            print()
            print(f"Applied to {len(result['changed_files'])} file(s): "
                  f"{', '.join(result['changed_files']) or 'none'}")
            for entry in result["skipped"]:
                print(f"  skipped {entry['file']}: {entry['reason']}")
            print()
            print("Only the mechanical fixes were applied. Run your tests.")
        return EXIT_OK

    if not args.json and (mechanical or generated):
        print("Nothing was changed. Add --write for the mechanical fixes, "
              "--write-generated for the generated files.")

    return EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    from .django_env import DjangoBootError, ensure_django
    from .project import project_root

    report = run_all(tenant_root=args.tenant_root, only=args.only, skip=args.skip)

    project = getattr(args, "_project_config", None) or _config.Config()
    # The root is only used to resolve --since and baseline paths. On a project
    # with no Django there is still a root, and refusing to report because the
    # settings module is missing would throw away the checks that did run.
    try:
        root = Path(ensure_django().project_path)
    except DjangoBootError:
        root = project_root()

    if project.ignore:
        kept = [
            f for f in report["findings"]
            if not (f.get("location") and project.is_ignored(f["location"].split(":")[0]))
        ]
        ignored_count = len(report["findings"]) - len(kept)
        report = dict(report, findings=kept, finding_count=len(kept))
    else:
        ignored_count = 0

    split = _config.apply_suppressions(
        [
            {**f, "file": (f["location"] or "").split(":")[0],
             "line": int(f["location"].split(":")[1])
             if f.get("location") and ":" in f["location"]
             and f["location"].split(":")[1].isdigit() else None}
            for f in report["findings"]
        ],
        root,
    )
    report = dict(
        report,
        findings=split["findings"],
        finding_count=len(split["findings"]),
        suppressed=split["suppressed"],
        refused_suppressions=split["refused_suppressions"],
    )
    counts: dict[str, int] = {}
    for finding in report["findings"]:
        counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1
    report["by_severity"] = counts

    if args.sarif:
        Path(args.sarif).write_text(_sarif.dumps(report), encoding="utf-8")

    _emit(report, args.json)

    if not args.json:
        counts = report["by_severity"]
        order = ["critical", "high", "medium", "low"]
        line = ", ".join(f"{counts[s]} {s}" for s in order if counts.get(s))
        print(f"{report['finding_count']} finding(s): {line or 'none'}")
        print()

        current = None
        for finding in report["findings"]:
            if finding["severity"] != current:
                current = finding["severity"]
                print(f"{current.upper()}")
                print("-" * len(current))
            location = finding["location"] or "-"
            print(f"  [{finding['check']}] {finding['title']}")
            print(f"      {location}")
            if finding.get("detail"):
                print(f"      {finding['detail']}")
            if finding.get("fix"):
                print(f"      fix: {finding['fix']}")
            print()

        if report.get("suppressed"):
            print(f"{len(report['suppressed'])} finding(s) suppressed in the source:")
            for entry in report["suppressed"][:10]:
                print(f"  {entry.get('location') or '-'}  {entry['suppressed_because']}")
            print()
        if report.get("refused_suppressions"):
            print("Suppressions that did NOT take effect:")
            for entry in report["refused_suppressions"]:
                print(f"  {entry.get('location') or '-'}  {entry['suppression_problem']}")
            print()
        if ignored_count:
            print(f"{ignored_count} finding(s) hidden by the ignore patterns in "
                  f"{project.source}")
            print()

        ran = report["checks_run"]
        ok = [n for n, st in ran.items() if st["ok"]]
        print(f"Ran {len(ok)} check(s): {', '.join(ok)}")
        if project.source:
            print(f"Settings from {project.source}")
        if report["checks_failed"]:
            print()
            print("These checks could NOT run, so their area is unverified:")
            for name in report["checks_failed"]:
                print(f"  {name}: {ran[name]['error']}")

        if args.sarif:
            print()
            print(f"SARIF written to {args.sarif}")

    skipped = report.get("checks_not_applicable") or {}
    if skipped and not args.json:
        print()
        frameworks = ", ".join(report.get("frameworks") or {}) or "none detected"
        print(f"Frameworks found: {frameworks}")
        print(f"{len(skipped)} check(s) do not apply to this project:")
        # Eighteen checks skipped for one missing environment variable printed
        # the same 300-character reason eighteen times, which buries the one
        # sentence that tells somebody what to do about it.
        by_reason: dict[str, list[str]] = {}
        for name, reason in sorted(skipped.items()):
            by_reason.setdefault(reason, []).append(name)
        for reason, names in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
            print(f"    {', '.join(names)}")
            for line in textwrap.wrap(reason, width=76):
                print(f"      {line}")

    # A check that could not run is not a pass.
    if report["checks_failed"] and args.strict:
        return EXIT_ERROR
    if gate(report, args.fail_on):
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_serializer_nplusone(args: argparse.Namespace) -> int:
    report = serializer_nplusone(max_depth=args.max_depth)
    _emit(report, args.json)

    if not args.json:
        if not report["rest_framework_installed"]:
            print("djangorestframework is not importable here, nothing to inspect.")
            return EXIT_OK
        print(f"{report['serializer_count']} serializer(s), "
              f"{report['finding_count']} finding(s), "
              f"{report['high_severity_count']} high")
        print()
        for finding in report["findings"]:
            print(f"  {finding['severity']:<7} {finding['serializer']}.{finding['field']}")
            print(f"          {finding.get('location') or '-'}  {finding['why']}")
            if finding.get("suggested"):
                print(f"          {finding['suggested']}")
            print()
        if report["queryset_advice"]:
            print("Queryset changes, per serializer:")
            for name, advice in sorted(report["queryset_advice"].items()):
                parts = []
                if advice["select_related"]:
                    parts.append("select_related(" + ", ".join(repr(x) for x in advice["select_related"]) + ")")
                if advice["prefetch_related"]:
                    parts.append("prefetch_related(" + ", ".join(repr(x) for x in advice["prefetch_related"]) + ")")
                print(f"  {name}")
                print(f"      .{'.'.join(parts)}")
        for entry in report["unreadable_serializers"]:
            print(f"  (unreadable) {entry['serializer']}: {entry['unreadable']}")

    if args.max_high is not None and report["high_severity_count"] > args.max_high:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_explain(args: argparse.Namespace) -> int:
    report = explain_model(
        args.model, tenant_root=args.tenant_root, include_raw=args.raw
    )
    _emit(report, args.json)

    if not args.json:
        print(report["model"])
        print("=" * len(report["model"]))
        print(report["summary"])
        print()

        structure = report["structure"]
        print(f"Table {structure['db_table']}, {structure['field_count']} field(s)")
        for relation in structure["relations"]:
            arrow = "->" if relation["direction"] == "forward" else "<-"
            suffix = f"  {relation['on_delete']}" if relation.get("on_delete") else ""
            print(f"    {relation['field']:<18} {relation['kind']:<11} {arrow} "
                  f"{relation['to']}{suffix}")
        print()

        ownership = report["ownership"]
        if ownership.get("path"):
            print(f"Owned via '{ownership['path']}' ({ownership['depth']} hop(s))")
        else:
            print("Not owned by the tenant root")
        print()

        delete = report["on_delete"]
        print(f"On delete: {delete['summary']}")
        for row in delete["cascades"]:
            print(f"    cascades into {row['from_model']} via {row['via_field']}")
        for row in delete["blocked_by"]:
            print(f"    blocked by {row['from_model']}.{row['via_field']} ({row['on_delete']})")
        if delete["signal_receivers"]:
            print(f"    plus {delete['signal_receivers']} signal receiver(s)")
        print()

        save = report["on_save"]
        if save["receiver_count"]:
            print(f"On save: {save['receiver_count']} receiver(s), "
                  f"writes {', '.join(save['models_written']) or 'nothing'}")
            for effect in save["side_effects"]:
                print(f"    {effect['kind']}: {effect['call']}()")
            print()

        if report["api_exposure"]:
            print("API exposure:")
            for finding in report["api_exposure"]:
                print(f"    {finding['serializer']}  mode {finding['mode']}")
                for entry in finding.get("sensitive", []):
                    print(f"      ! {entry['field']}  ({entry['category']})")
            print()

        if report["performance"]["unindexed_fields"]:
            print("Filtered or sorted without an index:")
            for finding in report["performance"]["unindexed_fields"]:
                print(f"    {finding['field']}  ({finding['occurrences']}x, "
                      f"{', '.join(finding['methods'])})")
            print()

        if report["datetime_fields"]:
            print("Datetime fields:")
            for finding in report["datetime_fields"]:
                print(f"    {finding['field']}: {finding['detail']}")
            print()

        risks = report["correlated_risks"]
        if risks:
            print("CORRELATED RISKS")
            print("-" * 60)
            print("Only visible by combining analyses. No single check sees these.")
            print()
            for risk in risks:
                print(f"  [{risk['severity'].upper()}] {risk['title']}")
                print(f"      {risk['detail']}")
                print(f"      seen by: {', '.join(risk['seen_by'])}")
                print()

    if args.fail_on_critical and any(
        r["severity"] == "critical" for r in report["correlated_risks"]
    ):
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_serializers(args: argparse.Namespace) -> int:
    report = serializer_exposure(include_safe=args.include_safe)
    _emit(report, args.json)

    if not args.json:
        if not report["rest_framework_installed"]:
            print("djangorestframework is not importable here, nothing to inspect.")
            print(report["note"])
            return EXIT_OK

        print(f"{report['serializer_count']} ModelSerializer subclass(es), "
              f"{report['finding_count']} finding(s), "
              f"{report['high_severity_count']} high")
        print()
        for finding in report["findings"]:
            print(f"  {finding['severity']:<7} {finding['serializer']}")
            print(f"          {finding['model']}, mode {finding['mode']}, "
                  f"{finding['exposed_count']} field(s)")
            for entry in finding["sensitive"]:
                print(f"            ! {entry['field']}  ({entry['category']})")
            print(f"          {finding['why']}")
            print(f"          {finding['suggested']}")
            print()
        for entry in report.get("explicit_and_clean", []):
            print(f"  ok      {entry['serializer']}  ({entry['exposed_count']} field(s))")

    if args.fail_on_findings and report["finding_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_datetimes(args: argparse.Namespace) -> int:
    report = datetime_audit(search_path=args.search_path)
    _emit(report, args.json)

    if not args.json:
        print(f"USE_TZ = {report['use_tz']}, files scanned: {report['files_scanned']}")
        if not report["use_tz"]:
            print("USE_TZ is off, so the code findings are informational.")
        print()
        if report["model_findings"]:
            print("Model fields:")
            for finding in report["model_findings"]:
                print(f"  {finding['severity']:<7} {finding['model']}.{finding['field']}")
                print(f"          {finding['detail']}")
                print(f"          use: {finding['suggested']}")
            print()
        if report["code_findings"]:
            print("Code:")
            for finding in report["code_findings"]:
                print(f"  {finding['severity']:<7} {finding['file']}:{finding['line']}  "
                      f"{finding['call']}")
                print(f"          {finding['code']}")
                print(f"          use: {finding['suggested']}")
            print()
        print(f"{report['total']} finding(s), {report['high_severity_count']} high")

    if args.fail_on_findings and report["total"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_indexes(args: argparse.Namespace) -> int:
    report = missing_indexes(
        search_path=args.search_path,
        min_occurrences=args.min_occurrences,
    )
    _emit(report, args.json)

    if not args.json:
        print(f"Files scanned: {report['files_scanned']}, "
              f"candidates: {report['candidate_count']} "
              f"({report['high_severity_count']} high)")
        if report["ignored_lookups"]:
            ignored = ", ".join(f"{k}={v}" for k, v in sorted(report["ignored_lookups"].items()))
            print(f"Ignored, no btree index would help: {ignored}")
        print()
        for finding in report["findings"]:
            print(f"  {finding['severity']:<7} {finding['model']}.{finding['field']}  "
                  f"({finding['field_type']}, {finding['occurrences']}x, "
                  f"{', '.join(finding['methods'])})")
            for use in finding["used_at"][:4]:
                print(f"          {use['file']}:{use['line']}  {use['code']}")
            print(f"          {finding['suggested_model_change']}")
            print()

    if getattr(args, "since", None):
        base = Path(report["search_path"])
        kept = []
        for finding in report["findings"]:
            uses, _ = _restrict_to_changes(
                argparse.Namespace(since=args.since), finding["used_at"], base
            )
            if uses:
                kept.append(dict(finding, used_at=uses, occurrences=len(uses)))
        report = dict(report, findings=kept, candidate_count=len(kept),
                      high_severity_count=sum(1 for f in kept if f["severity"] == "high"))
        print(f"{len(kept)} candidate(s) touched by changed files")

    if args.max_candidates is not None and report["candidate_count"] > args.max_candidates:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_signals(args: argparse.Namespace) -> int:
    report = what_happens_on(args.model, event=args.event, max_depth=args.max_depth)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['model']}.{report['event']}() triggers "
              f"{report['receiver_count']} receiver(s)")
        if report["models_written"]:
            print(f"Models written along the way: {', '.join(report['models_written'])}")
        print()
        for step in report["chain"]:
            indent = "  " * (step.get("depth", 0) + 1)
            if "receiver" not in step:
                print(f"{indent}(cycle) {step['model']}: {step['note']}")
                continue
            print(f"{indent}{step['signal']:<12} {step['receiver']}  on {step['on_model']}")
            for write in step.get("writes", []):
                target = write["resolved_model"] or "unresolved"
                print(f"{indent}    writes {write['target']}.{write['method']}() -> {target}")
            for effect in step.get("side_effects", []):
                print(f"{indent}    {effect['kind']}: {effect['call']}()  ({effect['why']})")
        if report["side_effects"]:
            print()
            print("Side effects in total:")
            for effect in report["side_effects"]:
                print(f"  {effect['kind']:<22} {effect['call']}()  via {effect['receiver']}")
        if report["unreadable_receivers"]:
            print()
            print("Receivers whose source could not be read:")
            for name in report["unreadable_receivers"]:
                print(f"  {name}")
    return EXIT_OK


def _cmd_tenancy(args: argparse.Namespace) -> int:
    report = find_unscoped_queries(
        tenant_root=args.tenant_root,
        search_path=args.search_path,
        max_depth=args.max_depth,
        include_exempt=args.include_exempt,
    )
    _emit(report, args.json)

    if not args.json:
        print(f"Tenant root: {report['tenant_root']}")
        print(f"Tenant-scoped models: {len(report['tenant_scoped_models'])}")
        for label, info in report["tenant_scoped_models"].items():
            print(f"    {label:<26} owner path: {info['path']}  ({info['depth']} hop(s))")
        print()
        print(f"Files scanned: {report['files_scanned']}, "
              f"queryset chains: {report['queryset_chains_seen']}")
        print()
        for finding in report["findings"]:
            print(f"  {finding['severity']:<7} {finding['file']}:{finding['line']}")
            print(f"          {finding['code']}")
            print(f"          {finding['model']} is owned via "
                  f"'{finding['owner_path']}', filtered on {finding['filter_keys'] or 'nothing'}")
            print(f"          add: {finding['suggested']}")
            print()
        print(f"{report['unscoped_count']} candidate(s), "
              f"{report['high_severity_count']} at one hop from the owner")

        # Shown rather than silently dropped. A reviewer needs to see what was
        # ruled out and on what grounds, because the grounds can be wrong.
        if report["scoped_elsewhere_count"]:
            print()
            print(f"{report['scoped_elsewhere_count']} query(s) ruled out because "
                  "they are scoped somewhere the line cannot show:")
            for entry in report["scoped_elsewhere"]:
                print(f"    {entry['file']}:{entry['line']}  {entry['model']}")
                print(f"        {entry['detail']}")

    findings, narrowed = _restrict_to_changes(
        args, report["findings"], Path(report["search_path"])
    )
    if narrowed:
        report = dict(report, findings=findings, unscoped_count=len(findings),
                      high_severity_count=sum(1 for f in findings if f["severity"] == "high"))
        print(f"{report['unscoped_count']} candidate(s) in changed files")

    decided = _apply_baseline("tenancy", report, args)
    if decided is not None:
        return decided

    if args.fail_on_findings and report["unscoped_count"]:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_deploy_safety(args: argparse.Namespace) -> int:
    report = deploy_safety(
        search_path=args.search_path,
        include_third_party=args.include_third_party,
    )
    _emit(report, args.json)

    if not args.json:
        print(f"Scanned {report['search_path']}")
        print(f"Project apps: {', '.join(report['project_apps']) or '(none)'}")
        if report["skipped_third_party_apps"]:
            print(f"Skipped third-party: {', '.join(report['skipped_third_party_apps'])}")
        print()
        for entry in report["clear"]:
            print(f"  CLEAR     {entry['app']}.{entry['migration']}  "
                  f"{entry['operation']} {entry['symbol']!r}")
        for entry in report["blocking"]:
            print(f"  BLOCKING  {entry['app']}.{entry['migration']}  "
                  f"{entry['operation']} {entry['symbol']!r}  "
                  f"({entry['reference_count']} references, confidence {entry['confidence']})")
            for ref in entry["references"]:
                print(f"              {ref['path']}:{ref['line']}  [{ref['kind']}]")
        print()
        print(f"{report['blocking_count']} blocking, {report['clear_count']} clear")

    if getattr(args, "since", None):
        entries = [
            dict(entry, file=f"{entry['app']}/migrations/{entry['migration']}.py")
            for entry in report["blocking"]
        ]
        kept, _ = _restrict_to_changes(args, entries, Path(report["search_path"]))
        report = dict(report, blocking=kept, blocking_count=len(kept))
        print(f"{len(kept)} blocking migration(s) added by this branch")

    decided = _apply_baseline("deploy-safety", report, args)
    if decided is not None:
        return decided

    return EXIT_FINDINGS if report["blocking_count"] else EXIT_OK


def _cmd_nplusone(args: argparse.Namespace) -> int:
    context = {}
    for pair in args.context or []:
        if "=" not in pair:
            print(f"--context expects name=app.Model, got {pair!r}", file=sys.stderr)
            return EXIT_ERROR
        name, label = pair.split("=", 1)
        context[name.strip()] = label.strip()

    report = scan_templates(
        template_root=args.templates,
        project_root=args.project_path,
        root_models=context or None,
    )
    _emit(report, args.json)

    if not args.json:
        print(f"Templates found: {report['templates_found']}, "
              f"analysed: {report['templates_analysed']}, "
              f"with findings: {report['templates_with_findings']}")
        if report["templates_skipped_no_context"]:
            print(f"Skipped, no resolvable context: {len(report['templates_skipped_no_context'])}")
        print()
        for result in report["results"]:
            print(f"  {result['template']}  ({result['high_severity_count']} high)")
            for finding in result["findings"]:
                if finding["severity"] == "high":
                    print(f"      {finding['expression']:<38} {finding['suggested']}")
            suggestion = result["suggested_queryset"]
            parts = []
            if suggestion["select_related"]:
                parts.append("select_related(" + ", ".join(repr(s) for s in suggestion["select_related"]) + ")")
            if suggestion["prefetch_related"]:
                parts.append("prefetch_related(" + ", ".join(repr(s) for s in suggestion["prefetch_related"]) + ")")
            if parts:
                print(f"      -> .{'.'.join(parts)}")
            print()
        print(f"{report['high_severity_total']} high severity candidate(s)")

    results, narrowed = _restrict_to_changes(
        args, report["results"], Path(report["template_root"]), key="template"
    )
    if narrowed:
        report = dict(
            report,
            results=results,
            templates_with_findings=len(results),
            high_severity_total=sum(r["high_severity_count"] for r in results),
        )
        print(f"{report['high_severity_total']} high severity candidate(s) in changed templates")

    decided = _apply_baseline("n+1", report, args)
    if decided is not None:
        return decided

    if args.max_high is not None and report["high_severity_total"] > args.max_high:
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_migrations(args: argparse.Namespace) -> int:
    report = migration_risk(include_applied=args.include_applied)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['migration_count']} migration(s), {report['risky_count']} risky")
        print()
        for entry in report["migrations"]:
            if entry["worst_risk"] == "safe":
                continue
            print(f"  {entry['worst_risk']:<22} {entry['app']}.{entry['name']}")
            for operation in entry["operations"]:
                if operation["risk"] != "safe":
                    print(f"      {operation['operation']:<16} {operation['detail']}")
                    if operation.get("safer"):
                        print(f"          safer: {operation['safer']}")

    blocking = {"blocks_writes", "breaks_running_code", "rewrites_table"}
    if args.fail_on_risk and any(e["worst_risk"] in blocking for e in report["migrations"]):
        return EXIT_FINDINGS
    return EXIT_OK


def _cmd_delete_impact(args: argparse.Namespace) -> int:
    report = delete_impact(args.model, max_depth=args.max_depth)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['model']}: {report['summary']}")
        print()
        for row in report["cascades"]:
            print(f"  CASCADE  {row['from_model']:<24} via {row['via_field']:<14} "
                  f"depth {row['depth']}   [{row['path']}]")
        for row in report["blocked_by"]:
            print(f"  BLOCKS   {row['from_model']:<24} via {row['via_field']:<14} {row['on_delete']}")
        for row in report["fields_cleared"]:
            print(f"  NULLED   {row['from_model']:<24} via {row['via_field']:<14} {row['on_delete']}")
    return EXIT_OK


def _cmd_models(args: argparse.Namespace) -> int:
    report = list_models(app_label=args.app, include_fields=not args.short)
    _emit(report, args.json)

    if not args.json:
        print(f"{report['model_count']} model(s) in {report['settings_module']}")
        for model in report["models"]:
            print(f"  {model['label']:<32} {model['db_table']}")
            for field in model.get("fields", []):
                relation = field.get("relation")
                if relation:
                    print(f"      {field['name']:<20} {relation['kind']:<12} "
                          f"{relation['direction']:<8} -> {relation['to']}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="django-chainsaw",
        description="Analyse a Django project: cascades, N+1 candidates, migration safety.",
    )
    parser.add_argument("--project-path", help=f"overrides ${PROJECT_PATH_VAR}")
    parser.add_argument("--settings", help=f"overrides ${SETTINGS_MODULE_VAR}")
    parser.add_argument("--json", action="store_true", help="machine readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("deploy-safety", help="are pending destructive migrations safe to ship?")
    p.add_argument("--search-path", help="directory to scan for references")
    p.add_argument("--include-third-party", action="store_true")
    p.set_defaults(func=_cmd_deploy_safety)

    p = sub.add_parser("n+1", help="scan templates for relation traversals")
    p.add_argument("--templates", help="template directory")
    p.add_argument("--context", action="append", metavar="name=app.Model",
                   help="context variable applied to every template, repeatable")
    p.add_argument("--max-high", type=int, metavar="N",
                   help="exit 1 if more than N high severity candidates are found")
    p.set_defaults(func=_cmd_nplusone)

    p = sub.add_parser("cost", help="estimated queries per request, per endpoint")
    p.add_argument("--page-size", type=int, default=50)
    p.add_argument("--fan-out", type=int, default=5,
                   help="assumed child objects per parent for nested levels")
    p.add_argument("--list-only", action="store_true", help="skip detail views")
    p.add_argument("--max-queries", type=int, metavar="N",
                   help="exit 1 if the worst endpoint is above N")
    p.set_defaults(func=_cmd_cost)

    p = sub.add_parser("contract", help="what this branch changes about the API")
    p.add_argument("--snapshot", default=CONTRACT_FILE,
                   help="the committed contract to compare against")
    p.add_argument("--update", action="store_true",
                   help="overwrite the snapshot with the current shape")
    p.add_argument("--max-depth", type=int, default=3,
                   help="how far to expand nested serializers")
    p.add_argument("--fail-on-breaking", action="store_true",
                   help="exit 1 if any change breaks an existing client")
    p.set_defaults(func=_cmd_contract)

    p = sub.add_parser("on-commit",
                       help="side effects inside a transaction that cannot be rolled back")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--include-low-confidence", action="store_true",
                   help="also report calls guessed from the name, such as .send()")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 if any high-severity call escapes a transaction")
    p.set_defaults(func=_cmd_oncommit)

    p = sub.add_parser("bypass", help="bulk writes that skip a model's save() chain")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--model", metavar="app_label.ModelName")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 if any bulk write skips effects that write or send")
    p.set_defaults(func=_cmd_bypass)

    p = sub.add_parser("races", help="read-modify-save races, locks outside a transaction, upserts with no unique constraint")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--no-parameters", action="store_true",
                   help="only report instances fetched in the same function")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any high-confidence race or unprotected lock")
    p.set_defaults(func=_cmd_races)

    p = sub.add_parser("open", help="open endpoints crossed with what their serializer exposes")
    p.add_argument("--sensitive-only", action="store_true",
                   help="skip open endpoints whose only problem is __all__ or exclude")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 if any open endpoint exposes a sensitive field")
    p.set_defaults(func=_cmd_open)

    p = sub.add_parser("overfetch",
                       help="select_related/prefetch_related the serializer never reads")
    p.add_argument("--include-low-confidence", action="store_true",
                   help="also report views with a method field or an overridden list/retrieve")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any high-confidence unused eager load")
    p.set_defaults(func=_cmd_overfetch)

    p = sub.add_parser("money", help="decimal amounts that stop being exact")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--include-low", action="store_true",
                   help="also show floats that happen to be exactly representable")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any high-severity precision loss")
    p.set_defaults(func=_cmd_money)

    p = sub.add_parser("profile", help="what this project is built on")
    p.add_argument("--search-path", metavar="DIR")
    p.set_defaults(func=_cmd_profile)

    p = sub.add_parser("async", help="blocking calls that run on the event loop")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--no-follow", action="store_true",
                   help="only report blocking written directly in an async function")
    p.add_argument("--max-depth", type=int, default=3,
                   help="how many calls deep to follow")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 if anything blocking runs on the loop")
    p.set_defaults(func=_cmd_async)

    p = sub.add_parser("routes",
                       help="FastAPI endpoints that serialise more than they declare")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any unauthenticated endpoint with an unbounded response")
    p.set_defaults(func=_cmd_routes)

    p = sub.add_parser("sqla",
                       help="SQLAlchemy relationships loaded one row at a time")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any lazy relationship crossed per row")
    p.set_defaults(func=_cmd_sqla)

    p = sub.add_parser("amplification",
                       help="endpoints anyone can call that cost a great deal")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any critical amplification surface")
    p.set_defaults(func=_cmd_amplification)

    p = sub.add_parser("celery",
                       help="model instances handed to tasks, and wrong argument counts")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any task given the wrong thing")
    p.set_defaults(func=_cmd_celery)

    p = sub.add_parser("dangling",
                       help="URL names and template names nothing will resolve")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--skip-templates", action="store_true",
                   help="do not read the templates themselves")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any name that will not resolve")
    p.set_defaults(func=_cmd_dangling)

    p = sub.add_parser("aggregates",
                       help="aggregates multiplied by a join across two relations")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any multiplied aggregate")
    p.set_defaults(func=_cmd_aggregates)

    p = sub.add_parser("choices",
                       help="literals a field's choices will never match")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--skip-tests", action="store_true",
                   help="do not scan test files")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any literal that cannot match")
    p.set_defaults(func=_cmd_choices)

    p = sub.add_parser("impact",
                       help="findings grouped by the entry points that reach them")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--tenant-root", default="auth.User", metavar="app.Model")
    p.add_argument("--max-depth", type=int, default=8, metavar="N",
                   help="how many callers to walk back through (default: 8)")
    p.add_argument("--top", type=int, default=20, metavar="N",
                   help="how many entry points to print (default: 20)")
    p.add_argument("--per-entry", type=int, default=5, metavar="N",
                   help="how many findings to print per entry point (default: 5)")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 if any entry point carries a high or critical finding")
    p.set_defaults(func=_cmd_impact)

    p = sub.add_parser("loops", help="database work written inside a loop")
    p.add_argument("--search-path", metavar="DIR")
    p.add_argument("--no-writes", action="store_true",
                   help="only report reads")
    p.add_argument("--no-follow-calls", action="store_true",
                   help="only read the loop body, do not follow calls out of it")
    p.add_argument("--max-depth", type=int, default=3, metavar="N",
                   help="how many calls to follow out of a loop (default: 3)")
    p.add_argument("--fail-on-findings", action="store_true",
                   help="exit 1 on any query that runs once per row")
    p.set_defaults(func=_cmd_loops)

    p = sub.add_parser("fix", help="turn findings into code, and say which are safe")
    p.add_argument("--tenant-root", default="auth.User", metavar="app.Model")
    p.add_argument("--skip", action="append", metavar="CHECK",
                   help="skip a check, repeatable")
    p.add_argument("--write", action="store_true",
                   help="apply the mechanical fixes to your files")
    p.add_argument("--write-generated", action="store_true",
                   help="write the generated files, such as index migrations")
    p.set_defaults(func=_cmd_fix)

    p = sub.add_parser("check", help="run every analysis and return one answer")
    p.add_argument("--tenant-root", default="auth.User", metavar="app.Model")
    p.add_argument("--only", action="append", choices=list(ALL_CHECKS), metavar="CHECK",
                   help=f"run just this check, repeatable. One of: {', '.join(ALL_CHECKS)}")
    p.add_argument("--skip", action="append", choices=list(ALL_CHECKS), metavar="CHECK",
                   help="run everything except this check, repeatable")
    p.add_argument("--fail-on", default=GATE_DEFAULT,
                   choices=["critical", "high", "medium", "low"],
                   help=f"exit 1 at or above this severity (default: {GATE_DEFAULT})")
    p.add_argument("--strict", action="store_true",
                   help="exit 2 if any check failed to run, instead of reporting the rest")
    p.add_argument("--sarif", metavar="FILE",
                   help="also write SARIF, so a code-scanning UI can annotate the diff")
    p.set_defaults(func=_cmd_check)

    p = sub.add_parser("n+1-serializer", help="N+1 in DRF serializers")
    p.add_argument("--max-depth", type=int, default=3, help="how far to follow nested serializers")
    p.add_argument("--max-high", type=int, metavar="N", help="exit 1 above N high severity findings")
    p.set_defaults(func=_cmd_serializer_nplusone)

    p = sub.add_parser("explain", help="everything about one model, plus correlated risks")
    p.add_argument("model", help="app_label.ModelName")
    p.add_argument("--tenant-root", default="auth.User", metavar="app.Model")
    p.add_argument("--raw", action="store_true", help="attach every analyser's full report")
    p.add_argument("--fail-on-critical", action="store_true",
                   help="exit 1 if a correlated risk is critical")
    p.set_defaults(func=_cmd_explain)

    p = sub.add_parser("serializers", help="what DRF serializers expose")
    p.add_argument("--include-safe", action="store_true",
                   help="also list serializers with an explicit, clean field list")
    p.add_argument("--fail-on-findings", action="store_true", help="exit 1 on any finding")
    p.set_defaults(func=_cmd_serializers)

    p = sub.add_parser("datetimes", help="naive datetimes and ambiguous field defaults")
    p.add_argument("--search-path", help="directory to scan")
    p.add_argument("--fail-on-findings", action="store_true", help="exit 1 on any finding")
    p.set_defaults(func=_cmd_datetimes)

    p = sub.add_parser("indexes", help="fields filtered or sorted on without an index")
    p.add_argument("--search-path", help="directory to scan")
    p.add_argument("--min-occurrences", type=int, default=1,
                   help="only report a field asked for at least this often")
    p.add_argument("--max-candidates", type=int, metavar="N",
                   help="exit 1 if more than N candidates are found")
    p.set_defaults(func=_cmd_indexes)

    p = sub.add_parser("signals", help="what a save or delete actually triggers")
    p.add_argument("model", help="app_label.ModelName")
    p.add_argument("--event", choices=["save", "delete"], default="save")
    p.add_argument("--max-depth", type=int, default=4)
    p.set_defaults(func=_cmd_signals)

    p = sub.add_parser("tenancy", help="querysets on owned data with no ownership filter")
    p.add_argument("--tenant-root", default="auth.User", metavar="app.Model",
                   help="the model that owns data (default: auth.User)")
    p.add_argument("--search-path", help="directory to scan")
    p.add_argument("--max-depth", type=int, default=4,
                   help="how many relation hops still count as owned")
    p.add_argument("--include-exempt", action="store_true",
                   help="also scan admin, management commands and tests")
    p.add_argument("--fail-on-findings", action="store_true", help="exit 1 on any candidate")
    p.set_defaults(func=_cmd_tenancy)

    p = sub.add_parser("migrations", help="rate migrations by production impact")
    p.add_argument("--include-applied", action="store_true")
    p.add_argument("--fail-on-risk", action="store_true", help="exit 1 if any migration is risky")
    p.set_defaults(func=_cmd_migrations)

    p = sub.add_parser("delete-impact", help="what a delete takes with it")
    p.add_argument("model", help="app_label.ModelName")
    p.add_argument("--max-depth", type=int, default=6)
    p.set_defaults(func=_cmd_delete_impact)

    p = sub.add_parser("models", help="list models and relations")
    p.add_argument("--app", help="restrict to one app label")
    p.add_argument("--short", action="store_true", help="omit field details")
    p.set_defaults(func=_cmd_models)

    for name in ("deploy-safety", "n+1", "tenancy", "indexes"):
        sub.choices[name].add_argument(
            "--since", metavar="REF",
            help="only report findings in files changed since REF, compared at "
                 "the merge base so the branch is not blamed for other people's work")

    for name in ("deploy-safety", "n+1", "tenancy"):
        target = sub.choices[name]
        target.add_argument(
            "--baseline", nargs="?", const=_baseline.DEFAULT_BASELINE, metavar="FILE",
            help="compare against a recorded baseline and fail on new findings only "
                 f"(default file: {_baseline.DEFAULT_BASELINE})")
        target.add_argument(
            "--update-baseline", action="store_true",
            help="record today's findings as the baseline and exit 0")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    from .project import NoProjectError

    try:
        _bootstrap(args)
    except (DjangoBootError, NoProjectError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
