# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

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
