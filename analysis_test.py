"""Check the three analysis tools against the demo project."""

import json
import os
from pathlib import Path

# Anchored on this file, not on the caller's working directory.
ROOT = Path(__file__).resolve().parent

os.environ.setdefault("DJANGO_CHAINSAW_PROJECT_PATH", str(ROOT / "testprojects"))
os.environ.setdefault("DJANGO_CHAINSAW_SETTINGS_MODULE", "demoshop.settings")

from django_chainsaw_mcp.cascade import delete_impact  # noqa: E402
from django_chainsaw_mcp.deploy_safety import deploy_safety  # noqa: E402
from django_chainsaw_mcp.migrations import migration_risk  # noqa: E402
from django_chainsaw_mcp.nplusone import analyse_template  # noqa: E402
from django_chainsaw_mcp.indexes import missing_indexes  # noqa: E402
from django_chainsaw_mcp.signals import what_happens_on  # noqa: E402
from django_chainsaw_mcp.tenancy import find_unscoped_queries  # noqa: E402

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


print("=" * 70)
print("delete_impact: shop.Customer")
print("=" * 70)
impact = delete_impact("shop.Customer")
print(impact["summary"])
for row in impact["cascades"]:
    print("  CASCADE  {:<18} via {:<10} depth {}  [{}]".format(
        row["from_model"], row["via_field"], row["depth"], row["path"]))
for row in impact["blocked_by"]:
    print("  BLOCKS   {:<18} via {:<10} {}".format(
        row["from_model"], row["via_field"], row["on_delete"]))

cascaded = {r["from_model"] for r in impact["cascades"]}
check("shop.Order" in cascaded, "Order should cascade from Customer")
check("shop.OrderLine" in cascaded, "OrderLine should cascade transitively")
check("shop.Invoice" in cascaded, "Invoice should cascade transitively")

print()
print("=" * 70)
print("delete_impact: shop.Product  (expect PROTECT to block)")
print("=" * 70)
product = delete_impact("shop.Product")
print(product["summary"])
for row in product["blocked_by"]:
    print("  BLOCKS   {:<18} via {:<10} {}".format(
        row["from_model"], row["via_field"], row["on_delete"]))
check(
    any(r["from_model"] == "shop.OrderLine" for r in product["blocked_by"]),
    "OrderLine.product uses PROTECT and must block deleting a Product",
)

print()
print("=" * 70)
print("delete_impact: shop.Category  (self-referencing FK, must not loop)")
print("=" * 70)
category = delete_impact("shop.Category")
print(category["summary"], "| max_depth_reached:", category["max_depth_reached"])
check(category["max_depth_reached"] is False, "self-reference must terminate, not hit max depth")

print()
print("=" * 70)
print("find_n_plus_one: order_list.html")
print("=" * 70)
n1 = analyse_template(
    str(ROOT / "testprojects/shop/templates/shop/order_list.html"),
    {"orders": "shop.Order", "single_order": "shop.Order"},
)
print("candidates:", n1["candidate_count"], "| high:", n1["high_severity_count"])
for f in n1["findings"]:
    print("  {:<6} {:<34} {}".format(f["severity"], f["expression"], f["suggested"]))
print()
print("  suggested select_related  :", n1["suggested_queryset"]["select_related"])
print("  suggested prefetch_related:", n1["suggested_queryset"]["prefetch_related"])

expressions = {f["expression"] for f in n1["findings"]}
check("order.customer.email" in expressions, "must flag order.customer.email")
check("order.invoice.number" in expressions, "must flag order.invoice.number")
check("order.placed_at" not in expressions, "must NOT flag a plain field")
check(
    "customer" in n1["suggested_queryset"]["select_related"],
    "customer is ManyToOne -> select_related",
)
check(
    "lines" in n1["suggested_queryset"]["prefetch_related"],
    "lines is a reverse FK -> prefetch_related",
)
low = [f for f in n1["findings"] if f["severity"] == "low"]
check(
    any(f["expression"] == "single_order.customer.email" for f in low),
    "outside a loop must be low severity",
)

print()
print("=" * 70)
print("migration_risk")
print("=" * 70)
risk = migration_risk()
print("db reachable:", risk["database_reachable"], "| migrations:", risk["migration_count"],
      "| risky:", risk["risky_count"])
