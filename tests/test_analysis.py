# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

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
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django

    root = Path(ensure_django().project_path)
    source = open(root / "shop" / "notifications.py", encoding="utf-8").read()
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
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django

    root = Path(ensure_django().project_path)
    source = open(root / "shop" / "scoped.py", encoding="utf-8").read()
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
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django
    from django_chainsaw_mcp.scan import _view_context_map

    mapping = _view_context_map(Path(ensure_django().project_path))
    assert mapping["shop/render_order_list.html"] == {"orders": "shop.Order"}
    assert mapping["shop/render_order_inline.html"] == {"orders": "shop.Order"}
    assert mapping["shop/render_order_variable.html"] == {"orders": "shop.Order"}
    assert mapping["shop/render_customer.html"] == {"customer": "shop.Customer"}


def test_a_runtime_built_template_name_is_not_guessed_at():
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django
    from django_chainsaw_mcp.scan import _view_context_map

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
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django

    root = Path(ensure_django().project_path)
    tree = ast.parse(open(root / "shop" / "pricing.py", encoding="utf-8").read())
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


def _sqla():
    from django_chainsaw_mcp.sqlalchemy_nplusone import sqlalchemy_nplusone

    report = sqlalchemy_nplusone(search_path=_fastapi_root())
    loops = {(f["function"], f["attribute"]) for f in report["in_loops"]}
    responses = {(f["function"], f["attribute"]) for f in report["in_response_models"]}
    return loops, responses, report


def test_a_lazy_relationship_touched_in_a_loop_is_an_n_plus_one():
    loops, _, _ = _sqla()
    assert ("report", "customer") in loops


def test_a_query_that_eagerly_loads_what_the_loop_touches_is_silent():
    loops, _, _ = _sqla()
    assert not [k for k in loops if k[0] == "report_eager"]


def test_a_relationship_that_is_always_eager_is_never_reported():
    # lazy="selectin" loads it up front every time, so it cannot be an N+1.
    loops, _, _ = _sqla()
    assert not [k for k in loops if k[0] == "report_always_eager"]


def test_lazy_raise_is_the_recommended_fix_and_is_not_a_finding():
    # It turns the mistake into an exception at runtime. Reporting it would be
    # telling somebody to fix the thing they already fixed.
    loops, _, _ = _sqla()
    assert not [k for k in loops if k[0] == "report_raising"]


def test_a_response_model_walks_the_relationship_with_no_loop_to_see():
    # Serialisation happens after the endpoint returns, so nothing in the
    # function body mentions `items` at all.
    _, responses, _ = _sqla()
    assert ("list_orders", "items") in responses


def test_a_response_model_declaring_no_relationship_is_silent():
    _, responses, _ = _sqla()
    assert not [k for k in responses if k[0] == "list_order_ids"]
    assert not [k for k in responses if k[0] == "list_orders_eager"]


def test_the_aggregate_check_runs_everything_on_a_django_project():
    from django_chainsaw_mcp.check import ALL_CHECKS, run_all

    report = run_all(tenant_root="shop.Customer")
    assert set(report["checks_run"]) == set(ALL_CHECKS), report["checks_not_applicable"]
    assert report["checks_not_applicable"] == {}


def test_every_check_declares_what_it_needs():
    # A check missing from _REQUIRES silently defaults to "runs anywhere",
    # which is how a Django check ends up producing a boot error on a FastAPI
    # project instead of saying it does not apply.
    from django_chainsaw_mcp.check import _REQUIRES, ALL_CHECKS

    assert set(_REQUIRES) == set(ALL_CHECKS)
    assert set(_REQUIRES.values()) <= {"django", "drf", "fastapi", "sqlalchemy", "any"}


def _amplification(root=None):
    from django_chainsaw_mcp.amplification import amplification

    report = amplification(search_path=root)
    return {f["endpoint"]: f for f in report["findings"]}, report


def test_a_public_expensive_endpoint_is_the_finding_neither_half_makes():
    # SlowOrderViewSet has no permission class and costs ~2851 queries. Open
    # is fine on a catalogue; expensive is fine behind a login. Together they
    # are one request from anyone that costs the database three thousand.
    found, _ = _amplification()
    entry = found["shop.viewsets.SlowOrderViewSet"]
    assert entry["severity"] == "critical"
    assert entry["estimated_queries"] > 1000
    assert "no credentials" in entry["why"]


def test_a_public_but_cheap_endpoint_is_high_not_critical():
    # Unpaginated and public is real - it is the whole table - but putting it
    # next to a 2851-query endpoint under the same label buries that one.
    found, _ = _amplification()
    assert found["shop.viewsets.WellTunedOrderViewSet"]["severity"] == "high"
    assert found["shop.viewsets.WellTunedOrderViewSet"]["estimated_queries"] < 100


def test_the_fastapi_half_joins_on_the_route_inventory_not_the_findings():
    # GET /orders declares a narrow response_model, so it produces no exposure
    # finding at all - and it is still reachable by anyone and still walks a
    # relationship per row, which is the entire point.
    found, report = _amplification(_fastapi_root())
    assert report["critical_count"] >= 1
    entry = found["GET /orders"]
    assert entry["framework"] == "fastapi"
    assert "Order.items" in entry["relationships_per_row"]


def test_a_route_behind_a_dependency_is_not_an_amplification_surface():
    _, report = _amplification(_fastapi_root())
    endpoints = {f.get("function") for f in report["findings"]}
    assert "me" not in endpoints


