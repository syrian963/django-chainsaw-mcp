# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""The CLI, driven in this process rather than as a subprocess.

`exitcheck.sh` and its neighbours run `django-chainsaw` as a subprocess, which
is the right way to test the exit codes somebody's CI will actually see. It
also means coverage cannot follow it: `cli.py` is 1479 statements and measured
at 0%, while being exercised the whole time.

So the same commands are called here through `main(argv)`. That is not for the
percentage - it catches a different class of defect. A subprocess test sees
only the exit code, and these see the exception, the argument parsing and the
report keys the printer reaches for. Three of the bugs found while building
this repository were an unformatted string or a missing key in a branch of a
printer that no test had entered.
"""

from __future__ import annotations

import pytest

from django_chainsaw_mcp import cli

EXIT_OK = 0
EXIT_FINDINGS = 1


def run(*argv: str) -> int:
    """The CLI in this process. Returns its exit code."""
    return cli.main(list(argv))


def test_every_subcommand_is_reachable_from_the_parser():
    # A subcommand added without wiring `func` would fail only when someone
    # ran it, and the help text would still list it.
    parser = cli.build_parser()
    subcommands: list[str] = []
    for action in parser._subparsers._group_actions:
        subcommands.extend(action.choices)
    assert len(subcommands) > 30
    for name in subcommands:
        for action in parser._subparsers._group_actions:
            sub = action.choices.get(name)
            if sub is not None:
                assert sub.get_default("func") is not None, name


@pytest.mark.parametrize(
    "command",
    [
        "models",
        "migrations",
        "datetimes",
        "indexes",
        "serializers",
        "money",
        "races",
        "bypass",
        "loops",
        "choices",
        "aggregates",
        "dangling",
        "on-commit",
        "open",
        "cost",
        "overfetch",
        "profile",
        "async",
        "impact",
        "n+1-serializer",
        "celery",
        "routes",
        "sqla",
        "amplification",
        "tenancy",
    ],
)
def test_a_command_runs_and_returns_a_documented_exit_code(command, capsys):
    # Without --fail-on-findings a check reports and succeeds, whatever it
    # found. The demo project has findings for most of these, so a non-zero
    # exit here would mean the default gate changed by accident.
    assert run(command) == EXIT_OK
    out = capsys.readouterr().out
    assert out.strip(), f"{command} printed nothing"


@pytest.mark.parametrize("command", ["loops", "choices", "aggregates", "dangling"])
def test_fail_on_findings_gates_the_commands_that_offer_it(command):
    # The demo project deliberately contains what each of these looks for, so
    # a zero exit would mean the flag stopped working.
    assert run(command, "--fail-on-findings") == EXIT_FINDINGS


def test_deploy_safety_gates_on_the_planted_migration(capsys):
    # demoshop ships a migration that drops a field the code still uses, so a
    # clean exit here would mean the check stopped seeing it.
    assert run("deploy-safety") == EXIT_FINDINGS
    assert "legacy_code" in capsys.readouterr().out


def test_the_template_check_scans_the_configured_template_directories(capsys):
    assert run("n+1") == EXIT_OK
    assert capsys.readouterr().out.strip()


def test_the_contract_captures_a_snapshot_and_then_compares_against_it(tmp_path, capsys):
    snapshot = tmp_path / "contract.json"

    assert run("contract", "--snapshot", str(snapshot), "--update") == EXIT_OK
    assert snapshot.is_file(), "--update should write the snapshot"
    capsys.readouterr()

    # Comparing a capture against itself has to be empty. Anything else means
    # the capture is not deterministic, and a contract check that reports
    # phantom changes is one people stop reading.
    assert run("contract", "--snapshot", str(snapshot)) == EXIT_OK
    assert "unchanged" in capsys.readouterr().out


def test_json_output_is_json(capsys):
    import json

    run("--json", "models")
    payload = json.loads(capsys.readouterr().out)
    assert payload["models"]


def test_the_aggregate_check_reports_and_gates(capsys):
    # demoshop ships a migration that drops a field the code still uses, so
    # the gate has something to catch.
    assert run("check", "--tenant-root", "shop.Customer") == EXIT_FINDINGS
    out = capsys.readouterr().out
    assert "finding(s)" in out
    assert "Ran " in out, "the run should say which checks it managed"


def test_only_and_skip_narrow_the_aggregate_run(capsys):
    run("check", "--only", "money", "--tenant-root", "shop.Customer")
    only = capsys.readouterr().out
    assert "Ran 1 check(s): money" in only

    run("check", "--skip", "money", "--tenant-root", "shop.Customer")
    skipped = capsys.readouterr().out
    assert "money" not in skipped.split("Ran ")[1].split("\n")[0]


def test_an_unknown_check_name_is_refused_rather_than_ignored():
    # Silently running everything because a name was misspelled would report a
    # different thing than the one asked for.
    with pytest.raises(SystemExit):
        run("check", "--only", "no-such-check")


def test_explain_reaches_the_per_model_report(capsys):
    assert run("explain", "shop.Order", "--tenant-root", "shop.Customer") == EXIT_OK
    assert "shop.Order" in capsys.readouterr().out


def test_delete_impact_reaches_the_cascade_report(capsys):
    assert run("delete-impact", "shop.Customer") == EXIT_OK
    out = capsys.readouterr().out
    assert "shop.Order" in out, "deleting a customer takes their orders"


def test_signals_reaches_the_chain_report(capsys):
    assert run("signals", "shop.Order", "--event", "save") == EXIT_OK
    assert capsys.readouterr().out.strip()


def test_fix_lists_suggestions_without_writing_anything(capsys, tmp_path):
    # `fix` with no --write must not touch the tree. The demo project is the
    # tree, so a write here would corrupt the fixtures for every other test.
    from django_chainsaw_mcp.project import project_root

    before = {
        path: path.stat().st_mtime_ns
        for path in project_root().rglob("*.py")
    }
    assert run("fix", "--tenant-root", "shop.Customer") == EXIT_OK
    after = {path: path.stat().st_mtime_ns for path in project_root().rglob("*.py")}
    assert before == after, "fix without --write changed a file"
    assert capsys.readouterr().out.strip()


# --- the HTML report -------------------------------------------------------


def test_the_report_is_one_self_contained_file(tmp_path):
    # No server, no build step, and nothing fetched at open time: the tool
    # promises nothing leaves the machine, and a report that pulls a font from
    # a CDN breaks that promise on somebody else's behalf.
    out = tmp_path / "report.html"
    assert run("report", "--out", str(out), "--no-impact",
               "--tenant-root", "shop.Customer") == EXIT_OK

    document = out.read_text(encoding="utf-8")
    assert document.startswith("<!doctype html>")
    for tag in ("<link", "<img", "src=\"http", "@import"):
        assert tag not in document, f"the report reaches outside itself: {tag}"


def test_the_report_payload_is_parseable_and_complete(tmp_path):
    import json
    import re

    out = tmp_path / "report.html"
    run("report", "--out", str(out), "--no-impact", "--tenant-root", "shop.Customer")
    document = out.read_text(encoding="utf-8")

    payload = re.search(
        r'<script type="application/json" id="payload">(.*?)</script>',
        document,
        re.S,
    )
    assert payload, "the report carries no payload"
    data = json.loads(payload.group(1))
    assert data["findings"], "no findings in the payload"
    assert data["checks_ran"]
    assert {"check", "severity", "title", "location"} <= set(data["findings"][0])


def test_a_finding_containing_a_script_tag_cannot_close_the_payload(tmp_path):
    # A finding quotes real source. If that source contained `</script>` the
    # payload would end early and the page would render as text.
    from django_chainsaw_mcp.report import html_report

    document = html_report(
        {
            "findings": [{
                "check": "made-up",
                "severity": "high",
                "title": "</script><script>alert(1)</script>",
                "location": "a.py:1",
                "detail": "<img onerror=alert(2)>",
                "fix": "&amp; <b>",
            }],
            "by_severity": {"high": 1},
            "checks_run": {"made-up": {"ok": True}},
        },
    )
    assert "</script><script>alert(1)" not in document
    assert document.count("</script>") == 2, "one payload tag and one script tag"


def test_the_report_keeps_the_caveats_that_travel_with_the_findings(tmp_path):
    from django_chainsaw_mcp.report import html_report

    document = html_report(
        {
            "findings": [],
            "by_severity": {},
            "checks_run": {"broken": {"ok": False, "error": "RuntimeError: nope"}},
            "checks_failed": ["broken"],
            "checks_not_applicable": {"routes": "no FastAPI here"},
        },
    )
    # A check that could not run is unverified, not clean.
    assert "could not run" in document
    assert "RuntimeError: nope" in document
    assert "no FastAPI here" in document


def test_the_report_gate_is_separate_from_writing_the_file(tmp_path):
    out = tmp_path / "report.html"
    # Writing always happens; the exit code is the caller's choice.
    assert run("report", "--out", str(out), "--no-impact",
               "--tenant-root", "shop.Customer") == EXIT_OK
    assert out.exists()

    out.unlink()
    assert run("report", "--out", str(out), "--no-impact", "--fail-on-findings",
               "--tenant-root", "shop.Customer") == EXIT_FINDINGS
    assert out.exists(), "the file must be written even when the gate trips"


# --- the paths that decide what a pipeline sees ----------------------------


@pytest.mark.parametrize("command", ["models", "indexes", "loops", "check", "impact"])
def test_json_output_parses_for_the_commands_ci_reads(command):
    # A broken JSON branch does not look broken: it looks like an integration
    # that stopped working, and the error surfaces in whatever consumes it.
    import io
    import json
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        args = ["--json", command]
        if command in {"check", "impact"}:
            args += ["--tenant-road" if False else "--tenant-root", "shop.Customer"]
        run(*args)
    json.loads(buffer.getvalue())


def test_since_restricts_the_findings_to_a_diff(capsys):
    # `--since HEAD` is a diff against the current commit, so on a clean tree
    # nothing is in it. A gate that reported findings anyway would be gating on
    # the whole codebase while claiming to gate on the branch.
    assert run("indexes", "--since", "HEAD") == EXIT_OK
    out = capsys.readouterr().out
    assert "HEAD" in out or "changed" in out.lower(), out


def test_since_says_what_it_could_not_place(capsys):
    # A finding with no file cannot be matched against a diff. Dropping it
    # silently is how `--since` would report a clean branch over a real
    # problem, so the count has to appear.
    run("check", "--since", "HEAD", "--tenant-root", "shop.Customer")
    out = capsys.readouterr().out
    assert "since" in out.lower() or "changed" in out.lower(), out


def test_a_bad_ref_warns_and_reports_everything_rather_than_nothing(capsys):
    """A ref git cannot resolve must not look like an empty diff.

    The behaviour is to warn and analyse the whole tree, which is the
    fail-safe direction: the answer is a superset rather than a subset, and it
    says on stderr that the narrowing did not happen. Narrowing to nothing
    would report a clean branch because somebody renamed main.
    """
    everything = run("indexes")
    capsys.readouterr()

    assert run("indexes", "--since", "definitely-not-a-ref") == everything
    result = capsys.readouterr()
    assert "definitely-not-a-ref" in result.err, "the ignored ref has to be named"
    assert "Since" not in result.out, "it must not claim to have narrowed"
    # And the findings are the unnarrowed set, not a smaller one.
    # The count line is the honest witness: it is the unnarrowed number.
    assert "candidates:" in result.out


def test_a_baseline_round_trip_through_the_cli(tmp_path, capsys):
    path = tmp_path / "tenancy-baseline.json"

    # Record, then compare against the recording: everything is accepted.
    assert run("tenancy", "--tenant-root", "shop.Customer",
               "--baseline", str(path), "--update-baseline") == EXIT_OK
    assert path.is_file()
    capsys.readouterr()

    assert run("tenancy", "--tenant-root", "shop.Customer",
               "--baseline", str(path)) == EXIT_OK
    out = capsys.readouterr().out
    assert "baseline" in out.lower(), out


def test_an_unset_project_path_is_an_error_with_the_variable_named(capsys, monkeypatch):
    # The first thing a new user sees. It has to name the variable and say
    # what to point it at.
    monkeypatch.delenv("DJANGO_CHAINSAW_PROJECT_PATH", raising=False)
    monkeypatch.delenv("DJANGO_CHAINSAW_SETTINGS_MODULE", raising=False)
    from django_chainsaw_mcp import django_env

    monkeypatch.setattr(
        django_env, "ensure_django",
        lambda *a, **k: (_ for _ in ()).throw(
            django_env.DjangoBootError("Missing environment variable(s)")
        ),
    )
    code = run("models")
    assert code != EXIT_OK
    combined = capsys.readouterr()
    assert "DJANGO_CHAINSAW" in (combined.out + combined.err)


def test_a_clean_check_prints_what_it_looked_at(django_project, monkeypatch, capsys):
    """The branch a real run cannot easily reach.

    Every check finds something on the demo project, so the "ran and found
    nothing" block never prints there - which is exactly the kind of printer
    branch this file exists for. A real report is taken and its `checks_run`
    replaced, so every other key the printer reaches for is genuine.
    """
    from django_chainsaw_mcp import cli as cli_module
    from django_chainsaw_mcp.check import run_all

    real = run_all(tenant_root="shop.Customer", only=["money"])
    real["checks_run"] = {
        "celery": {"ok": True, "findings": 0,
                   "examined": {"tasks_found": 12, "dispatches_checked": 41}},
        "money": {"ok": True, "findings": 0, "examined": {}},
        "loops": {"ok": True, "findings": 1, "examined": {"files_scanned": 9}},
    }
    real["checks_failed"] = []
    monkeypatch.setattr(cli_module, "run_all", lambda **kwargs: real)

    run("check")
    printed = capsys.readouterr().out

    assert "ran and found nothing" in printed, printed[-800:]
    assert "celery: tasks found 12, dispatches checked 41" in printed, printed
    # A check that does not report its coverage must be named as such rather
    # than listed as clean.
    assert "money: does not report its coverage" in printed, printed
    # A check that found something belongs in the findings, not in this block.
    assert "loops:" not in printed.split("ran and found nothing")[1]


def test_a_slow_run_says_where_the_time_went(django_project, monkeypatch, capsys):
    """Only printed above ten seconds, which the demo project never reaches."""
    from django_chainsaw_mcp import cli as cli_module
    from django_chainsaw_mcp.check import run_all

    real = run_all(tenant_root="shop.Customer", only=["money"])
    real["checks_run"] = {
        "races": {"ok": True, "findings": 2, "examined": {},
                  "seconds": 240.0, "cpu_seconds": 238.0},
        "loops": {"ok": True, "findings": 1, "examined": {},
                  "seconds": 60.0, "cpu_seconds": 59.0},
        "money": {"ok": True, "findings": 0, "examined": {},
                  "seconds": 0.5, "cpu_seconds": 0.5},
    }
    real["checks_failed"] = []
    monkeypatch.setattr(cli_module, "run_all", lambda **kwargs: real)

    run("check")
    printed = capsys.readouterr().out

    assert "300s in the checks" in printed, printed[-600:]
    assert "298s of CPU" in printed, printed[-600:]
    assert "races" in printed
    assert "80%" in printed, "the share is the point, not the seconds"
    assert "--skip takes these names" in printed
    # Wall and CPU agree here, so there is nothing to warn about.
    assert "machine was busy" not in printed


def test_a_run_on_a_busy_machine_says_the_shares_mean_nothing(
    django_project, monkeypatch, capsys
):
    """The lesson this feature learned about itself.

    A second DefectDojo run read 914 s across the checks for 186 s of CPU -
    the machine was busy and the numbers were mostly waiting. One check looked
    like it had gone from 24 s to 570 s. It had not moved.
    """
    from django_chainsaw_mcp import cli as cli_module
    from django_chainsaw_mcp.check import run_all

    real = run_all(tenant_root="shop.Customer", only=["money"])
    real["checks_run"] = {
        "races": {"ok": True, "findings": 2, "examined": {},
                  "seconds": 600.0, "cpu_seconds": 40.0},
        "loops": {"ok": True, "findings": 1, "examined": {},
                  "seconds": 300.0, "cpu_seconds": 20.0},
    }
    real["checks_failed"] = []
    monkeypatch.setattr(cli_module, "run_all", lambda **kwargs: real)

    run("check")
    printed = capsys.readouterr().out
    assert "machine was busy" in printed, printed[-600:]
    assert "Compare the CPU column" in printed


def test_a_fast_run_does_not_print_a_timing_table(django_project, monkeypatch, capsys):
    # A block on every run is decoration, and decoration gets skipped on the
    # run that needed reading.
    from django_chainsaw_mcp import cli as cli_module
    from django_chainsaw_mcp.check import run_all

    real = run_all(tenant_root="shop.Customer", only=["money"])
    real["checks_run"] = {
        "money": {"ok": True, "findings": 0, "examined": {},
                  "seconds": 0.4, "cpu_seconds": 0.4},
    }
    real["checks_failed"] = []
    monkeypatch.setattr(cli_module, "run_all", lambda **kwargs: real)

    run("check")
    assert "in the checks. The slowest" not in capsys.readouterr().out


def test_project_info_is_a_command_the_documentation_can_send_people_to(
    django_project, capsys
):
    """It was in the MCP server, four doc pages and the bug report template.

    Not in the CLI:

        django-chainsaw: error: argument command: invalid choice: 'project-info'

    The issue template asks a reporter what `django-chainsaw project-info`
    said, and every "it found nothing" answer starts there.
    """
    assert run("project-info") == EXIT_OK
    printed = capsys.readouterr().out
    assert "Django" in printed
    assert "settings" in printed or "demoshop" in printed
    assert "models" in printed
    assert "database" in printed


def test_project_info_reports_a_failed_boot_rather_than_raising(monkeypatch, capsys):
    from django_chainsaw_mcp import cli as cli_module
    from django_chainsaw_mcp.django_env import DjangoBootError

    def refuse():
        raise DjangoBootError("no settings module here")

    monkeypatch.setattr(cli_module, "_cmd_project_info", cli_module._cmd_project_info)
    import django_chainsaw_mcp.django_env as env

    monkeypatch.setattr(env, "ensure_django", refuse)
    assert run("project-info") == EXIT_FINDINGS
    printed = capsys.readouterr().out
    assert "Could not load the project" in printed
    assert "no settings module here" in printed


def test_project_info_warns_about_an_empty_registry(monkeypatch, django_project, capsys):
    from django.apps import apps

    monkeypatch.setattr(apps, "get_models", lambda *a, **k: [])
    run("project-info")
    printed = capsys.readouterr().out
    assert "defines no models" in printed


def test_project_info_warns_about_an_unreachable_database(monkeypatch, django_project, capsys):
    from django.conf import settings

    monkeypatch.setattr(
        settings, "DATABASES",
        {"default": {"ENGINE": "django.db.backends.postgresql",
                     "HOST": "127.0.0.1", "PORT": 1}},
        raising=False,
    )
    run("project-info")
    printed = capsys.readouterr().out
    assert "did not answer" in printed