for entry in risk["migrations"]:
    if entry["worst_risk"] == "safe":
        continue
    print("  {}.{}  -> {}".format(entry["app"], entry["name"], entry["worst_risk"]))
    for op in entry["operations"]:
        if op["risk"] != "safe":
            print("      {:<16} {:<22} {}".format(op["operation"], op["risk"], op["detail"][:60]))

print()
print("=" * 70)
print("deploy_safety")
print("=" * 70)
full = deploy_safety()
print("project apps:", full["project_apps"], "| third-party skipped:", full["skipped_third_party_apps"])
print("blocking:", full["blocking_count"], "| clear:", full["clear_count"])
for entry in full["blocking"]:
    print("  BLOCKING {}.{} {} {!r} (confidence {})".format(
        entry["app"], entry["migration"], entry["operation"], entry["symbol"], entry["confidence"]))
    for ref in entry["references"]:
        print("       {}:{}  [{}]".format(ref["path"], ref["line"], ref["kind"]))

check(full["blocking_count"] == 1, "the pending RemoveField must be reported as blocking")
check(
    "contenttypes" in full["skipped_third_party_apps"],
    "third-party migrations must be skipped, they generated the false positives",
)
check(
    full["project_apps"] == ["shop"],
    f"only shop is a project app, got {full['project_apps']}",
)

if full["blocking"]:
    refs = full["blocking"][0]["references"]
    kinds = {r["kind"] for r in refs}
    lines = {r["line"] for r in refs}
    check(
        {"attribute access", "string field name", "keyword argument"} <= kinds,
        f"all reference shapes must be found, got {sorted(kinds)}",
    )
    check(3 not in lines, "line 3 is inside the module docstring and must not count")
    check(len(refs) == 4, f"expected exactly the 4 real references, got {len(refs)}")

# Narrowing the scan must flip the verdict, not empty the analysis.
narrow = deploy_safety(search_path=str(ROOT / "testprojects/demoshop"))
print("narrowed scan -> blocking:", narrow["blocking_count"], "| clear:", narrow["clear_count"])
check(narrow["clear_count"] == 1, "with no references in scope the migration must be clear")
check(narrow["blocking_count"] == 0, "narrowed scan must not report blocking")

print()
print("=" * 70)
print("find_unscoped_queries: tenant root shop.Customer")
print("=" * 70)
tenancy = find_unscoped_queries(tenant_root="shop.Customer")
print("tenant-scoped models:", {k: v["path"] for k, v in tenancy["tenant_scoped_models"].items()})
print("chains seen:", tenancy["queryset_chains_seen"], "| candidates:", tenancy["unscoped_count"])
for f in tenancy["findings"]:
    print("  {:<7} {}:{:<4} {:<16} owned via {:<18} keys={}".format(
        f["severity"], f["file"], f["line"], f["model"], f["owner_path"], f["filter_keys"]))

owned = tenancy["tenant_scoped_models"]
check(owned.get("shop.Order", {}).get("path") == "customer",
      f"Order is one hop from Customer, got {owned.get('shop.Order')}")
check(owned.get("shop.OrderLine", {}).get("path") == "order__customer",
      f"OrderLine is two hops, got {owned.get('shop.OrderLine')}")
check("shop.Product" not in owned, "Product does not belong to a Customer and must not be scoped")
check("shop.Category" not in owned, "Category does not belong to a Customer")

flagged = {(f["file"], f["line"]) for f in tenancy["findings"]}

# Assertions are scoped to api.py, which was written for this check. Counting
# every finding in the project makes the test fail whenever the project grows,
# and that trains people to edit the number instead of reading the failure.
api_findings = [f for f in tenancy["findings"] if f["file"].endswith("api.py")]
api_lines = sorted(f["line"] for f in api_findings)

check(api_lines == [14, 19, 24, 29],
      f"api.py has exactly four unscoped views, got lines {api_lines}")
check(len(flagged) == len(tenancy["findings"]),
      "each queryset must be reported once; ast.walk visits inner and outer calls")
check(sum(1 for f in api_findings if f["severity"] == "high") == 2,
      "two of the api.py findings are one hop from the owner")
check(all(f["model"] != "shop.Product" for f in tenancy["findings"]),
      "Product.objects.all() is legitimate and must not be flagged")
