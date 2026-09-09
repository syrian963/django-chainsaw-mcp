# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Prefetches that survive their accessor, and prefetches that do not.

Both halves matter to the check: the four below that re-query, and the five
under them that read the cache and must stay quiet.
"""

from django.db.models import Prefetch

from .models import Order, OrderLine


def open_lines_per_order():
    """The prefetch is paid for and then thrown away, once per order."""
    orders = Order.objects.prefetch_related("lines")
    out = []
    for order in orders:
        for line in order.lines.filter(quantity__gt=0):
            out.append(line)
    return out


def newest_line_per_order():
    """`.order_by()` on a prefetched manager is the same defect."""
    orders = Order.objects.prefetch_related("lines", "reminders")
    return [order.lines.order_by("-id") for order in orders]


def line_products_for(order_id):
    """One object, so no N+1 - but the prefetch query is bought and unread."""
    order = Order.objects.prefetch_related("lines").get(pk=order_id)
    return order.lines.values_list("product_id", flat=True)


def first_reminder(order_id):
    """`.all()` in the middle changes nothing; `.first()` still re-queries."""
    order = Order.objects.prefetch_related("reminders").get(pk=order_id)
    return order.reminders.all().first()


def counts_are_cached():
    """`.count()` and `.exists()` are answered from the prefetch since 4.1."""
    orders = Order.objects.prefetch_related("lines")
    return [(order.lines.count(), order.lines.exists()) for order in orders]


def slicing_is_cached():
    """A slice and an index read the cached list."""
    orders = Order.objects.prefetch_related("lines")
    return [(order.lines.all()[:2], list(order.lines.all())) for order in orders]


def filtered_into_the_prefetch():
    """The condition belongs here, which is the fix the check suggests.

    `to_attr` puts the rows on `order.open_lines` and leaves `order.lines`
    exactly as it was, so the filter on the last line is an ordinary query
    against an unprefetched manager - not a prefetch being thrown away.
    """
    orders = Order.objects.prefetch_related(
        Prefetch(
            "lines",
            queryset=OrderLine.objects.filter(quantity__gt=0),
            to_attr="open_lines",
        )
    )
    return [(order.open_lines, order.lines.filter(quantity=0)) for order in orders]


def not_prefetched_at_all():
    """No prefetch, so nothing to defeat. `queries_in_loops` owns this one."""
    return [order.lines.filter(quantity__gt=0) for order in Order.objects.all()]


def rebound_before_use():
    """The prefetch is dropped by the reassignment, so the filter is fine."""
    orders = Order.objects.prefetch_related("lines")
    orders = Order.objects.filter(status="placed")
    return [order.lines.filter(quantity__gt=0) for order in orders]