def test_half_an_answer_is_reported_as_half_an_answer():
    # If one of the two underlying checks cannot run, the result is not a
    # clean bill of health and has to say so.
    _, report = _amplification()
    assert "checks_that_could_not_run" in report
    assert report["checks_that_could_not_run"] == []


def _celery():
    from django_chainsaw_mcp.celery_tasks import celery_arguments

    report = celery_arguments()
    instances = {(f["task"], f["model"], f["line"]) for f in report["instance_arguments"]}
    arity = {(f["task"], f["given"]) for f in report["arity_mismatches"]}
    return instances, arity, report


def test_a_model_instance_handed_to_a_task_is_reported():
    instances, _, _ = _celery()
    models = {(task, model) for task, model, _ in instances}
    assert ("send_confirmation", "Order") in models


def test_an_instance_from_get_object_or_404_counts_too():
    # It is a plain name call rather than an attribute one, which is how it
    # was missed at first.
    instances, _, _ = _celery()
    models = {(task, model) for task, model, _ in instances}
    assert ("send_confirmation", "Customer") in models


def test_a_dispatch_with_the_wrong_argument_count_is_reported():
    _, arity, _ = _celery()
    assert ("reconcile", 1) in arity
    assert ("reconcile", 3) in arity


def test_a_bound_task_does_not_count_self_against_the_caller():
    # @shared_task(bind=True) takes self from Celery, never from the call.
    _, arity, _ = _celery()
    assert not [a for a in arity if a[0] == "retryable"]


def test_passing_the_identifier_and_using_keywords_are_both_silent():
    _instances, arity, report = _celery()
    # dispatch_correctly passes order.pk
    assert report["instance_argument_count"] == 3, report["instance_arguments"]
    # dispatch_by_keyword uses kwargs, which arity cannot judge
    assert not [a for a in arity if a[0] == "reconcile" and a[1] == 0]


def test_an_unrelated_object_of_the_same_name_is_not_matched_to_a_task():
    # notifications.py has a plain object called send_confirmation. Matching
    # tasks by bare name reported every call to it against the real task's
    # signature - eighteen findings that were not real.
    _, _, report = _celery()
    files = {f["file"] for f in report["instance_arguments"] + report["arity_mismatches"]}
    assert not [f for f in files if "notifications.py" in f], files


def test_a_dispatch_inside_a_nested_function_is_counted_once():
    # _CallScan used to descend into nested defs, which were then scanned
    # again as their own scope. On a real project that reported 11 dispatches
    # where the source has 10.
    import ast
    from pathlib import Path

    from django_chainsaw_mcp.celery_tasks import celery_arguments
    from django_chainsaw_mcp.django_env import ensure_django

    root = Path(ensure_django().project_path)
    in_source = 0
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in {"delay", "apply_async", "delay_on_commit", "s", "si"}:
                    in_source += 1

    checked = celery_arguments()["dispatches_checked"]
    assert checked <= in_source, f"{checked} dispatches counted, {in_source} exist"


def test_an_empty_half_is_not_reported_as_a_clean_result():
    # Zero findings is only good news when both halves had something to work
    # with. This is the failure this check was written to avoid in other
    # people's tools, so it must not commit it itself.
    from django_chainsaw_mcp.amplification import amplification

    report = amplification()
    assert "sides_with_no_data" in report
    assert "answerable" in report
    # The demo project has both halves, so it is answerable.
    assert report["answerable"] is True


def test_the_async_check_does_not_build_a_call_graph_for_nothing():
    # A project with no async functions has nothing to report, and building a
    # graph over it cost 60 seconds on a real project to confirm that.
    import time

    from django_chainsaw_mcp.asyncio_blocking import blocking_in_async
    from django_chainsaw_mcp.django_env import ensure_django

    root = str(ensure_django().project_path) + "/demoshop"
    start = time.perf_counter()
    report = blocking_in_async(search_path=root)
    elapsed = time.perf_counter() - start

    assert report["async_functions_seen"] == 0
    assert report["finding_count"] == 0
    assert "no async functions" in report["note"]
    assert elapsed < 2.0, f"took {elapsed:.1f}s to say there is no async code"


def _loops():
    from django_chainsaw_mcp.loop_queries import queries_in_loops

    report = queries_in_loops()
    def lines(bucket):
        return {f["line"] for f in report[bucket] if f["file"].endswith("loops.py")}
    return lines("per_row"), lines("loop_invariant"), lines("writes_in_loops"), report


def _loops_spans():
    import ast
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django

    root = Path(ensure_django().project_path)
    tree = ast.parse(open(root / "shop" / "loops.py", encoding="utf-8").read())
    return {
        node.name: (node.lineno, node.end_lineno)
        for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }


def _in(lines, name):
    start, end = _loops_spans()[name]
    return any(start <= line <= end for line in lines)


def test_a_query_using_the_loop_variable_runs_once_per_row():
    per_row, _, _, _ = _loops()
    assert _in(per_row, "per_row")


def test_a_query_that_ignores_the_loop_variable_is_a_different_finding():
    # Same shape, different fix: it belongs above the loop and there is
    # nothing to trade off.
    per_row, invariant, _, _ = _loops()
    assert _in(invariant, "loop_invariant")
    assert not _in(per_row, "loop_invariant")


def test_a_nested_loop_raises_the_severity():
    _, _, _, report = _loops()
    nested = [f for f in report["per_row"]
              if f["file"].endswith("loops.py") and f["loop_depth"] > 1]
    assert nested and nested[0]["severity"] == "critical"


