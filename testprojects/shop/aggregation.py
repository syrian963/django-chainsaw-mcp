"""Aggregates over multi-valued relations, correct and multiplied side by side.

An Order has `lines`, `shipments` and `reminders` - three reverse foreign keys.
Joining any two of them in one query multiplies the rows.
"""

from django.db.models import Avg, Count, Max, Min, Sum

from .models import Order, Product


def one_relation_is_fine():
    """One multi-valued relation, one join, correct numbers."""
    return Order.objects.annotate(line_count=Count("lines"))


def two_relations_multiply():
    """3 lines and 2 shipments gives 6 rows, and both counts come back as 6."""
    return Order.objects.annotate(
        line_count=Count("lines"),
        shipment_count=Count("shipments"),
    )


def two_relations_with_distinct_are_correct():
    """distinct=True collapses the duplicates. Deliberately silent."""
    return Order.objects.annotate(
        line_count=Count("lines", distinct=True),
        shipment_count=Count("shipments", distinct=True),
    )


def a_sum_cannot_be_saved_by_distinct():
    """Sum has no distinct option, so this is wrong even beside a fixed Count."""
    return Order.objects.annotate(
        shipment_count=Count("shipments", distinct=True),
        line_total=Sum("lines__quantity"),
    )


def a_forward_foreign_key_cannot_multiply():
    """`customer` is one row per order. Nothing multiplies. Silent."""
    return Order.objects.annotate(
        line_count=Count("lines"),
        customer_age=Avg("customer__date_of_birth__year"),
    )


def min_and_max_survive_the_multiplication():
    """A join repeats rows; the smallest value is still the smallest.

    Reported as a defect by the first version of this check, on a real query
    whose number was right.
    """
    return Order.objects.annotate(
        first_line=Min("lines__id"),
        last_shipment=Max("shipments__id"),
    )


def an_average_survives_it_too():
    """Uniform duplication multiplies the total and the count equally."""
    return Order.objects.annotate(
        line_count=Count("lines", distinct=True),
        mean_quantity=Avg("lines__quantity"),
        shipments=Count("shipments", distinct=True),
    )


def a_filter_joins_a_second_relation():
    """One aggregate, and a filter that joins a different multi-valued relation."""
    return Order.objects.annotate(line_count=Count("lines")).filter(
        shipments__tracking="X"
    )


def a_filter_on_the_same_relation_is_fine():
    """The filter joins the relation already being counted. Silent."""
    return Order.objects.annotate(line_count=Count("lines")).filter(
        lines__quantity__gt=1
    )


def across_a_many_to_many():
    """`tags` is a ManyToMany and `order_lines` a reverse FK: both multiply."""
    return Product.objects.annotate(
        tag_count=Count("tags"),
        sold=Sum("order_lines__quantity"),
    )


def assigned_to_a_local():
    """The chain starts from a variable, which is how most code is written.

    On a real project only 27 of 247 annotate() calls started from a model
    directly; the rest looked like this.
    """
    qs = Order.objects.filter(status="placed")
    return qs.annotate(
        line_count=Count("lines"),
        shipment_count=Count("shipments"),
    )


def two_functions_can_use_the_same_name():
    """`qs` here is a Product, not an Order. Resolved per function."""
    qs = Product.objects.all()
    return qs.annotate(tag_count=Count("tags"), sold=Count("order_lines"))
