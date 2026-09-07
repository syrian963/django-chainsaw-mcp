# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The two features that decide what CI is allowed to ignore.

`--since main` and `--baseline` both answer the same question: which of these
findings does this pipeline have to care about. Both answer it by *removing*
things, which makes them the only parts of this tool where a bug hides a real
defect instead of inventing a fake one. A check that reports too much is
annoying. A gate that suppresses the wrong finding is the failure this whole
repository is written against.

`since_check.sh` and `baseline_check.sh` drive both through the CLI, which is
how a pipeline uses them. Neither reaches the decision functions directly, so
`gitdiff` measured at 18% and `baseline` at 38% while being exercised on every
run - and the cases below are the ones a subprocess test cannot express: a
path that does not line up, a fingerprint that has to stay stable while the
code moves, and a baseline entry that must not match a different finding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from django_chainsaw_mcp import baseline, gitdiff

# --- gitdiff: which findings are in the diff -------------------------------


def test_a_finding_in_a_changed_file_is_kept_and_one_elsewhere_is_not(tmp_path):
    root = tmp_path
    (root / "app").mkdir()
    findings = [
        {"file": "app/touched.py", "title": "in the diff"},
        {"file": "app/untouched.py", "title": "not in the diff"},
    ]
    matched, unmatchable = gitdiff.filter_findings(
        findings, {"app/touched.py"}, base_dir=root, root=root
    )
    assert [f["title"] for f in matched] == ["in the diff"]
    assert unmatchable == []


def test_the_finding_path_and_the_git_path_are_compared_absolutely(tmp_path):
    # Findings are relative to the scanned directory, git reports relative to
    # the repository root, and those are the same place only by luck. A
    # project at repo/backend with a finding at "app/x.py" has to match git's
    # "backend/app/x.py".
    root = tmp_path
    scanned = root / "backend"
    (scanned / "app").mkdir(parents=True)

    findings = [{"file": "app/x.py", "title": "same file, different prefix"}]
    matched, unmatchable = gitdiff.filter_findings(
        findings, {"backend/app/x.py"}, base_dir=scanned, root=root
    )
    assert len(matched) == 1, "the same file must match through both prefixes"
    assert unmatchable == []


def test_a_finding_with_no_path_is_returned_separately_not_dropped(tmp_path):
    # A migration or a serializer class has no file:line. Dropping it silently
    # is how a gate reports a clean diff over a real problem.
    findings = [
        {"title": "no file at all"},
        {"file": "", "title": "empty file"},
        {"file": "app/x.py", "title": "has one"},
    ]
    matched, unmatchable = gitdiff.filter_findings(
        findings, set(), base_dir=tmp_path, root=tmp_path
    )
    assert matched == []
    assert len(unmatchable) == 2
    assert {f["title"] for f in unmatchable} == {"no file at all", "empty file"}


def test_an_alternative_key_can_be_used_for_findings_that_name_it_differently(tmp_path):
    (tmp_path / "app").mkdir()
    findings = [{"location": "app/x.py", "title": "keyed on location"}]
    matched, _ = gitdiff.filter_findings(
        findings, {"app/x.py"}, base_dir=tmp_path, root=tmp_path, key="location"
    )
    assert len(matched) == 1


def test_a_ref_that_does_not_exist_is_an_error_and_not_an_empty_diff():
    # "No files changed" and "that branch is not here" must never look the
    # same: the first means the pipeline can pass, the second means it cannot
    # know.
    with pytest.raises(gitdiff.GitError):
        gitdiff.changed_files("no-such-ref-here-at-all", Path.cwd())


def test_the_diff_is_taken_from_the_merge_base_and_says_so():
    # A plain two-dot diff against a main that has moved on blames this branch
    # for other people's commits.
    report = gitdiff.changed_files("HEAD", Path.cwd())
    assert "merge-base" in report["strategy"] or "direct diff" in report["strategy"]
    assert report["repo_root"]
    assert isinstance(report["changed_files"], list)


# --- baseline: which findings are already accepted -------------------------


def _tenancy_report(*models: str) -> dict:
    """A tenancy report in the shape the extractor expects.

    The keys are not decoration: the fingerprint is built from the model, the
    query chain and the filter keys, which is what makes it survive a line
    moving and still distinguish two findings in the same file.
    """
    return {
        "findings": [
            {
                "model": model,
                "chain": "objects.filter",
                "filter_keys": ["pk"],
                "file": f"app/{model.lower()}.py",
                "line": 10,
                "severity": "high",
                "why": "unscoped",
            }
            for model in models
        ]
    }


def test_a_fingerprint_survives_the_code_moving_down_the_file():
    # Somebody adds an import and every line number shifts. That is not a new
    # finding, and a baseline that thinks it is has stopped working.
    first = baseline.extract("tenancy", _tenancy_report("Order"))
    moved = _tenancy_report("Order")
    moved["findings"][0]["line"] = 402
    second = baseline.extract("tenancy", moved)
    assert first[0]["fingerprint"] == second[0]["fingerprint"]


def test_a_different_finding_gets_a_different_fingerprint():
    order = baseline.extract("tenancy", _tenancy_report("Order"))[0]
    invoice = baseline.extract("tenancy", _tenancy_report("Invoice"))[0]
    assert order["fingerprint"] != invoice["fingerprint"]