def test_a_write_in_a_loop_is_its_own_category():
    _, _, writes, _ = _loops()
    assert _in(writes, "writes_per_row")


def test_a_chained_lookup_is_one_finding_not_one_per_link():
    # Invoice.objects.filter(...).first() is two qualifying calls and one
    # query; reporting both doubled every chained lookup.
    _, _, _, report = _loops()
    start, end = _loops_spans()["nested"]
    inside = [f for f in report["per_row"]
              if f["file"].endswith("loops.py") and start <= f["line"] <= end]
    assert len(inside) == 1, inside


def test_the_four_correct_shapes_are_silent():
    per_row, invariant, writes, _ = _loops()
    everything = per_row | invariant | writes
    for name in ("already_fixed", "hoisted", "not_a_query", "small_literal_list"):
        assert not _in(everything, name), name


def test_a_loop_that_calls_a_function_which_queries_is_found():
    # Nothing in the loop body looks like a query, and there is one per row.
    # This is the shape a codebase organised into services actually has.
    from django_chainsaw_mcp.loop_queries import queries_in_loops

    report = queries_in_loops()
    reached = {f["queries_in"].rsplit(".", 1)[-1] for f in report["through_a_call"]}
    assert "enrich" in reached


def test_both_the_comprehension_and_the_statement_form_are_found():
    # [enrich(o) for o in orders] is the most idiomatic way to write this and
    # was the one shape the check could not see.
    _, _, _, report = _loops()
    lines = {f["line"] for f in report["through_a_call"] if f["file"].endswith("loops.py")}
    assert _in(lines, "through_a_call")
    assert _in(lines, "through_a_call_stmt")


def test_a_loop_calling_something_that_never_queries_is_silent():
    _, _, _, report = _loops()
    lines = {f["line"] for f in report["through_a_call"] if f["file"].endswith("loops.py")}
    assert not _in(lines, "calls_something_harmless")


def test_the_iterable_of_a_loop_is_not_a_query_inside_it():
    # `for p in Product.objects.all():` evaluates the queryset once. Reporting
    # it would flag the one line in the whole pattern that is fine.
    per_row, invariant, _, _ = _loops()
    assert not _in(per_row | invariant, "iterable_is_evaluated_once")
    # and the same for a comprehension's outermost iterable
    assert not _in(per_row | invariant, "already_fixed")


# --- impact: findings grouped by the entry points that reach them -----------


def _impact():
    from django_chainsaw_mcp.impact import impact

    return impact()


def _entry(report, needle):
    for entry in report["entry_points"]:
        if needle in entry["entry"]:
            return entry
    return None


def test_entry_points_are_found_for_every_framework_in_the_fixtures():
    report = _impact()
    kinds = report["entry_points_by_kind"]
    # FastAPI routes, Celery tasks and Django signal receivers all live in the
    # test projects, and all three are entry points a request can start at.
    assert kinds.get("http", 0) > 0
    assert kinds.get("task", 0) > 0
    assert kinds.get("signal", 0) > 0


def test_a_finding_two_calls_below_a_viewset_is_attributed_to_it():
    # The view calls a service, the service calls a repository, the defect is
    # in the repository. Nothing about the repository says it serves requests.
    entry = _entry(_impact(), "OrderReportViewSet.list")
    assert entry is not None
    assert entry["finding_count"] >= 1
    hops = entry["findings"][0]["through"]
    assert len(hops) > 1, "the path to the finding should name the hops it took"
    assert hops[0].endswith("OrderReportViewSet.list")


def test_a_framework_hook_counts_as_an_entry_point():
    # Nothing in a project calls get_queryset. Django calls it on every
    # request, so a finding inside one must not read as unreachable.
    from django_chainsaw_mcp.callgraph import build
    from django_chainsaw_mcp.impact import entry_points
    from django_chainsaw_mcp.project import project_root

    entries = entry_points(build(project_root()))
    assert any(q.endswith("TenantScopedViewSet.get_queryset") for q in entries)


def test_a_private_method_on_a_view_is_not_an_entry_point():
    # Making one an entry point would stop the backward walk at the helper and
    # hide the action that actually serves the request.
    from django_chainsaw_mcp.callgraph import build
    from django_chainsaw_mcp.impact import entry_points
    from django_chainsaw_mcp.project import project_root

    entries = entry_points(build(project_root()))
    assert not any(q.endswith("OrderActionViewSet._internal") for q in entries)


def test_unattributed_is_reported_separately_and_not_as_clean():
    report = _impact()
    # The fixture helpers are called by nothing, which is the honest answer.
    assert report["unattributed_count"] > 0
    assert all("reason" in f for f in report["unattributed"])
    assert "not that it is safe" in report["note"]


def test_a_decorator_written_with_arguments_is_still_recorded():
    # _dotted returns "" for a Call node, so `@shared_task(bind=True)` used to
    # record nothing at all and the task was not an entry point.
    from django_chainsaw_mcp.callgraph import build
    from django_chainsaw_mcp.project import project_root

    graph = build(project_root())
    bound = next(
        fn for name, fn in graph.functions.items() if name.endswith("tasks.retryable")
    )
    assert any(d.endswith("shared_task") or d.endswith("task") for d in bound.decorators)


def test_a_serializer_finding_points_at_a_path_not_a_bare_filename():
    # "serializers.py:16" is ambiguous the moment a project has two of them,
    # which every project of any size does, and no editor can open it.
    from django_chainsaw_mcp.serializer_nplusone import serializer_nplusone

    located = [f["location"] for f in serializer_nplusone()["findings"] if f["location"]]
    assert located
    assert all("/" in where for where in located), located[:3]


