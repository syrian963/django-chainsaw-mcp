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
    # The endpoint is unpaginated, which is its own unbounded term. The claim
    # under test is narrower: the method fields themselves were readable, so
    # none of THEIR breakdown lines is an unknown.
    method_lines = [b for b in endpoint["breakdown"] if b["reason"].startswith("get_")]
    assert method_lines and all(b["queries"] is not None for b in method_lines)


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
    #
    # The first version of this test only asserted the key existed, which let
    # a useless signal through: DRF defines get_fields and to_representation
    # on its own base classes, so walking the whole MRO flagged every
    # serializer that has ever been written. A flag that is always on carries
    # no information, so the test now pins both directions.
    from django_chainsaw_mcp.api_contract import contract

    captured = contract()
    for name, entry in captured["serializers"].items():
        assert "reshapes_output" in entry, name

    # A serializer that overrides nothing must not be flagged. Every
    # serializer in existence inherits DRF's own get_fields, so a flag that
    # catches that is always on and carries no information.
    plain = captured["serializers"]["shop.api_serializers.CustomerSerializer"]
    assert plain["reshapes_output"] == [], plain["reshapes_output"]
    assert len(captured["reshaped"]) < captured["serializer_count"], (
        "flagging every serializer means the check is matching DRF's base "
        f"classes: {captured['reshaped']}"
    )


def test_a_project_override_of_to_representation_is_named():
    from django_chainsaw_mcp.api_contract import contract

    reshaped = contract()["reshaped"]
    assert any("ReshapedInvoiceSerializer" in name for name in reshaped), reshaped


def _bypass():
    from django_chainsaw_mcp.bypass import bypassed_effects

    return bypassed_effects()


def test_a_bulk_write_on_a_model_with_a_chain_names_what_it_skips():
    # The finding is not "bulk_create bypasses signals" but this call, on this
    # model, skipping these effects. Without the names it is a lecture.
    order = [f for f in _bypass()["findings"] if f["model"] == "shop.Order" and f["method"] == "bulk_create"]
    assert order, "Order.objects.bulk_create skips the invoice receiver"
    assert any("create_invoice_for_order" in r for r in order[0]["receivers_not_fired"])
    assert "shop.Invoice" in order[0]["models_not_written"]


def test_the_skipped_chain_is_followed_transitively():
    # announce_invoice is a receiver on Invoice, one hop down. It never fires
    # because the Invoice is never created, and the finding has to say so.
    order = next(f for f in _bypass()["findings"] if f["model"] == "shop.Order")
    assert any("announce_invoice" in r for r in order["receivers_not_fired"])


def test_an_overridden_save_counts_as_skipped_too():
    shipment = next(f for f in _bypass()["findings"] if f["model"] == "shop.Shipment")
    assert shipment["method"] == "bulk_update"
    assert any("AuditedMixin.save" in o for o in shipment["overrides_not_run"])


def test_a_bulk_write_on_a_model_with_nothing_to_skip_is_not_a_finding():
    # Product has no receivers and no save() override. bulk_create on it is
    # just fast, and reporting it is how a check earns an ignore rule.
    report = _bypass()
    assert not [f for f in report["findings"] if f["model"] == "shop.Product"]
    assert report["bulk_writes_seen"] > report["finding_count"]


def test_a_bulk_write_on_an_unknown_model_is_counted_not_invented():
    report = _bypass()
    assert report["bulk_writes_on_unresolved_model"] >= 1
    assert all(f["model"] for f in report["findings"])


def _races():
    from django_chainsaw_mcp.concurrency import race_conditions

    return race_conditions()


def _race_functions(report):
    return {f["function"].rsplit(".", 1)[-1]: f for f in report["races"]}


def test_a_fetched_instance_changed_in_python_and_saved_is_a_race():
    found = _race_functions(_races())
    assert "reserve" in found and found["reserve"]["confidence"] == "high"
    assert found["reserve"]["field"] == "stock"
    # Spelled out long-hand it is the same defect.
    assert "reserve_long_hand" in found


