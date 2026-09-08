# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The declared output shape, and what breaks when the code drifts from it.

`check` is the only tool with a schema, and the SDK enforces it: a return that
does not fit is a tool error rather than a wrong answer. That is the trade this
file is here to keep honest. Loud beats silent, but it means a new check
inventing a fifth severity would break `check` entirely - so the vocabulary is
pinned here, where the failure reads as "the severity list moved" instead of
"check is broken".

The progress callback is tested the same way and for the same reason: it is a
count somebody reads while waiting, and a count that goes backwards or stops
short of the total is worse than none.
"""

from __future__ import annotations

from django_chainsaw_mcp import check as check_module
from django_chainsaw_mcp.schemas import CheckReport, Finding

# --- the shape --------------------------------------------------------------


def test_the_finding_model_matches_what_the_merge_actually_builds():
    # `_finding` is the one place a merged finding is constructed. If it grows
    # or renames a key, the schema handed to clients is out of date, and this
    # is the cheapest place to notice.
    built = check_module._finding(
        check="indexes",
        severity="high",
        title="t",
        location="app/x.py:12",
        detail="d",
        fix="f",
    )
    declared = set(Finding.model_fields)
    assert set(built) == declared, (
        f"_finding and the declared schema disagree: "
        f"only in code {set(built) - declared}, only in schema {declared - set(built)}"
    )
    assert Finding.model_validate(built).line == 12


def test_the_severity_vocabulary_is_the_one_the_sort_order_knows():
    # Two lists of severities in one codebase is one list too many: a severity
    # the schema accepts and `_ORDER` does not sorts to the bottom silently.
    from typing import get_args

    from django_chainsaw_mcp.schemas import Severity

    assert set(get_args(Severity)) == set(check_module._ORDER)


def test_every_severity_a_check_can_emit_is_in_the_schema(django_project):
    # The real guarantee: not that the two lists match each other, but that no
    # check emits something outside them. Read off the demo project, which
    # exercises every Django check.
    report = check_module.run_all(tenant_root="shop.Customer")
    emitted = {f["severity"] for f in report["findings"]}
    assert emitted, "the demo project produced no findings, so this proves nothing"
    assert emitted <= set(check_module._ORDER), f"unknown severity emitted: {emitted}"
    # And the whole report has to satisfy the schema clients were handed.
    CheckReport.model_validate(report)


def test_the_error_envelope_is_a_valid_return_and_not_a_validation_failure():
    # A settings module that will not import comes back as `ok: false` with a
    # message. A schema with required fields would turn that readable answer
    # into a protocol error, which is how a configuration mistake starts
    # looking like a broken server.
    parsed = CheckReport.model_validate({"ok": False, "error": "no settings module"})
    assert parsed.ok is False
    assert parsed.finding_count is None


def test_a_check_that_starts_reporting_more_is_not_a_protocol_error():
    # Extras are allowed on purpose. The alternative makes this file the thing
    # that has to be edited before any check can say more than it does today.
    parsed = Finding.model_validate(
        {
            "check": "indexes",
            "severity": "high",
            "title": "t",
            "detail": "d",
            "rows_scanned": 234_624,
        }
    )
    assert parsed.model_extra == {"rows_scanned": 234_624}


# --- where the time went ----------------------------------------------------


def test_every_check_records_how_long_it_took(django_project):
    """A full run on a 2001-file project is 22 minutes of CPU.

    Nothing in the output said which check spent it, so there was no way to
    decide what to `--skip`. `benchmark.sh` times each check, but as a
    separate process on a generated project - a different measurement, and
    one that says nothing about the repository in front of you.
    """
    from django_chainsaw_mcp.check import run_all

    report = run_all(tenant_root="shop.Customer")
    for name, state in report["checks_run"].items():
        assert "seconds" in state, f"{name} did not record its time"
        assert isinstance(state["seconds"], (int, float)), name
        assert state["seconds"] >= 0, name


def test_a_check_that_failed_is_still_timed(django_project, monkeypatch):
    # A check that spent four minutes and then raised is the most useful one
    # to know the cost of, and the timing sat inside the success branch.
    from django_chainsaw_mcp import check as check_module

    original = check_module._CHECKS["money"]

    def explode(**kwargs):
        raise RuntimeError("no")

    monkeypatch.setitem(
        check_module._CHECKS, "money", (explode, original[1], original[2])
    )
    report = check_module.run_all(only=["money"])
    state = report["checks_run"]["money"]
    assert state["ok"] is False
    assert "seconds" in state, "a check that raised was not timed"


def test_the_recorded_times_add_up_to_the_run(django_project):
    import time

    from django_chainsaw_mcp.check import run_all

    started = time.perf_counter()
    report = run_all(tenant_root="shop.Customer")
    wall = time.perf_counter() - started

    recorded = sum(state["seconds"] for state in report["checks_run"].values())
    assert recorded <= wall + 0.5, "the parts cannot exceed the whole"
    # Boot, framework detection and the merge sit outside the per-check
    # timings, so the sum is a floor rather than the total.
    assert recorded > 0


# --- the progress callback --------------------------------------------------


def test_progress_counts_every_check_and_ends_on_the_total(django_project):
    seen: list[tuple[int, int, str]] = []
    report = check_module.run_all(
        tenant_root="shop.Customer",
        on_progress=lambda done, total, upcoming: seen.append((done, total, upcoming)),
    )

    assert seen, "no progress was reported"
    total = seen[0][1]
    assert total == len(report["checks_run"]), (
        "the total has to be the checks that will run, not the ones that were "
        "selected: a client showing 21 and stopping at 18 looks stuck"
    )
    assert [d for d, _, _ in seen] == sorted(d for d, _, _ in seen), "progress went backwards"
    assert seen[-1][0] == total, f"the last report was not the total: {seen[-1]}"
    assert seen[-1][2] == "", "the final report names no upcoming check"
    names = [name for _, _, name in seen[:-1]]
    assert names == list(report["checks_run"]), (
        "each report names the check about to start, in the order they run"
    )


def test_a_narrowed_run_reports_its_own_total_not_the_full_one(django_project):
    seen: list[tuple[int, int, str]] = []
    check_module.run_all(
        only=["money"],
        on_progress=lambda done, total, upcoming: seen.append((done, total, upcoming)),
    )
    assert [(0, 1, "money"), (1, 1, "")] == seen


def test_no_callback_is_the_default_and_costs_nothing(django_project):
    # Every other caller - the CLI, the report writer, the gate - passes
    # nothing, and has to keep working unchanged.
    report = check_module.run_all(only=["money"])
    assert set(report["checks_run"]) == {"money"}
    assert report["checks_run"]["money"]["ok"] is True
    assert report["checks_run"]["money"]["findings"] == report["finding_count"]