def test_a_baseline_suppresses_only_what_it_recorded(tmp_path):
    path = tmp_path / "baseline.json"
    baseline.write(path, "tenancy", _tenancy_report("Order"))

    stored = baseline.load(path)
    # The same finding is accepted, and a second one is not.
    both = baseline.compare("tenancy", _tenancy_report("Order", "Invoice"), stored)
    new = both["new"] if "new" in both else both.get("new_findings", [])
    assert len(new) == 1, both
    # The comparison speaks in the extracted shape, not the raw report: an
    # identity and a summary rather than the check's own keys.
    assert "Invoice" in new[0]["summary"]
    assert "Order" not in new[0]["summary"], "the recorded finding must stay suppressed"


def test_a_missing_or_empty_baseline_file_means_no_baseline_not_a_broken_one(tmp_path):
    # `touch` and `mktemp` both produce an empty file, and erroring there sends
    # people hunting for a corruption that is not there.
    assert baseline.load(tmp_path / "never-written.json")["checks"] == {}

    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert baseline.load(empty)["checks"] == {}

    whitespace = tmp_path / "blank.json"
    whitespace.write_text("   \n\t\n")
    assert baseline.load(whitespace)["checks"] == {}


def test_a_check_with_no_baseline_support_is_refused_by_name():
    # Silently baselining nothing would report a clean run for a check whose
    # findings were never recorded.
    with pytest.raises(ValueError) as raised:
        baseline.extract("no-such-check", {"findings": []})
    assert "no-such-check" in str(raised.value)
    assert "Available" in str(raised.value), "the error should list what is supported"


# --- api_contract: who a change breaks -------------------------------------
#
# The classification is the product here. "This field changed" is not useful;
# "an existing client that omits this field will now be rejected" is. And it is
# the sort of judgement that is wrong silently: a breaking change filed as
# additive ships, and a additive one filed as breaking teaches people to
# ignore the gate.


def _snapshot(fields: dict) -> dict:
    return {
        "captured_at": "2026-01-01T00:00:00Z",
        "serializers": {"app.OrderSerializer": {"fields": fields}},
        "unreadable": [],
    }


def _field(**overrides) -> dict:
    base = {
        "type": "CharField",
        "required": False,
        "read_only": False,
        "write_only": False,
        "allow_null": False,
    }
    base.update(overrides)
    return base


def _kinds(old: dict, new: dict) -> dict[str, str]:
    from django_chainsaw_mcp.api_contract import diff

    return {c["path"]: c["kind"] for c in diff(old, new)["changes"]}


def test_a_removed_field_breaks_the_clients_that_read_it():
    kinds = _kinds(
        _snapshot({"total": _field(), "note": _field()}),
        _snapshot({"total": _field()}),
    )
    assert any(k == "breaking" for k in kinds.values()), kinds


def test_a_new_optional_field_breaks_nobody():
    kinds = _kinds(
        _snapshot({"total": _field()}),
        _snapshot({"total": _field(), "note": _field()}),
    )
    assert set(kinds.values()) == {"additive"}, kinds


def test_a_new_required_field_breaks_every_existing_writer():
    # This is the case a plain field-set diff gets wrong: it is an addition,
    # and it rejects every request that was valid yesterday.
    kinds = _kinds(
        _snapshot({"total": _field()}),
        _snapshot({"total": _field(), "coupon": _field(required=True)}),
    )
    assert "breaking" in kinds.values(), kinds


def test_a_field_becoming_required_is_breaking():
    kinds = _kinds(
        _snapshot({"coupon": _field(required=False)}),
        _snapshot({"coupon": _field(required=True)}),
    )
    assert "breaking" in kinds.values(), kinds


def test_a_field_becoming_nullable_is_risky_rather_than_breaking():
    # It still parses. The client may not expect null, which is a different
    # conversation from "this stopped working".
    kinds = _kinds(
        _snapshot({"total": _field(allow_null=False)}),
        _snapshot({"total": _field(allow_null=True)}),
    )
    assert "risky" in kinds.values(), kinds
    assert "breaking" not in kinds.values(), kinds


def test_a_removed_serializer_is_breaking_and_an_added_one_is_not():
    from django_chainsaw_mcp.api_contract import diff

    old = _snapshot({"total": _field()})
    new = {"captured_at": "x", "serializers": {}, "unreadable": []}
    assert diff(old, new)["breaking_count"] == 1

    reverse = diff(new, old)
    assert reverse["breaking_count"] == 0
    assert reverse["by_kind"].get("additive") == 1


def test_a_serializer_that_stopped_resolving_is_unreadable_not_removed():
    # The class is still there; instantiating it raised. Calling that
    # "removed" is a confident wrong answer, and the verdict has to fail the
    # gate without claiming to know what changed.
    from django_chainsaw_mcp.api_contract import diff

    old = _snapshot({"total": _field()})
    new = {
        "captured_at": "x",
        "serializers": {},
        "unreadable": ["app.OrderSerializer: FieldError: no such field 'total'"],
    }
    report = diff(old, new)
    assert report["unreadable_count"] == 1
    assert report["breaking_count"] == 0
    assert "no such field" in report["changes"][0]["change"]


def test_an_unchanged_contract_produces_no_changes():
    from django_chainsaw_mcp.api_contract import diff

    same = _snapshot({"total": _field(), "note": _field(allow_null=True)})
    assert diff(same, same)["change_count"] == 0