def test_the_three_correct_spellings_are_silent():
    # F() on the queryset, F() on the instance, select_for_update inside
    # atomic. If any of these is reported the check is noise and gets
    # switched off within a week.
    found = _race_functions(_races())
    for name in ("reserve_with_update", "reserve_with_f", "reserve_locked"):
        assert name not in found, found[name] if name in found else None


def test_a_plain_assignment_is_not_read_modify_write():
    assert "reprice" not in _race_functions(_races())


def test_a_parameter_is_reported_at_medium_confidence_and_can_be_dropped():
    from django_chainsaw_mcp.concurrency import race_conditions

    found = _race_functions(_races())
    assert found["bump_retries"]["confidence"] == "medium"
    without = _race_functions(race_conditions(include_parameters=False))
    assert "bump_retries" not in without
    assert "reserve" in without


def test_a_lock_with_no_transaction_is_a_crash_not_a_race():
    report = _races()
    names = {f["function"].rsplit(".", 1)[-1] for f in report["locks_outside_transaction"]}
    assert "lock_without_transaction" in names
    # The decorator is the transaction; the with-block is the transaction.
    assert "lock_under_decorator" not in names
    assert "reserve_locked" not in names


def _open():
    from django_chainsaw_mcp.exposure_auth import open_endpoints

    return open_endpoints()


def _open_views(report):
    return {f["view"].rsplit(".", 1)[-1]: f for f in report["findings"]}


def test_an_explicitly_open_view_on_a_leaking_serializer_is_critical():
    found = _open_views(_open())
    assert found["PublicCustomerExport"]["severity"] == "critical"
    assert "password_reset_token" in found["PublicCustomerExport"]["sensitive_fields"]
    assert found["PublicCustomerExport"]["permission_source"] == "set on the view"


def test_the_implicit_default_is_the_same_finding_and_says_where_it_came_from():
    # No permission_classes at all is the shape most leaks have. The finding
    # must say the permission came from the default, not from the view.
    found = _open_views(_open())
    entry = found["ImplicitlyOpenCustomers"]
    assert entry["severity"] == "critical"
    assert "default" in entry["permission_source"]


def test_the_same_serializer_behind_auth_is_not_a_finding():
    report = _open()
    assert "StaffCustomerExport" not in _open_views(report)
    assert report["protected_view_count"] >= 1


def test_an_open_view_exposing_nothing_sensitive_is_medium_at_most():
    from django_chainsaw_mcp.exposure_auth import open_endpoints

    found = _open_views(_open())
    assert found["PublicCatalogue"]["severity"] == "medium"
    # and it disappears entirely when only sensitive fields are asked for
    assert "PublicCatalogue" not in _open_views(open_endpoints(include_unbounded=False))


def test_a_view_deciding_at_runtime_is_listed_not_judged():
    report = _open()
    runtime = {v.rsplit(".", 1)[-1] for v in report["views_deciding_at_runtime"]}
    assert "DecidedAtRuntime" in runtime
    assert "DecidedAtRuntime" not in _open_views(report)


def test_the_open_default_is_volunteered():
    report = _open()
    assert report["default_is_open"] is True
    assert report["note"].startswith("The default permission is open")


def test_every_view_module_imports():
    # A module that fails to import is a module whose views are invisible,
    # and this project's own fixtures must not be in that state.
    assert _open()["view_discovery"]["failed"] == []


def _upserts():
    return {
        (f["model"].rsplit(".", 1)[-1], f["method"], tuple(f["lookup"])): f
        for f in _races()["unsafe_upserts"]
    }


def test_get_or_create_on_a_non_unique_field_is_reported_with_the_real_unique_fields():
    found = _upserts()
    tag = found[("Tag", "get_or_create", ("name",))]
    # The fix is only useful if it says what IS unique, so the reader can
    # judge whether to look up by that instead.
    assert ["slug"] in tag["unique_on_model"]


def test_update_or_create_is_the_same_race_and_defaults_is_not_a_lookup():
    found = _upserts()
    assert ("Product", "update_or_create", ("name",)) in found
    assert all("defaults" not in key[2] for key in found)


