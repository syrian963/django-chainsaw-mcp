# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""The HTML report, and what it has to admit."""

from __future__ import annotations

# --- the caveats the terminal learned to print, in the artifact ------------
#
# The HTML file is the thing people forward. It carried the checks that could
# not run and nothing else, so a page could list two thousand findings without
# mentioning that the project had loaded no models - which reads cleaner than
# the analysis was, the failure this whole report already had a section
# against.


def _report(**overrides):
    base = {
        "findings": [],
        "by_severity": {},
        "checks_run": {},
        "checks_failed": [],
        "checks_not_applicable": {},
        "frameworks": {},
        "finding_count": 0,
    }
    base.update(overrides)
    return base


def test_a_project_that_did_not_really_load_says_so_above_the_numbers():
    from django_chainsaw_mcp.report import html_report

    page = html_report(
        _report(registry_warning="This project has no models."), None, "x"
    )
    assert "caveat alarm" in page
    assert "Read this before the numbers" in page
    assert "This project has no models." in page
    # Above the findings, not appended after them.
    assert page.index("Read this before the numbers") < page.index("findings")


def test_an_unreachable_database_is_carried_too():
    from django_chainsaw_mcp.report import html_report

    page = html_report(
        _report(database_warning="postgres:3306 did not answer within 2s."),
        None, "x",
    )
    assert "postgres:3306 did not answer" in page
    assert "caveat alarm" in page


def test_a_healthy_project_gets_no_alarm():
    # A banner on every report is decoration, and decoration is skipped on the
    # report that needed reading.
    from django_chainsaw_mcp.report import html_report

    assert "caveat alarm" not in html_report(_report(), None, "x")


def test_a_clean_zero_is_distinguished_from_a_blind_one():
    from django_chainsaw_mcp.report import html_report

    page = html_report(
        _report(checks_run={
            "celery": {"ok": True, "findings": 0,
                       "examined": {"tasks_found": 12, "dispatches_checked": 41},
                       "cpu_seconds": 1.0},
            "money": {"ok": True, "findings": 0, "examined": {}, "cpu_seconds": 1.0},
            "loops": {"ok": True, "findings": 3, "examined": {"files_scanned": 9},
                      "cpu_seconds": 1.0},
        }),
        None, "x",
    )
    assert "ran and found nothing" in page
    assert "tasks found 12" in page
    assert "does not report its coverage" in page
    # A check that found something is not in that list.
    section = page.split("ran and found nothing")[1].split("</div>")[0]
    assert "loops" not in section


def test_where_the_time_went_appears_only_when_there_is_time_to_explain():
    from django_chainsaw_mcp.report import html_report

    quick = html_report(
        _report(checks_run={
            "money": {"ok": True, "findings": 0, "examined": {},
                      "cpu_seconds": 0.4, "seconds": 0.4},
        }),
        None, "x",
    )
    assert "of CPU went" not in quick

    slow = html_report(
        _report(checks_run={
            "deploy-safety": {"ok": True, "findings": 5, "examined": {},
                              "cpu_seconds": 40.0, "seconds": 41.0},
            "loops": {"ok": True, "findings": 1, "examined": {},
                      "cpu_seconds": 10.0, "seconds": 10.0},
        }),
        None, "x",
    )
    assert "50s of CPU went" in slow
    assert "deploy-safety" in slow
    assert "80%" in slow
    assert "--skip" in slow


def test_a_busy_machine_is_called_out_in_the_artifact_too():
    from django_chainsaw_mcp.report import html_report

    page = html_report(
        _report(checks_run={
            "races": {"ok": True, "findings": 1, "examined": {},
                      "cpu_seconds": 40.0, "seconds": 600.0},
        }),
        None, "x",
    )
    assert "machine was busy" in page


def test_a_warning_cannot_break_out_of_the_page():
    # Warnings carry a settings module name and a database host, both of which
    # come from the project being analysed. The report already escapes finding
    # text for this reason; these were new fields.
    from django_chainsaw_mcp.report import html_report

    page = html_report(
        _report(registry_warning="</div><script>alert(1)</script>"),
        None, "x",
    )
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