check(all("create" not in f["chain"] for f in tenancy["findings"]),
      "inserting a row cannot leak data and must not be reported")

# The three in reports.py are genuine: they read tenant data across customers.
report_lines = sorted(f["line"] for f in tenancy["findings"] if f["file"].endswith("reports.py"))
check(report_lines == [38, 42, 54],
      f"reports.py has three genuine cross-customer reads, got {report_lines}")

print()
print("=" * 70)
print("what_happens_on: shop.OrderLine save, three hops deep")
print("=" * 70)
chain = what_happens_on("shop.OrderLine", "save")
print("receivers:", chain["receiver_count"], "| models written:", chain["models_written"])
for step in chain["chain"]:
    if "receiver" not in step:
        print("  (cycle)", step["model"], step["note"])
        continue
    print("  d{} {:<28} on {}".format(step["depth"], step["receiver"].split(".")[-1], step["on_model"]))
    for write in step.get("writes", []):
        print("        writes {} -> {}".format(write["target"], write["resolved_model"]))
    for effect in step.get("side_effects", []):
        print("        {}: {}()".format(effect["kind"], effect["call"]))

check(chain["receiver_count"] == 3,
      f"the chain is three receivers deep, got {chain['receiver_count']}")
check(set(chain["models_written"]) == {"shop.Order", "shop.Invoice"},
      f"OrderLine.save writes Order then Invoice, got {chain['models_written']}")
effect_calls = {e["call"] for e in chain["side_effects"]}
check("delay" in effect_calls,
      f"a Celery task three hops away must surface, got {sorted(effect_calls)}")
check("set" in effect_calls, "the cache write must surface")

first = next(s for s in chain["chain"] if "receiver" in s)
resolved = [w for w in first["writes"] if w["resolved_model"] == "shop.Order"]
check(bool(resolved),
      "instance.order.save() must resolve through the sender's relations")
check(all(w["method"] != "set" or w["resolved_model"] for s in chain["chain"]
          if "receiver" in s for w in s.get("writes", [])),
      "cache.set() is not a model write and must not be reported as one")

delete_chain = what_happens_on("shop.Order", "delete")
check(delete_chain["receiver_count"] == 1,
      f"one post_delete receiver on Order, got {delete_chain['receiver_count']}")
check(not delete_chain["models_written"],
      f"the delete receiver writes no model, got {delete_chain['models_written']}")

print()
print("=" * 70)
print("missing_indexes")
print("=" * 70)
idx = missing_indexes()
print("candidates:", idx["candidate_count"], "| high:", idx["high_severity_count"],
      "| ignored lookups:", idx["ignored_lookups"])
for f in idx["findings"]:
    print("  {:<7} {}.{:<12} {:<22} {}x  {}".format(
        f["severity"], f["model"], f["field"], f["field_type"],
        f["occurrences"], ", ".join(f["methods"])))

flagged = {(f["model"], f["field"]) for f in idx["findings"]}

check(("shop.Product", "name") in flagged, "Product.name has no index and is filtered on")
check(("shop.Product", "price") in flagged, "Product.price has no index")
check(("shop.Order", "placed_at") in flagged, "Order.placed_at has no index")

# Everything the database can already seek on must stay out.
check(("shop.Product", "sku") not in flagged, "sku is unique and db_index=True")
check(("shop.Invoice", "number") not in flagged, "Invoice.number is unique")
check(("shop.OrderLine", "order") not in flagged, "a ForeignKey is indexed by Django")
check(("shop.Category", "name") not in flagged, "Category.name is unique")

name_finding = next((f for f in idx["findings"] if f["field"] == "name"
                     and f["model"] == "shop.Product"), None)
check(name_finding is not None and name_finding["severity"] == "high",
      "Product.name is asked for three times including order_by, so high")
check(name_finding is not None and "order_by" in name_finding["methods"],
      f"order_by must be recorded, got {name_finding['methods'] if name_finding else None}")
check(idx["ignored_lookups"].get("icontains") == 1,
      f"icontains cannot use a btree index and must be ignored, got {idx['ignored_lookups']}")

print()
print("=" * 70)
if failures:
    for line in failures:
        print("FAIL:", line)
    raise SystemExit(1)
print("ALLE CHECKS BESTANDEN")