def test_a_lookup_covered_by_a_unique_field_is_silent_including_supersets_and_pk():
    lookups = {key[2] for key in _upserts()}
    assert ("sku",) not in lookups
    assert ("name", "sku") not in lookups, "a superset of a unique field matches at most one row"
    assert ("pk",) not in lookups


def test_a_lookup_through_a_relation_is_not_judged():
    assert not any("user__email" in key[2] for key in _upserts())


def test_an_unpaginated_list_endpoint_is_not_pretended_to_return_fifty_rows():
    # No pagination_class and no DEFAULT_PAGINATION_CLASS means the whole
    # table. The estimate can still assume a number, but it must say it is a
    # floor and name the endpoint as unbounded.
    slow = _endpoints()["SlowOrderViewSet"]
    assert slow["pagination"]["paginated"] is False
    assert slow["at_least"] is True
    reasons = " ".join(b["reason"] for b in slow["breakdown"])
    assert "no pagination" in reasons


def test_a_paginated_endpoint_uses_its_own_page_size():
    paginated = _endpoints()["PaginatedOrderViewSet"]
    assert paginated["pagination"]["paginated"] is True
    assert paginated["pagination"]["page_size"] == 20
    assert paginated["objects_assumed"] == 20


def test_unpaginated_endpoints_are_listed_at_the_top_level():
    from django_chainsaw_mcp.endpoint_cost import endpoint_cost

    report = endpoint_cost()
    names = {v.rsplit(".", 1)[-1] for v in report["unpaginated_list_endpoints"]}
    assert "SlowOrderViewSet" in names
    assert "PaginatedOrderViewSet" not in names


def test_endpoint_cost_counts_the_views_it_could_not_use():
    # A project whose views build responses by hand yields zero endpoints, and
    # "0" reads as a clean result rather than an empty one. The counts are what
    # tell those apart.
    from django_chainsaw_mcp.endpoint_cost import endpoint_cost

    report = endpoint_cost()
    assert report["views_seen"] >= report["endpoint_count"]
    assert report["views_seen"] == report["endpoint_count"] + report["views_without_serializer_class"]


def test_attribution_is_reported_as_possible_when_a_view_names_a_serializer():
    from django_chainsaw_mcp.api_contract import contract

    captured = contract()
    assert captured["attribution_possible"] is True
    # and with attribution working, unserved is a real answer rather than
    # every serializer in the project
    assert len(captured["unserved"]) < captured["serializer_count"]


def _scan():
    from django_chainsaw_mcp.scan import scan_templates

    report = scan_templates()
    return {item["template"].rsplit("/", 1)[-1]: item for item in report["results"]}, report


def test_a_function_view_supplies_context_in_all_the_usual_shapes():
    # Most Django views are written this way. On a real project the
    # class-based reader alone resolved 28 templates out of 2181.
    from django_chainsaw_mcp.django_env import ensure_django
    from django_chainsaw_mcp.scan import _view_context_map
    from pathlib import Path

    mapping = _view_context_map(Path(ensure_django().project_path))
    assert mapping["shop/render_order_list.html"] == {"orders": "shop.Order"}
    assert mapping["shop/render_order_inline.html"] == {"orders": "shop.Order"}
    assert mapping["shop/render_order_variable.html"] == {"orders": "shop.Order"}
    assert mapping["shop/render_customer.html"] == {"customer": "shop.Customer"}


def test_a_runtime_built_template_name_is_not_guessed_at():
    from django_chainsaw_mcp.django_env import ensure_django
    from django_chainsaw_mcp.scan import _view_context_map
    from pathlib import Path

    mapping = _view_context_map(Path(ensure_django().project_path))
    assert not any(name.endswith(".html}") or "{" in name for name in mapping)


def test_an_n_plus_one_inside_an_included_partial_is_found():
    # The loop is in one file and the relation traversal is in another, and
    # neither file is suspicious on its own.
    results, _ = _scan()
    inline = results["render_order_inline.html"]
    assert inline["high_severity_count"] >= 3
    assert "lines" in inline["suggested_queryset"]["prefetch_related"]
    assert "product__category" in inline["suggested_queryset"]["select_related"]