def test_a_serializer_n_plus_one_is_attributed_to_the_views_that_declare_it():
    # A serializer field is inside no function, so the backward walk has
    # nothing to start from. DRF knows which view declares the serializer,
    # which is the same question asked about a class instead.
    report = _impact()
    served = [
        entry for entry in report["entry_points"]
        if any(f["check"] == "n+1-serializer" for f in entry["findings"])
    ]
    assert served, "no serializer finding reached a view"
    assert any("ProductViewSet" in entry["entry"] for entry in served)


def test_a_serializer_no_view_declares_stays_unattributed():
    report = _impact()
    # It is still in the contract and still a finding. It is simply not on the
    # path of any request this can see, and saying otherwise would be a guess.
    assert any(f["check"] == "n+1-serializer" for f in report["unattributed"])


# --- impact: the URLconf, and serializers built in a method body ------------


def test_a_plain_function_view_is_found_through_the_urlconf():
    # No decorator, no view class. Nothing in its source says it serves HTTP,
    # so without reading the URLconf its defects are reached by nothing.
    entry = _entry(_impact(), "service_layer.daily_report")
    assert entry is not None
    assert entry["finding_count"] >= 1
    assert entry["url"] == "/reports/daily/"


def test_an_entry_point_carries_the_url_it_is_served_at():
    report = _impact()
    assert report["entry_points_from_the_urlconf"] > 0
    routed = [e for e in report["entry_points"] if e.get("url")]
    assert routed
    assert all(e["label"].startswith(e["url"]) for e in routed)


def test_a_private_method_on_a_routed_viewset_is_still_not_an_entry_point():
    # The URLconf names the class, which is not a licence to promote every
    # method on it: the backward walk would stop at the helper and hide the
    # action that actually serves the request.
    from django_chainsaw_mcp.callgraph import build
    from django_chainsaw_mcp.impact import entry_points
    from django_chainsaw_mcp.project import project_root

    entries = entry_points(build(project_root()))
    assert any(q.endswith("OrderActionViewSet.create") for q in entries)
    assert not any(q.endswith("OrderActionViewSet._internal") for q in entries)


def test_a_serializer_built_in_a_method_body_is_attributed_to_that_view():
    # DRF's declarative serializer_class is one way to serve a serializer and
    # not the common one: a plain APIView builds it in the body, and there is
    # no attribute for anything to read.
    report = _impact()
    assert report["classes_built_inside_a_function"] > 0
    entry = _entry(report, "ManualReportView.get")
    assert entry is not None
    assert any(f["check"] == "n+1-serializer" for f in entry["findings"])


def test_an_empty_serializer_map_says_whether_it_could_not_look():
    # An empty map and a map that could not be built look identical from the
    # outside, and one means "nothing to attribute" while the other means
    # "could not look".
    assert "serializer_map_error" in _impact()


def test_the_call_graph_is_built_once_per_unchanged_tree():
    from django_chainsaw_mcp import callgraph
    from django_chainsaw_mcp.project import project_root

    root = project_root()
    callgraph.clear_cache()
    first = callgraph.build(root)
    assert callgraph.build(root) is first
    assert callgraph.build(root, refresh=True) is not first


def test_an_edited_file_invalidates_the_cached_graph(tmp_path):
    from django_chainsaw_mcp import callgraph

    source = tmp_path / "m.py"
    source.write_text("def a():\n    pass\n")
    callgraph.clear_cache()
    before = callgraph.build(tmp_path)
    assert any(q.endswith(".a") for q in before.functions)

    (tmp_path / "n.py").write_text("def b():\n    pass\n")
    after = callgraph.build(tmp_path)
    assert after is not before
    assert any(q.endswith(".b") for q in after.functions)


def test_a_nested_serializer_is_attributed_through_its_parent():
    # The nested class is named in a class body, not in any function, so
    # neither the backward walk nor the method-body route can see it.
    report = _impact()
    assert report["classes_nested_in_another_class"] > 0
    entry = _entry(report, "ManualReportView.get")
    assert entry is not None
    nested = [
        f for f in entry["findings"]
        if "CustomerBriefSerializer" in (f["title"] or "")
    ]
    assert nested, "the nested serializer's finding did not reach the view"
    # and the path names the nesting rather than stopping at the parent
    assert any(
        hop.endswith("CustomerBriefSerializer") for hop in nested[0]["through"]
    )


def test_one_field_reached_by_two_roots_gives_two_distinguishable_findings():
    # The same nested field is reported once per root serializer, because the
    # prefetch belongs on each root's queryset and those are different fixes.
    # Dropping the root made them look like one line printed twice.
    from django_chainsaw_mcp.check import run_all

    titles = [
        f["title"] for f in run_all(only=["n+1-serializer"])["findings"]
        if "CustomerBriefSerializer.orders" in f["title"]
    ]
    assert len(titles) > 1
    assert len(set(titles)) == len(titles), titles


def test_a_method_called_through_a_variable_is_unresolved_not_unreached():
    # `target.recalculate([])` plainly calls it. Which class `target` is
    # cannot be decided without type inference, and reporting "nothing calls
    # it" would be a confident wrong answer about a line that does.
    report = _impact()
    assert report["unattributed_because_the_receiver_is_unknown"] >= 1
    reasons = [
        f["reason"] for f in report["unattributed"]
        if "recalculate" in f["reason"]
    ]
    assert reasons, "the holder was not reported as called by name"
    assert "cannot be decided" in reasons[0]


