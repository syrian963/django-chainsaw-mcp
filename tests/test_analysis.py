# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The analysers, as pytest.

The shell scripts stay: they cover things pytest cannot reach in one process,
such as CLI exit codes, a fresh virtualenv, and a real git repository. This file
covers the analysis layer, which is what a contributor will want to run while
editing it.

Every assertion here names what it expects rather than counting what the demo
project happens to contain. A count breaks whenever a fixture is added, and the
reflex is to edit the number instead of reading the failure.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("django_project")


# --------------------------------------------------------------------------- structure


def test_reverse_relations_have_a_cardinality():
    from django_chainsaw_mcp.introspect import list_models

    report = list_models(app_label="shop")
    unknown = [
        f"{m['label']}.{f['name']}"
        for m in report["models"]
        for f in m["fields"]
        if f.get("relation", {}).get("kind") == "Unknown"
    ]
    assert not unknown, f"one_to_many was once missing, losing exactly these: {unknown}"


def test_forward_and_reverse_are_distinguished():
    from django_chainsaw_mcp.introspect import list_models

    order = next(
        m for m in list_models(app_label="shop")["models"] if m["label"] == "shop.Order"
    )
    directions = {
        f["name"]: f["relation"]["direction"] for f in order["fields"] if f.get("relation")
    }
    assert directions["customer"] == "forward"
    assert directions["lines"] == "reverse"


# --------------------------------------------------------------------------- cascade


def test_cascade_is_transitive():
    from django_chainsaw_mcp.cascade import delete_impact

    cascaded = {r["from_model"] for r in delete_impact("shop.Customer")["cascades"]}
    assert {"shop.Order", "shop.OrderLine", "shop.Invoice"} <= cascaded


def test_protect_blocks_rather_than_cascades():
    from django_chainsaw_mcp.cascade import delete_impact

    report = delete_impact("shop.Product")
    assert any(r["from_model"] == "shop.OrderLine" for r in report["blocked_by"])
    assert not any(r["from_model"] == "shop.OrderLine" for r in report["cascades"])


def test_self_reference_terminates():
    from django_chainsaw_mcp.cascade import delete_impact

    assert delete_impact("shop.Category")["max_depth_reached"] is False


def test_unknown_model_is_a_value_error():
    from django_chainsaw_mcp.cascade import delete_impact

    with pytest.raises(ValueError, match="Unknown model"):
        delete_impact("nope.Nope")


# --------------------------------------------------------------------------- N+1


def test_template_nested_loop_resolves(demo_root):
    from django_chainsaw_mcp.nplusone import analyse_template

    report = analyse_template(
        str(demo_root / "shop/templates/shop/order_list.html"),
        {"orders": "shop.Order", "single_order": "shop.Order"},
    )
    suggested = report["suggested_queryset"]
    assert "lines" in suggested["prefetch_related"], "a reverse FK needs prefetch, not select"
    assert "product__category" in suggested["select_related"], "the nested loop must resolve"

    flagged = {f["expression"] for f in report["findings"]}
    assert "order.placed_at" not in flagged, "a plain field is not a relation crossing"


def test_serializer_nplusone_separates_select_from_prefetch():
    from django_chainsaw_mcp.serializer_nplusone import serializer_nplusone

    report = serializer_nplusone()
    if not report["rest_framework_installed"]:
        pytest.skip("DRF not installed in this environment")

    advice = report["queryset_advice"].get("shop.api_serializers.ProductSerializer", {})
    assert "tags" in advice.get("prefetch_related", []), "ManyToMany needs prefetch_related"
    assert "category" in advice.get("select_related", []), "ManyToOne needs select_related"


# --------------------------------------------------------------------------- tenancy


def test_ownership_paths_are_shortest_first():
    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    owned = find_unscoped_queries(tenant_root="shop.Customer")["tenant_scoped_models"]
    assert owned["shop.Order"]["path"] == "customer"
    assert owned["shop.OrderLine"]["path"] == "order__customer"
    assert "shop.Product" not in owned, "the catalogue belongs to nobody"


def test_scoped_queries_are_not_flagged():
    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    findings = find_unscoped_queries(tenant_root="shop.Customer")["findings"]
    api = sorted(f["line"] for f in findings if f["file"].endswith("api.py"))
    assert api == [14, 19, 24, 29], "only the deliberately unscoped views in api.py"