def test_include_with_only_gets_no_context_from_its_caller():
    # `only` means exactly that, so the partial's traversals resolve against
    # nothing and reporting them would be a fabrication.
    results, _ = _scan()
    assert "render_order_variable.html" not in results


def test_a_foreign_key_column_is_not_reported_as_needing_an_index():
    # `order` and `order_id` name the same column, and Django indexes every FK
    # by default. Reporting filter(order_id=...) sends somebody to add an index
    # that already exists; on a real project this was four of the seven
    # most-reported candidates.
    from django_chainsaw_mcp.indexes import missing_indexes

    reported = {(f["model"], f["field"]) for f in missing_indexes()["findings"]}
    assert ("shop.Product", "category_id") not in reported
    assert ("shop.Product", "category") not in reported
    # and a genuinely unindexed column still is
    assert ("shop.Product", "name") in reported


def _overfetch(low=False):
    from django_chainsaw_mcp.overfetch import unused_eager_loading

    report = unused_eager_loading(include_low_confidence=low)
    return {(f["view"].rsplit(".", 1)[-1], f["kind"], f["path"]): f for f in report["findings"]}


def test_a_relation_loaded_and_never_read_is_reported():
    found = _overfetch()
    assert ("OverFetchingOrderViewSet", "select_related", "customer") in found
    assert ("OverFetchingOrderViewSet", "prefetch_related", "lines__product__category") in found


def test_a_queryset_that_loads_exactly_what_it_renders_is_silent():
    # If this ever reports, the check is telling people to delete the
    # prefetches that make the endpoint fast.
    assert not [k for k in _overfetch() if k[0] == "WellTunedOrderViewSet"]


def test_a_deeper_read_earns_a_shallower_prefetch():
    # prefetch_related("lines") is earned by a read of lines__product, since
    # the deeper path cannot be traversed without the shallower one.
    assert ("WellTunedOrderViewSet", "prefetch_related", "lines") not in _overfetch()


def test_a_method_field_makes_it_low_confidence_and_hidden():
    # The select_related IS used, inside a method field this cannot read.
    # Reporting it by default would put the N+1 back.
    key = ("OpaqueOrderViewSet", "select_related", "customer")
    assert key not in _overfetch()
    low = _overfetch(low=True)
    assert key in low
    assert low[key]["confidence"] == "low"
    assert "SerializerMethodField" in low[key]["unreadable_because"]


def test_a_prefetch_object_does_not_blind_the_whole_view():
    # Prefetch(...) is the normal way to prefetch anything filtered. Treating
    # it as an unreadable runtime path made every view that uses one invisible
    # to both checks.
    found = _overfetch()
    assert ("PrefetchObjectOrderViewSet", "prefetch_related", "reminders") in found
    # and the paths the serializer does read stay silent
    assert ("PrefetchObjectOrderViewSet", "prefetch_related", "lines") not in found
    assert ("PrefetchObjectOrderViewSet", "prefetch_related", "lines__product") not in found


def test_a_prefetch_with_to_attr_is_passed_over():
    # to_attr loads the relation under another name, so a read of the relation
    # path proves nothing and its absence proves nothing either.
    assert ("RenamedPrefetchOrderViewSet", "prefetch_related", "reminders") not in _overfetch()

    endpoints = _endpoints()
    optimises = endpoints["RenamedPrefetchOrderViewSet"]["queryset_optimises"]
    assert optimises["renamed_prefetches"] == ["reminders"]
    assert optimises["dynamic"] is False


def test_a_prefetch_object_no_longer_marks_the_queryset_dynamic():
    optimises = _endpoints()["PrefetchObjectOrderViewSet"]["queryset_optimises"]
    assert optimises["dynamic"] is False
    assert "reminders" in optimises["prefetch_related"]


def _money():
    from django_chainsaw_mcp.money import money_precision

    report = money_precision()
    return {f["kind"]: f for f in report["findings"]}, report