def test_a_function_nothing_mentions_says_so_plainly():
    report = _impact()
    plain = [
        f for f in report["unattributed"]
        if f["reason"].startswith("nothing in the project calls")
    ]
    # The fixture helpers really are called by nothing, and that is a
    # different answer from "called, receiver unknown".
    assert plain


# --- choices: literals a field will never match ----------------------------


def _choices():
    from django_chainsaw_mcp.choices import choice_typos

    report = choice_typos()
    return report, {f["line"] for f in report["findings"]
                    if f["file"].endswith("statuses.py")}


def _at(name):
    """The line a fixture function's body starts on, by name."""
    import ast

    from django_chainsaw_mcp.project import project_root

    source = (project_root() / "shop" / "statuses.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return {
                line for line in
                (getattr(n, "lineno", None) for n in ast.walk(node))
                if line is not None
            }
    raise AssertionError(f"no fixture named {name}")


def test_a_misspelled_choice_in_a_filter_is_found():
    # Valid Python, valid SQL, zero rows, no exception, wrong forever.
    _, lines = _choices()
    assert lines & _at("typo_read")


def test_one_wrong_value_inside_a_list_is_found():
    _, lines = _choices()
    assert lines & _at("typo_in_a_list")


def test_a_write_is_found_and_says_why_it_is_worse():
    # create() never calls full_clean(), so the value reaches the column.
    report, lines = _choices()
    assert lines & _at("typo_write")
    written = [f for f in report["findings"] if f["method"] in {"create", "update", "__init__"}]
    assert written
    assert "full_clean" in written[0]["why"]


def test_a_model_constructed_directly_is_checked():
    _, lines = _choices()
    assert lines & _at("constructed_directly")


def test_the_correct_spelling_is_silent():
    _, lines = _choices()
    for name in ("correct_read", "correct_in_a_list"):
        assert not (lines & _at(name)), name


def test_referencing_the_choice_by_name_is_silent():
    # Order.Status.CANCELED cannot be misspelled without an AttributeError.
    _, lines = _choices()
    assert not (lines & _at("by_reference"))


def test_iexact_is_left_alone():
    # A case-insensitive lookup can legitimately match a differently spelled
    # literal, so a difference is not evidence of a typo.
    _, lines = _choices()
    assert not (lines & _at("case_insensitive_is_left_alone"))


def test_a_comparison_that_names_no_model_is_silent():
    # Measured at five false positives out of five on a real codebase: every
    # one was an attribute on an object that was not a model.
    _, lines = _choices()
    assert not (lines & _at("a_comparison_names_no_model"))


def test_a_string_literal_against_an_integer_field_is_not_a_typo():
    # Django coerces the value to the field's type, so filter(x="1") on an
    # IntegerField whose choices are 1 and 2 is correct code.
    import ast

    from django_chainsaw_mcp.choices import _literals

    assert _literals(ast.parse("'1'", mode="eval").body) == ["1"]
    # and the check compares string forms, which is what runtime does
    from django_chainsaw_mcp.choices import choice_typos

    assert all(
        str(f["value"]) not in {str(a) for a in f["allowed"]}
        for f in choice_typos()["findings"]
    )


# --- dangling: names the framework has to resolve --------------------------


def _dangling():
    from django_chainsaw_mcp.dangling import dangling_references

    report = dangling_references()
    return report, {(f["file"], f["name"]) for f in report["findings"]}


def _links(name):
    import ast

    from django_chainsaw_mcp.project import project_root

    source = (project_root() / "shop" / "links.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return {
                line for line in
                (getattr(n, "lineno", None) for n in ast.walk(node))
                if line is not None
            }
    raise AssertionError(f"no fixture named {name}")


def _lines_reported(report, suffix):
    return {f["line"] for f in report["findings"] if f["file"].endswith(suffix)}


def test_a_misspelled_url_name_is_found():
    report, _ = _dangling()
    assert _lines_reported(report, "links.py") & _links("wrong_reverse")


def test_reverse_lazy_and_redirect_are_checked_too():
    report, _ = _dangling()
    reported = _lines_reported(report, "links.py")
    assert reported & _links("wrong_reverse_lazy")
    assert reported & _links("wrong_redirect")


def test_a_correct_url_name_is_silent():
    report, _ = _dangling()
    reported = _lines_reported(report, "links.py")
    for name in ("correct_reverse", "correct_redirect"):
        assert not (reported & _links(name)), name


def test_redirect_to_a_path_or_a_url_is_left_alone():
    # redirect() also takes a path and a model instance, so a literal that
    # cannot be a name is not one.
    report, _ = _dangling()
    reported = _lines_reported(report, "links.py")
    for name in ("redirect_to_a_path_is_left_alone",
                 "redirect_to_an_absolute_url_is_left_alone"):
        assert not (reported & _links(name)), name


def test_a_missing_template_is_found_and_an_existing_one_is_not():
    report, _ = _dangling()
    reported = _lines_reported(report, "links.py")
    assert reported & _links("wrong_template")
    assert not (reported & _links("correct_template"))


def test_a_template_name_built_at_runtime_is_silent():
    report, _ = _dangling()
    assert not (_lines_reported(report, "links.py") & _links("a_name_built_at_runtime"))


def test_url_and_include_tags_inside_a_template_are_checked():
    report, _ = _dangling()
    from_templates = {
        f["name"] for f in report["findings"] if f["file"].endswith(".html")
    }
    assert "confirm-ordr" in from_templates
    assert "shop/_order_rows.html" in from_templates


