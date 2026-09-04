"""Check the three analysis tools against the demo project."""

import json
import os
from pathlib import Path

os.environ.setdefault("DJANGO_CHAINSAW_PROJECT_PATH", "testprojects")
os.environ.setdefault("DJANGO_CHAINSAW_SETTINGS_MODULE", "demoshop.settings")

from django_chainsaw_mcp.cascade import delete_impact  # noqa: E402
from django_chainsaw_mcp.migrations import migration_risk  # noqa: E402
from django_chainsaw_mcp.nplusone import analyse_template  # noqa: E402

ROOT = Path(__file__).parent
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
if failures:
    for line in failures:
        print("FAIL:", line)
    raise SystemExit(1)
print("ALLE CHECKS BESTANDEN")