def test_an_inexact_float_literal_is_high_and_an_exact_one_is_not():
    # This is the whole difference between a useful check and 50 false
    # alarms: Decimal(0.5) loses nothing, Decimal(0.19) is already wrong.
    found, _ = _money()
    assert found["decimal_from_inexact_float"]["severity"] == "high"
    assert found["decimal_from_exact_float"]["severity"] == "low"
    assert "0.19" in found["decimal_from_inexact_float"]["detail"]


def test_float_on_a_decimal_column_is_reported_and_a_non_money_field_is_not():
    found, report = _money()
    assert found["float_of_decimal"]["field"] == "price"
    # `quantity` is not a DecimalField anywhere, so float(order.quantity) is
    # left alone rather than guessed at.
    assert "quantity" not in report["decimal_field_names"]
    assert all(f.get("field") != "quantity" for f in report["findings"])


def test_round_on_money_is_medium_and_names_the_rounding_rule():
    found, _ = _money()
    entry = found["round_of_decimal"]
    assert entry["severity"] == "medium"
    assert "0.13" in entry["detail"]
    assert "quantize" in entry["fix"]


def test_a_float_column_holding_money_is_a_model_level_finding():
    found, _ = _money()
    entry = found["float_column_for_money"]
    assert entry["target"] == "shop.Invoice.legacy_total_fee"
    assert entry["severity"] == "high"


def test_correctly_written_money_code_is_silent():
    # correct_gross() multiplies Decimals and quantizes with an explicit
    # rounding mode. If that is ever reported the check is noise.
    _, report = _money()
    lines = {f.get("line") for f in report["findings"] if f.get("file", "").endswith("pricing.py")}
    import ast
    import io
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django

    root = Path(ensure_django().project_path)
    tree = ast.parse(io.open(root / "shop" / "pricing.py", encoding="utf-8").read())
    spans = {
        n.name: (n.lineno, n.end_lineno)
        for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
    }
    for name in ("correct_gross", "not_money"):
        start, end = spans[name]
        assert not [ln for ln in lines if start <= ln <= end], name


def _migrations():
    from django_chainsaw_mcp.migrations import migration_risk

    return migration_risk()


def test_a_data_migration_with_no_reverse_is_named():
    # Reversibility is a different question from row impact: a harmless data
    # migration with no reverse is still why a rollback fails at 3am.
    report = _migrations()
    kinds = {(e["name"], e["operation"]) for e in report["irreversible"]}
    assert ("0003_backfill_stock", "RunPython") in kinds
    assert ("0003_backfill_stock", "RunSQL") in kinds


def test_a_declared_reverse_including_noop_is_not_reported():
    # noop says going backwards should do nothing; None says nobody decided,
    # and the two are indistinguishable later unless one of them is written.
    report = _migrations()
    entry = next(e for e in report["migrations"] if e["name"] == "0003_backfill_stock")
    reversible = [op["reversible"] for op in entry["operations"]]
    assert reversible.count(True) == 2, reversible
    assert reversible.count(False) == 2, reversible


def test_a_schema_operation_has_no_reversibility_question():
    report = _migrations()
    for entry in report["migrations"]:
        for op in entry["operations"]:
            if op["operation"] not in {"RunPython", "RunSQL"}:
                assert op["reversible"] is None, op


def test_raw_sql_is_inventoried_so_clear_can_be_qualified():
    # "clear" means nothing was found here. These are the specific places the
    # search could not read, and they are short enough to check by hand.
    from django_chainsaw_mcp.deploy_safety import deploy_safety

    report = deploy_safety()
    kinds = {(e["file"].rsplit("/", 1)[-1], e["kind"]) for e in report["raw_sql_sites"]}
    assert ("rawsql.py", "cursor.execute()") in kinds
    assert ("rawsql.py", ".raw()") in kinds
    assert report["raw_sql_count"] == len(report["raw_sql_sites"])
    assert "build SQL by hand" in report["note"]


def _fastapi_root():
    from pathlib import Path

    return str(Path(__file__).resolve().parent.parent / "testprojects" / "fastapi_demo")


def _async_report():
    from django_chainsaw_mcp.asyncio_blocking import blocking_in_async

    return blocking_in_async(search_path=_fastapi_root())