def test_every_urlconf_is_read_not_only_the_root():
    # A project serving two sites picks the URLconf per request, and checking
    # against ROOT_URLCONF alone reported 629 working reverse() calls as
    # broken on the first real project this saw.
    report, _ = _dangling()
    assert report["urlconfs_read"] >= 2


def test_findings_are_grouped_by_name():
    # One missing name used in thirty-five places is one problem.
    report, _ = _dangling()
    grouped = {g["name"]: g for g in report["by_name"]}
    assert grouped["daily-reprot"]["uses"] == 2
    assert len(grouped["daily-reprot"]["files"]) == 2


def test_a_string_signal_sender_that_names_no_model_is_found():
    # Verified rather than assumed: Django resolves a string sender lazily, so
    # a misspelled label connects nothing, raises nothing, and the system
    # checks report nothing. The receiver simply never runs.
    report, _ = _dangling()
    senders = {f["name"] for f in report["findings"] if f["kind"] == "signal"}
    assert "shop.Ordr" in senders
    assert "shop.Shpiment" in senders, "connect(sender=...) should be checked too"
    assert "shop.Order" not in senders


def test_a_beat_entry_naming_a_task_that_does_not_exist_is_found():
    # Beat keeps scheduling it, the worker rejects each message, and the job
    # stops happening on a schedule nobody watches.
    report, _ = _dangling()
    tasks = {f["name"] for f in report["findings"] if f["kind"] == "task"}
    assert "shop.tasks.cleanup_old_orders" in tasks
    assert "shop.tasks.reconcile" not in tasks


def test_send_task_with_a_misspelled_name_is_found():
    report, _ = _dangling()
    reported = _lines_reported(report, "links.py")
    assert reported & _links("send_a_task_that_does_not")
    assert not (reported & _links("send_a_task_that_exists"))


def test_task_names_are_derived_without_loading_the_celery_app():
    # Celery's default name is module.function and an explicit name= overrides
    # it; both are readable from the source, so a project whose app is only
    # built by the worker still gets checked.
    from django_chainsaw_mcp.dangling import _task_names
    from django_chainsaw_mcp.project import project_root

    names = _task_names(project_root())
    assert "shop.tasks.reconcile" in names


# --- aggregates: numbers multiplied by a join ------------------------------


def _aggregates():
    from django_chainsaw_mcp.aggregates import multiplied_aggregates

    report = multiplied_aggregates()
    lines = {f["line"] for f in report["findings"]}
    lines |= {f["line"] for f in report["multiplied_by_a_filter"]}
    return report, lines


def _report_fn(name):
    import ast

    from django_chainsaw_mcp.project import project_root

    source = (project_root() / "shop" / "aggregation.py").read_text()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return {
                line for line in
                (getattr(n, "lineno", None) for n in ast.walk(node))
                if line is not None
            }
    raise AssertionError(f"no fixture named {name}")


def test_two_multi_valued_relations_in_one_annotate_are_reported():
    # 3 lines and 2 shipments is 6 rows, and both counts come back as 6.
    _, lines = _aggregates()
    assert lines & _report_fn("two_relations_multiply")


def test_one_relation_is_not_a_finding():
    _, lines = _aggregates()
    assert not (lines & _report_fn("one_relation_is_fine"))


def test_count_distinct_is_treated_as_correct():
    _, lines = _aggregates()
    assert not (lines & _report_fn("two_relations_with_distinct_are_correct"))


def test_a_sum_beside_a_distinct_count_is_still_wrong():
    # Sum has no distinct option; the duplicates are real rows to it.
    report, lines = _aggregates()
    assert lines & _report_fn("a_sum_cannot_be_saved_by_distinct")
    sums = [f for f in report["findings"] if any("Sum" in a for a in f["aggregates"])]
    assert sums
    assert "Subquery" in sums[0]["fix"]


def test_min_max_and_avg_survive_the_multiplication():
    # A join repeats rows uniformly within each group, so the smallest value
    # is still the smallest and the mean is unchanged. Including them reported
    # a correct query on a real project as a defect.
    _, lines = _aggregates()
    assert not (lines & _report_fn("min_and_max_survive_the_multiplication"))
    assert not (lines & _report_fn("an_average_survives_it_too"))


def test_a_forward_foreign_key_cannot_multiply_anything():
    _, lines = _aggregates()
    assert not (lines & _report_fn("a_forward_foreign_key_cannot_multiply"))


def test_a_filter_joining_a_second_relation_is_reported_separately():
    report, _ = _aggregates()
    filtered = {f["line"] for f in report["multiplied_by_a_filter"]}
    assert filtered & _report_fn("a_filter_joins_a_second_relation")
    assert not (filtered & _report_fn("a_filter_on_the_same_relation_is_fine"))


def test_a_queryset_held_in_a_local_variable_is_resolved():
    # On a real project only 27 of 247 annotate() calls started from a model
    # directly. A check that only understood that spelling saw 11% of the code.
    _, lines = _aggregates()
    assert lines & _report_fn("assigned_to_a_local")


def test_the_same_local_name_in_two_functions_stays_two_models():
    report, lines = _aggregates()
    assert lines & _report_fn("two_functions_can_use_the_same_name")
    product = [
        f for f in report["findings"]
        if f["line"] in _report_fn("two_functions_can_use_the_same_name")
    ]
    assert product and product[0]["model"].endswith("Product")