def test_inserting_a_row_is_not_an_authorisation_problem():
    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    findings = find_unscoped_queries(tenant_root="shop.Customer")["findings"]
    assert all("create" not in f["chain"] for f in findings)


# --------------------------------------------------------------------------- deploy safety


def test_blocking_migration_lists_every_reference_shape():
    from django_chainsaw_mcp.deploy_safety import deploy_safety

    report = deploy_safety()
    blocking = [b for b in report["blocking"] if b["symbol"] == "legacy_code"]
    assert blocking, "the planted RemoveField must be blocking"

    kinds = {r["kind"] for r in blocking[0]["references"]}
    assert {"attribute access", "string field name", "keyword argument"} <= kinds


def test_docstrings_are_not_references():
    from django_chainsaw_mcp.deploy_safety import deploy_safety

    blocking = [b for b in deploy_safety()["blocking"] if b["symbol"] == "legacy_code"]
    lines = {r["line"] for r in blocking[0]["references"]}
    assert 3 not in lines, "line 3 of services.py is prose inside the module docstring"


def test_third_party_migrations_are_skipped():
    from django_chainsaw_mcp.deploy_safety import deploy_safety

    assert "contenttypes" in deploy_safety()["skipped_third_party_apps"]


# --------------------------------------------------------------------------- signals


def test_signal_chain_follows_three_hops():
    from django_chainsaw_mcp.signals import what_happens_on

    chain = what_happens_on("shop.OrderLine", "save")
    assert set(chain["models_written"]) == {"shop.Order", "shop.Invoice"}
    assert "delay" in {e["call"] for e in chain["side_effects"]}, (
        "a Celery task three hops away is the whole point"
    )


def test_instance_relation_resolves_through_the_model_graph():
    from django_chainsaw_mcp.signals import what_happens_on

    first = next(
        s for s in what_happens_on("shop.OrderLine", "save")["chain"] if "receiver" in s
    )
    assert any(w["resolved_model"] == "shop.Order" for w in first["writes"]), (
        "instance.order.save() must resolve, or the chain stops at hop one"
    )


# --------------------------------------------------------------------------- indexes


def test_indexed_fields_are_not_reported():
    from django_chainsaw_mcp.indexes import missing_indexes

    flagged = {(f["model"], f["field"]) for f in missing_indexes()["findings"]}
    assert ("shop.Product", "name") in flagged
    assert ("shop.Product", "sku") not in flagged, "unique implies an index"
    assert ("shop.OrderLine", "order") not in flagged, "Django indexes foreign keys"


def test_lookups_a_btree_cannot_serve_are_ignored():
    from django_chainsaw_mcp.indexes import missing_indexes

    assert missing_indexes()["ignored_lookups"].get("icontains") == 1


# --------------------------------------------------------------------------- datetimes


def test_naive_calls_are_flagged_and_correct_ones_are_not():
    from django_chainsaw_mcp.datetimes import datetime_audit

    report = datetime_audit()
    code = {(f["file"], f["line"]) for f in report["code_findings"]}
    assert ("shop/scheduling.py", 19) in code, "datetime.now() is naive"
    assert ("shop/scheduling.py", 24) in code, "utcnow() is naive and looks right"
    assert ("shop/scheduling.py", 29) in code, "a literal built from parts is naive"

    flagged_code = " ".join(f["code"] for f in report["code_findings"])
    assert "make_aware" not in flagged_code, "make_aware(datetime(...)) is correct"
    assert "timezone.now()" not in flagged_code


def test_frozen_default_is_separated_from_naive_default():
    from django_chainsaw_mcp.datetimes import datetime_audit

    kinds = {
        f["field"]: f["kind"]
        for f in datetime_audit()["model_findings"]
        if f["model"] == "shop.Reminder"
    }
    assert kinds["due_at"] == "naive_default"
    assert kinds["created_at"] == "frozen_default"
    assert kinds["touched_at"] == "conflicting_auto"


# --------------------------------------------------------------------------- correlation