def test_a_non_django_project_is_profiled_without_booting_django():
    # Every check used to start with ensure_django(), so a FastAPI project got
    # a boot error instead of an answer to a question that never needed Django.
    from pathlib import Path

    from django_chainsaw_mcp.project import get_profile

    found = get_profile(Path(_fastapi_root()))
    assert found.uses("fastapi")
    assert found.uses("sqlalchemy")
    assert not found.is_django
    assert found.async_functions >= 5


def test_a_blocking_database_call_on_the_event_loop_is_reported():
    # session.query(User).get(pk) has a call in the middle of the chain, which
    # is why a plain dotted-name walk missed the commonest form of this bug.
    direct = {(f["function"], f["kind"]) for f in _async_report()["direct"]}
    assert ("get_user", "database") in direct


def test_blocking_reached_through_a_call_is_reported_with_the_path():
    # Neither file is suspicious alone: the endpoint calls a helper, and the
    # helper is where the loop stops.
    reached = {f["function"]: f for f in _async_report()["reached_through_a_call"]}
    assert "get_report" in reached
    assert reached["get_report"]["reached_through"][-1].endswith("build_report")
    assert "notify" in reached


def test_a_sync_def_endpoint_is_never_reported():
    # FastAPI runs it in a threadpool. Telling somebody to make it async is
    # the advice that causes the outage.
    report = _async_report()
    names = {f["function"] for f in report["direct"]}
    names |= {f["function"] for f in report["reached_through_a_call"]}
    assert "get_user_sync" not in names


def test_awaited_and_non_database_calls_stay_silent():
    report = _async_report()
    names = {f["function"] for f in report["direct"]}
    names |= {f["function"] for f in report["reached_through_a_call"]}
    # httpx awaited inside an async client
    assert "proxy" not in names
    # values.count(1) on a list is not a database call
    assert "counts" not in names


def test_offloading_to_a_thread_is_not_a_finding():
    # asyncio.to_thread and loop.run_in_executor are the correct way to run
    # blocking work from an async endpoint. Both are awaited, and awaiting is
    # what makes a call safe, so they need no special case - but they need a
    # test, because the documentation once claimed they were false positives.
    report = _async_report()
    names = {f["function"] for f in report["direct"]}
    names |= {f["function"] for f in report["reached_through_a_call"]}
    assert "offloaded" not in names
    assert "via_executor" not in names


def _routes():
    from django_chainsaw_mcp.fastapi_exposure import fastapi_exposure

    report = fastapi_exposure(search_path=_fastapi_root())
    return {f["endpoint"]: f for f in report["findings"]}, report


def test_an_endpoint_with_no_response_model_returning_an_orm_object_is_critical():
    # The absence of one line is the whole bug: there is nothing in the file
    # to read or review, and the next column added joins the response.
    found, _ = _routes()
    entry = found["leaky_user"]
    assert entry["kind"] == "no_response_model"
    assert entry["severity"] == "critical"
    assert entry["authenticated"] is False


def test_the_same_shape_behind_authentication_is_high_not_critical():
    # It still leaks, to everyone who can log in.
    found, _ = _routes()
    assert found["me"]["severity"] == "high"
    assert found["me"]["authenticated"] is True


def test_a_dependency_that_is_not_authentication_does_not_count_as_one():
    # Depends(get_db) supplies a session, not an identity.
    found, _ = _routes()
    assert found["leaky_user"]["authenticated"] is False
    assert found["leaky_user"]["auth_via"] == []


def test_a_declared_model_carrying_a_sensitive_field_is_reported():
    found, _ = _routes()
    entry = found["admin_user"]
    assert entry["kind"] == "sensitive_in_response_model"
    assert entry["response_model"] == "UserAdminOut"
    assert "password_hash" in entry["sensitive_fields"]
    assert "is_superuser" in entry["sensitive_fields"]


def test_a_bounded_or_author_written_response_is_silent():
    found, _ = _routes()
    # response_model=UserOut, a narrow model
    assert "safe_user" not in found
    # FastAPI uses the return annotation the same way
    assert "annotated_user" not in found
    # a dict the author wrote is not a leak
    assert "status" not in found