def test_the_report_says_how_much_of_the_source_it_could_resolve():
    # A clean result means nothing without the denominator: on a real project
    # only 27 of 247 annotate() calls started from a model directly.
    report, _ = _aggregates()
    assert report["annotate_calls_in_source"] > 0
    assert report["annotate_calls_in_source"] >= report["annotate_calls_seen"]


# --- querysets: which model is this chain about ----------------------------


def _resolve(source, models=None):
    """Resolve every filter() in a snippet to its model."""
    import ast

    from django_chainsaw_mcp import querysets

    models = models or {"Booking"}
    tree = ast.parse(source)
    context = querysets.scopes(tree, models)
    out = {}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "filter"):
            names, class_models = querysets.context_for(context, node)
            unwound = querysets.unwind(node, models, names, class_models)
            out[ast.unparse(node)] = unwound[0] if unwound else None
    return out


def test_a_custom_manager_resolves_like_objects_does():
    # A project with Order.available is not a project to go quiet on.
    found = _resolve("Booking.available.filter(x=1)")
    assert found["Booking.available.filter(x=1)"] == "Booking"


def test_a_local_variable_resolves_to_its_model():
    found = _resolve("def f():\n    qs = Booking.objects.all()\n    qs.filter(x=1)\n")
    assert found["qs.filter(x=1)"] == "Booking"


def test_two_functions_using_the_same_local_name_stay_separate():
    found = _resolve(
        "def a():\n    qs = Booking.objects.all()\n    qs.filter(x=1)\n"
        "def b():\n    qs = Other.objects.all()\n    qs.filter(y=2)\n",
        models={"Booking"},
    )
    assert found["qs.filter(x=1)"] == "Booking"
    assert found["qs.filter(y=2)"] is None


def test_a_name_assigned_from_two_models_is_dropped_not_guessed():
    # Picking one and being wrong points a finding at code that is fine.
    found = _resolve(
        "def f():\n    qs = Booking.objects.all()\n"
        "    qs = Other.objects.all()\n    qs.filter(x=1)\n",
        models={"Booking", "Other"},
    )
    assert found["qs.filter(x=1)"] is None


def test_self_model_resolves_when_the_class_declares_one():
    found = _resolve(
        "class V:\n    model = Booking\n"
        "    def get(self):\n        self.model.objects.filter(x=1)\n"
    )
    assert found["self.model.objects.filter(x=1)"] == "Booking"


def test_self_get_queryset_resolves_only_when_it_is_not_overridden():
    inherited = _resolve(
        "class V:\n    model = Booking\n"
        "    def get(self):\n        self.get_queryset().filter(x=1)\n"
    )
    assert inherited["self.get_queryset().filter(x=1)"] == "Booking"

    # A class that writes its own get_queryset() can return anything.
    overridden = _resolve(
        "class V:\n    model = Booking\n"
        "    def get_queryset(self):\n        return Other.objects.all()\n"
        "    def get(self):\n        self.get_queryset().filter(x=1)\n"
    )
    assert overridden["self.get_queryset().filter(x=1)"] is None


def test_an_unknown_receiver_is_not_guessed_at():
    found = _resolve("def f(qs):\n    qs.filter(x=1)\n")
    assert found["qs.filter(x=1)"] is None


# --- the source-text pre-filter in deploy_safety ---------------------------


def _scan_both_ways(root, symbols, model_symbols=frozenset()):
    """The same scan with the pre-filter on and off."""
    from django_chainsaw_mcp import deploy_safety as ds

    with_filter = ds._scan_all(root, set(symbols), set(model_symbols), 25)
    original = ds._symbol_pattern
    ds._symbol_pattern = lambda _symbols: None
    try:
        without = ds._scan_all(root, set(symbols), set(model_symbols), 25)
    finally:
        ds._symbol_pattern = original
    return with_filter, without


def test_skipping_a_file_whose_text_lacks_the_symbol_changes_nothing(tmp_path):
    """The optimisation has to be invisible in the output.

    Every match this visitor can make - an attribute, a bare name, a string
    constant, a keyword argument - is a name that appears verbatim in the
    source, so a file whose text lacks all of them cannot produce a hit.
    Measured on a 2001-file project: 31% off a scan for three specific names,
    10% off one for 158. Neither is worth a single changed finding.
    """
    (tmp_path / "uses_it.py").write_text(
        "def go(order):\n"
        "    return order.legacy_total\n"
    )
    (tmp_path / "mentions_it_in_a_string.py").write_text(
        "QUERY = 'legacy_total'\n"
    )
    (tmp_path / "unrelated.py").write_text(
        "def other(thing):\n"
        "    return thing.something_else\n"
    )

    with_filter, without = _scan_both_ways(tmp_path, {"legacy_total"})
    assert with_filter == without
    assert len(with_filter["legacy_total"]) == 2


def test_the_filter_is_not_fooled_by_a_name_inside_a_longer_word(tmp_path):
    # `total` appears inside `subtotal`, so the text filter lets the file
    # through - and the visitor, which matches whole identifiers, correctly
    # reports nothing. The filter may only ever be too permissive.
    (tmp_path / "app.py").write_text("def go(x):\n    return x.subtotal\n")
    with_filter, without = _scan_both_ways(tmp_path, {"total"})
    assert with_filter == without
    assert with_filter["total"] == []