def test_correlated_risk_needs_more_than_one_analyser():
    from django_chainsaw_mcp.explain import explain_model

    report = explain_model("shop.Invoice", tenant_root="shop.Customer")
    critical = [r for r in report["correlated_risks"] if r["severity"] == "critical"]
    assert critical, "owned, read unscoped and exposed is the complete leak path"
    assert len(critical[0]["seen_by"]) >= 2, (
        "a correlated risk that one analyser could see is not a correlated risk"
    )


def test_children_are_not_reported_as_a_different_owner():
    from django_chainsaw_mcp.explain import explain_model

    risks = explain_model("shop.Order", tenant_root="shop.Customer")["correlated_risks"]
    crossing = [r for r in risks if r["id"] == "cascade_crosses_ownership"]
    assert not crossing, (
        "Invoice reaches the owner as order__customer, which is Order's own path "
        "with a hop in front: the same route, not a different one"
    )


# --------------------------------------------------------------------------- check


def test_check_merges_and_sorts_by_severity():
    from django_chainsaw_mcp.check import run_all

    report = run_all(tenant_root="shop.Customer")
    assert report["finding_count"] > 0
    assert not report["checks_failed"], report["checks_failed"]

    order = ["critical", "high", "medium", "low"]
    positions = [order.index(f["severity"]) for f in report["findings"]]
    assert positions == sorted(positions), "findings must come out worst first"


def test_a_check_that_cannot_run_is_not_a_pass():
    from django_chainsaw_mcp.check import run_all

    report = run_all(tenant_root="shop.Customer", only=["indexes"])
    assert list(report["checks_run"]) == ["indexes"]


def test_unknown_check_name_is_rejected():
    from django_chainsaw_mcp.check import run_all

    with pytest.raises(ValueError, match="Unknown check"):
        run_all(only=["not-a-check"])


def test_sarif_is_well_formed():
    from django_chainsaw_mcp.check import run_all
    from django_chainsaw_mcp.sarif import to_sarif

    log = to_sarif(run_all(tenant_root="shop.Customer"))
    assert log["version"] == "2.1.0"
    run = log["runs"][0]
    assert run["tool"]["driver"]["rules"], "every finding needs a rule to hang off"
    assert all(r["level"] in {"error", "warning", "note"} for r in run["results"])


def _side_effects(**kwargs):
    from django_chainsaw_mcp.on_commit import escaping_side_effects

    return escaping_side_effects(**kwargs)["findings"]


def test_a_task_dispatched_inside_a_transaction_is_found():
    calls = {f["call"] for f in _side_effects() if f["kind"] == "task"}
    assert "send_confirmation.delay" in calls
    assert "rebuild_search_index.apply_async" in calls


def test_the_on_commit_version_of_the_same_call_is_silent():
    # The fixture writes place_order and place_order_correctly with the same
    # four calls. Reporting the second one is the failure mode that gets a
    # check switched off, so it matters more than finding the first.
    import ast
    import io
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django

    root = Path(ensure_django().project_path)
    source = io.open(root / "shop" / "notifications.py", encoding="utf-8").read()
    spans = {
        node.name: (node.lineno, node.end_lineno)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
    }

    for name in ("place_order_correctly", "send_receipt"):
        start, end = spans[name]
        inside = [
            f for f in _side_effects()
            if f["file"].endswith("notifications.py") and start <= f["line"] <= end
        ]
        assert not inside, f"{name} is correct code and must produce nothing: {inside}"


def test_a_closed_inner_block_does_not_end_the_transaction():
    # Django's nested atomic() is a savepoint, not a second transaction, so a
    # call after the inner block closes is still inside the outer one.
    lines = [
        f for f in _side_effects()
        if f["inside"].startswith("@transaction.atomic on nested_still_counts")
    ]
    assert lines, "a call after a closed inner block is still inside the transaction"


def test_guessed_calls_are_opt_in():
    default = {f["confidence"] for f in _side_effects()}
    assert "low" not in default

    asked = [f for f in _side_effects(include_low_confidence=True) if f["confidence"] == "low"]
    assert asked, ".send() is guessable and should appear when asked for"


def test_the_suggested_fix_quotes_the_real_call():
    for finding in _side_effects():
        assert finding["code"] in finding["fix"], (
            "a fix that does not contain the call is a category name, not a fix"
        )


def _tenancy():
    from django_chainsaw_mcp.tenancy import find_unscoped_queries

    return find_unscoped_queries(tenant_root="shop.Customer")


