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
from typing import Any

from .cascade import delete_impact
from .deploy_safety import deploy_safety
from .django_env import PROJECT_PATH_VAR, SETTINGS_MODULE_VAR, BootConfig, DjangoBootError, ensure_django
from .introspect import list_models
from .migrations import migration_risk
from .scan import scan_templates
from .tenancy import find_unscoped_queries

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _bootstrap(args: argparse.Namespace) -> None:
    if args.project_path:
        os.environ[PROJECT_PATH_VAR] = args.project_path
    if args.settings:
        os.environ[SETTINGS_MODULE_VAR] = args.settings
    ensure_django(BootConfig.from_env())


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        _bootstrap(args)
    except DjangoBootError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        return args.func(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
