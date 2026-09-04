"""The layered version: neither file looks wrong on its own.

This is the shape the single-file analysis cannot see, and the shape real
projects actually have, because the whole point of the service layer is that
it does not know who called it.
"""

from django.db import transaction

from .fulfilment import finalise, notify_only
from .models import Order


@transaction.atomic
def checkout(customer_id):
    """Opens the transaction. Calls something innocent-looking."""
    order = Order.objects.create(customer_id=customer_id)
    finalise(order.pk)          # three modules away, this sends mail
    return order


def browse(customer_id):
    """No transaction here, so the same call is fine."""
    notify_only(customer_id)


@transaction.atomic
def checkout_correctly(customer_id):
    """The deferred version of the same thing."""
    order = Order.objects.create(customer_id=customer_id)
    transaction.on_commit(lambda: finalise(order.pk))
    return order


@transaction.atomic
def checkout_via_wrapper(customer_id):
    """The call site says `notify(...)`, which matches no dispatch pattern."""
    order = Order.objects.create(customer_id=customer_id)
    from .fulfilment import notify

    notify(order.pk, "confirmation")
    return order