def _lines_in(entries, filename):
    return {e["line"] for e in entries if e["file"].endswith(filename)}


def test_a_mixin_that_scopes_is_not_reported():
    # OrderViewSet and LeakyOrderViewSet contain the identical line. One of
    # them inherits a get_queryset that narrows it; the other does not. If the
    # analysis cannot tell them apart it is guessing.
    report = _tenancy()
    reported = _lines_in(report["findings"], "scoped.py")
    ruled_out = _lines_in(report["scoped_elsewhere"], "scoped.py")

    assert ruled_out & reported == set(), "a query cannot be both reported and ruled out"
    assert len(ruled_out) >= 2, report["scoped_elsewhere"]
    assert len(reported) >= 2, "the genuinely unscoped queries must survive"


def test_a_default_manager_that_scopes_is_not_reported():
    report = _tenancy()
    managers = [
        e for e in report["scoped_elsewhere"] if e["scoped_by"] == "default manager"
    ]
    assert managers, "ScopedNote.objects narrows on every access"
    assert "ScopedNote" in managers[0]["model"]


def test_what_was_ruled_out_says_why():
    # There are three grounds for ruling one out and each states its own; the
    # requirement is that none of them is silent, not that they all say the
    # same thing.
    grounds = set()
    for entry in _tenancy()["scoped_elsewhere"]:
        assert entry["detail"], "suppressing a finding without a reason is just hiding it"
        assert entry["scoped_by"], entry
        grounds.add(entry["scoped_by"])
    assert len(grounds) >= 3, grounds


def test_a_serializer_nothing_imports_is_still_found():
    # The dangerous one is always the one no URLconf reaches: Django never
    # imports it, so a __subclasses__() walk reports the project as clean.
    # CustomerExportSerializer exposes a password reset token.
    from django_chainsaw_mcp.serializers import serializer_exposure

    report = serializer_exposure()
    names = {f["serializer"] for f in report["findings"]}
    assert any("CustomerExportSerializer" in n for n in names), sorted(names)


def test_a_module_that_cannot_be_imported_is_reported_not_swallowed():
    from django_chainsaw_mcp.serializers import serializer_exposure

    discovery = serializer_exposure()["discovery"]
    assert "failed" in discovery
    assert not discovery["failed"], discovery["failed"]


def test_a_nested_serializer_is_not_called_unserved():
    # OrderLineSerializer is named by no view and rendered on every order
    # response. Calling it unserved would invite deleting a field a client
    # reads, which is the failure this check exists to prevent.
    from django_chainsaw_mcp.api_contract import contract

    captured = contract()
    assert "shop.viewsets.OrderLineSerializer" in captured["serializers"]
    assert "shop.viewsets.OrderLineSerializer" not in captured["unserved"]
    assert captured["serializers"]["shop.viewsets.OrderLineSerializer"]["served_by"] == []


def test_a_serializer_a_view_names_is_attributed_to_it():
    from django_chainsaw_mcp.api_contract import contract

    entry = contract()["serializers"]["shop.viewsets.OrderDetailSerializer"]
    assert "shop.viewsets.SlowOrderViewSet" in entry["served_by"]


def _shipment_chain():
    from django_chainsaw_mcp.signals import what_happens_on

    return what_happens_on("shop.Shipment")


def test_an_overridden_save_is_part_of_the_chain():
    # AuditedMixin.save() writes an AuditEntry on every write. It is not a
    # signal, so it used to be invisible, and "nothing else happens" is the one
    # answer this tool must never give wrongly.
    report = _shipment_chain()
    overrides = [s for s in report["chain"] if s.get("kind") == "override"]
    assert overrides, report["chain"]
    assert any("AuditedMixin.save()" in s["receiver"] for s in overrides)
    assert "shop.AuditEntry" in report["models_written"]


def test_a_receiver_connected_twice_is_called_out():
    duplicates = _shipment_chain()["duplicate_receivers"]
    assert duplicates, "two distinct objects with one qualname are a real double registration"
    assert duplicates[0]["connected"] == 2
    assert "twice" in duplicates[0]["consequence"] or "2 times" in duplicates[0]["consequence"]


