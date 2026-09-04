"""Queries in loops, in the three shapes that need three different fixes."""

from .models import Category, Customer, Invoice, Order, Product


def per_row(orders):
    """Uses the loop variable, so once per row."""
    out = []
    for order in orders:
        customer = Customer.objects.get(pk=order.customer_id)
        out.append(customer.name)
    return out


def loop_invariant(orders):
    """Asks the same question every iteration and gets the same answer."""
    out = []
    for order in orders:
        default = Category.objects.get(name="Uncategorised")
        out.append((order.pk, default.pk))
    return out


def writes_per_row(orders):
    """N round trips, each its own transaction unless something wraps it."""
    for order in orders:
        order.placed_at = None
        order.save()


def nested(customers):
    """Once per row of the outer loop as well."""
    for customer in customers:
        for order in customer.orders.all():
            Invoice.objects.filter(order_id=order.pk).first()


def already_fixed(orders):
    """Fetched once, indexed, then read. Nothing to report."""
    customers = {c.pk: c for c in Customer.objects.all()}
    return [customers[o.customer_id].name for o in orders]


def hoisted(orders):
    """The invariant query, moved where it belongs."""
    default = Category.objects.get(name="Uncategorised")
    return [(order.pk, default.pk) for order in orders]


def not_a_query(rows):
    """`.count()` and `.get()` on plain Python containers must stay silent."""
    totals = {"a": 1}
    seen = []
    for row in rows:
        seen.append(totals.get(row, 0))
        seen.append([1, 2, 3].count(row))
    return seen


def small_literal_list():
    """A loop over two constants is not a loop over rows."""
    out = []
    for sku in ("a", "b"):
        out.append(Product.objects.filter(sku=sku).first())
    return out
