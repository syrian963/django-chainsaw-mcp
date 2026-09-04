"""Literals checked against a field's choices, right and wrong side by side.

Every wrong one here is valid Python and valid SQL. None of them raises.
"""

from .models import Order, Shipment


def correct_read():
    """The literal is one of the choices. Must stay silent."""
    return Order.objects.filter(status="canceled")


def typo_read():
    """Two Ls. Returns zero rows, forever, and says nothing."""
    return Order.objects.filter(status="cancelled")


def typo_exclude():
    return Order.objects.exclude(status="Shipped")


def typo_in_a_list():
    """One of the two is wrong, which is the version that survives review."""
    return Order.objects.filter(status__in=["canceled", "dispatched"])


def correct_in_a_list():
    return Order.objects.filter(status__in=["canceled", "shipped"])


def typo_write():
    """Worse than the read: create() never calls full_clean()."""
    return Order.objects.create(customer_id=1, status="cancelled")


def constructed_directly():
    return Order(customer_id=1, status="void")


def typo_update():
    return Order.objects.filter(pk=1).update(status="complete")


def by_reference():
    """The spelling that cannot go wrong. Must stay silent."""
    return Order.objects.filter(status=Order.Status.CANCELED)


def case_insensitive_is_left_alone():
    """`iexact` can legitimately match a differently spelled literal."""
    return Order.objects.filter(status__iexact="CANCELED")


def a_comparison_names_no_model(order):
    """Deliberately silent, and it took a false-positive rate of 5/5 to learn.

    `order` could be anything - a model, a form, a plain class holding a
    string, a `datetime.date`. Judging the literal against every model with a
    `status` field found five things on a real codebase and every one of them
    was an attribute on something that was not a model.
    """
    return order.status == "cancelled"