def test_a_model_without_an_override_reports_none():
    from django_chainsaw_mcp.signals import what_happens_on

    # Order has receivers but no save() override. Reporting one would mean the
    # MRO walk is picking up Django's own Model.save.
    assert what_happens_on("shop.Order")["override_count"] == 0


def _endpoints():
    from django_chainsaw_mcp.endpoint_cost import endpoint_cost

    return {e["view"].split(".")[-1]: e for e in endpoint_cost()["endpoints"]}


def test_a_querying_method_field_is_counted_not_shrugged_at():
    # An unknown at the top of a list endpoint is the difference between 2
    # queries and 2000, so the method body is worth reading.
    endpoint = _endpoints()["OrderSummaryViewSet"]
    fields = {m["field"]: m["queries"] for m in endpoint["method_fields"]}
    assert fields["line_count"] == 1
    assert fields["latest_line"] == 2
    assert endpoint["estimated_queries"] > 100, endpoint["breakdown"]
    assert not endpoint["at_least"], "the method was readable, so nothing is unbounded"


def test_only_that_omits_a_rendered_column_costs_queries():
    # only("id") reads as an optimisation. The serializer renders placed_at,
    # so it is fetched one row at a time: an N+1 wearing a performance hat.
    deferred = _endpoints()["DeferredOrderViewSet"]
    plain = _endpoints()["OrderSummaryViewSet"]
    assert deferred["estimated_queries"] > plain["estimated_queries"]
    reasons = " ".join(b["reason"] for b in deferred["breakdown"])
    assert "only() omits placed_at" in reasons, reasons


def test_only_does_not_blame_fields_it_could_never_have_fetched():
    # A SerializerMethodField is not a column. Naming it here would be a
    # fabricated finding dressed up as a measurement.
    reasons = " ".join(
        b["reason"] for b in _endpoints()["DeferredOrderViewSet"]["breakdown"]
    )
    assert "line_count" not in reasons.split("only() omits")[-1]
    assert "latest_line" not in reasons.split("only() omits")[-1]


def _scoped_py_spans():
    import ast
    import io
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django

    root = Path(ensure_django().project_path)
    source = io.open(root / "shop" / "scoped.py", encoding="utf-8").read()
    return {
        node.name: (node.lineno, node.end_lineno)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef)
    }


def _entries_in(entries, name):
    start, end = _scoped_py_spans()[name]
    return [e for e in entries if e["file"].endswith("scoped.py") and start <= e["line"] <= end]


def test_a_filter_further_down_the_same_function_is_not_a_leak():
    report = _tenancy()
    assert not _entries_in(report["findings"], "narrowed_further_down")
    ruled = _entries_in(report["scoped_elsewhere"], "narrowed_further_down")
    assert ruled and "further down" in ruled[0]["scoped_by"]


def test_a_permission_check_after_the_fetch_is_labelled_not_dropped():
    # Naming a guard is not proof the guard is correct, so this downgrades the
    # finding rather than removing it.
    found = _entries_in(_tenancy()["findings"], "guarded_after_fetch")
    assert found, "loading by pk without a filter is still worth seeing"
    assert found[0]["permission_check_nearby"] == "has_perm"


def test_a_genuine_leak_survives_all_of_it():
    found = _entries_in(_tenancy()["findings"], "invoices_for_everyone")
    assert found, "no amount of context-reading should excuse Invoice.objects.all()"
    assert found[0]["permission_check_nearby"] is None


def test_a_write_behind_if_created_is_marked_conditional():
    # A chain that presents a guarded write as unconditional overstates itself,
    # and the chain's whole value is that somebody trusts it.
    from django_chainsaw_mcp.signals import what_happens_on

    writes = [
        write
        for step in what_happens_on("shop.Order")["chain"]
        for write in step.get("writes", [])
        if write.get("resolved_model") == "shop.Invoice"
    ]
    assert writes, "saving an Order still writes an Invoice"
    assert all(w["conditional"] for w in writes), writes


def test_a_serializer_that_rewrites_its_own_output_is_named():
    # to_representation runs after the fields have had their say, so the
    # captured contract is the shape going in. That is a real hole and it is
    # named rather than papered over.
    from django_chainsaw_mcp.api_contract import contract

    captured = contract()
    for name, entry in captured["serializers"].items():
        assert "reshapes_output" in entry, name
    assert isinstance(captured["reshaped"], list)