def test_a_symbol_with_regex_characters_is_matched_literally(tmp_path):
    # A field cannot be named `a.b`, but the alternation is built from
    # whatever the migration says, and an unescaped one would match anything.
    (tmp_path / "app.py").write_text("VALUE = 'axb'\n")
    with_filter, without = _scan_both_ways(tmp_path, {"a.b"})
    assert with_filter == without
    assert with_filter["a.b"] == [], "an unescaped dot would have matched 'axb'"


def test_scanning_for_nothing_walks_nothing(tmp_path):
    from django_chainsaw_mcp import deploy_safety as ds

    (tmp_path / "app.py").write_text("x = 1\n")
    assert ds._scan_all(tmp_path, set(), set(), 25) == {}
    assert ds._symbol_pattern(set()) is None


# --- prefetches paid for and then thrown away ------------------------------


def _prefetch():
    """Findings in the fixture module, keyed by the function they sit in.

    Keyed by function rather than by line number: three of these tests were
    first written against literal lines, and three of those literals were off
    by one or two, so the assertions passed without ever reaching the code
    they were meant to guard.
    """
    import ast
    from pathlib import Path

    from django_chainsaw_mcp.django_env import ensure_django
    from django_chainsaw_mcp.prefetch import defeated_prefetches

    fixture = Path(ensure_django().project_path) / "shop" / "prefetching.py"
    tree = ast.parse(fixture.read_text(encoding="utf-8"))
    spans = [
        (node.name, node.lineno, max(
            getattr(child, "end_lineno", node.lineno) or node.lineno
            for child in ast.walk(node)
        ))
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    ]

    def owner(line):
        for name, start, end in spans:
            if start <= line <= end:
                return name
        return None

    report = defeated_prefetches()
    found = {}
    for finding in report["findings"]:
        if finding["file"].endswith("prefetching.py"):
            found.setdefault(owner(finding["line"]), []).append(finding)
    assert set(found) <= {name for name, _, _ in spans}, sorted(found)
    return found, report


def test_a_filter_on_a_prefetched_relation_is_reported_once_per_row():
    found, _ = _prefetch()
    hit, = found["open_lines_per_order"]
    assert hit["severity"] == "high"
    assert hit["relation"] == "lines"
    assert hit["call"] == "order.lines.filter()"
    assert "once per row" in hit["why"]


def test_a_single_object_gets_the_lower_severity():
    # No loop, so no N+1 - but the prefetch query is still bought and unread,
    # and calling that critical would train people to ignore the check.
    found, _ = _prefetch()
    hit, = found["line_products_for"]
    assert hit["severity"] == "medium"
    assert "bought and never read" in hit["why"]


def test_all_in_the_middle_does_not_hide_the_call():
    found, _ = _prefetch()
    hit, = found["first_reminder"]
    assert hit["call"] == "order.reminders.first()", "order.reminders.all().first()"


def test_count_and_exists_read_the_cache_and_are_not_reported():
    # Measured on Django 6.1: both are answered from the prefetched result.
    # Reporting them would be a false positive whose fix changes nothing,
    # which is the fastest way to get a check switched off.
    found, _ = _prefetch()
    assert "counts_are_cached" not in found, found.get("counts_are_cached")


def test_a_slice_of_a_prefetched_manager_is_not_reported():
    found, _ = _prefetch()
    assert "slicing_is_cached" not in found, found.get("slicing_is_cached")


def test_a_prefetch_with_to_attr_is_the_fix_and_stays_quiet():
    # to_attr="open_lines" puts the rows there and leaves order.lines
    # unprefetched, so the filter on it is an ordinary query with no prefetch
    # behind it to throw away.
    found, _ = _prefetch()
    assert "filtered_into_the_prefetch" not in found, \
        found.get("filtered_into_the_prefetch")


def test_a_relation_that_was_never_prefetched_belongs_to_another_check():
    found, _ = _prefetch()
    assert "not_prefetched_at_all" not in found, "queries_in_loops owns this one"


def test_a_name_reassigned_before_use_no_longer_carries_the_prefetch():
    # orders is prefetched and then rebound to a plain queryset on the next
    # line. Collapsing both bindings into one set for the name reports the
    # filter below as a defeated prefetch, which it is not.
    found, _ = _prefetch()
    assert "rebound_before_use" not in found, found.get("rebound_before_use")


def test_the_prefetch_check_reaches_the_merged_report():
    from django_chainsaw_mcp.check import run_all

    report = run_all(only=["prefetch"])
    assert not report["checks_failed"], report["checks_failed"]
    assert not report["checks_not_applicable"], report["checks_not_applicable"]

    ran = report["checks_run"]["prefetch"]
    assert ran["ok"] and ran["findings"] == 4, ran
    # An empty `examined` marks a zero nobody can interpret, and this check
    # must never produce one.
    assert ran["examined"].get("prefetch_sites_scanned"), ran

    mine = [f for f in report["findings"] if f["check"] == "prefetch"]
    assert mine, "the adapter dropped every finding"
    assert all(f["file"] and f["line"] and f["fix"] for f in mine)


def test_a_name_bound_in_another_function_is_not_matched():
    # Saleor has lines = ...prefetch_related(...) in one function and
    # fulfillment.lines.first() in another, a hundred lines apart. Walking the
    # module body into its functions reported that as one finding.
    import ast

    from django_chainsaw_mcp.prefetch import _collect

    tree = ast.parse(
        "def a():\n"
        "    orders = Order.objects.prefetch_related('lines')\n"
        "\n"
        "def b(order):\n"
        "    return order.lines.filter(x=1)\n"
    )
    assert not _collect(tree), "module scope leaked into a function body"
