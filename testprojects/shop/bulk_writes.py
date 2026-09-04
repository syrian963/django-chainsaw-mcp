"""Bulk writes, on models with and without a save() chain.

Saving an Order creates an Invoice. Saving a Shipment writes an audit row via
an overridden save() and a receiver. None of that happens on any line below,
and nothing on any line below says so.
"""

from .models import Order, Product, Shipment


def import_orders(rows):
    # Every receiver for Order is skipped: no Invoice is created.
    return Order.objects.bulk_create([Order(**row) for row in rows])


def mark_placed():
    # update() runs as one SQL statement on every matched row. No save().
    return Order.objects.filter(status="draft").update(status="placed")


def retag_shipments(shipments):
    # Skips both the AuditedMixin.save() override and the connected receivers.
    return Shipment.objects.bulk_update(shipments, ["tracking"])


def import_products(rows):
    # Product has no receivers and no save() override. This is just fast, and
    # reporting it would be noise.
    return Product.objects.bulk_create([Product(**row) for row in rows])


def archive(queryset):
    # A bypass on an unknown model. Counted, not reported.
    return queryset.update(archived=True)
