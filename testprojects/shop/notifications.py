"""Side effects around transactions, written the way both versions really look.

The point of this fixture is that the broken and the correct version are almost
the same code. Nothing at the call site distinguishes them except where the
call sits, which is exactly why reading the diff does not catch it.
"""

import requests
from django.core.cache import cache
from django.core.mail import EmailMessage, send_mail
from django.db import transaction

from .models import Customer, Invoice, Order


# A stand-in for a Celery task. The analysis matches the call, not the import,
# because in a real project the task lives in another module anyway.
class _Task:
    def delay(self, *args):
        ...

    def apply_async(self, *args, **kwargs):
        ...

    def delay_on_commit(self, *args):
        ...


send_confirmation = _Task()
rebuild_search_index = _Task()


@transaction.atomic
def place_order(customer_id, lines):
    """The classic. Four different ways to lose, all in one function."""
    order = Order.objects.create(customer_id=customer_id)

    # The worker can pick this up before the row is committed.
    send_confirmation.delay(order.pk)

    # The warehouse has been told about an order that may not survive.
    requests.post("https://warehouse.example/orders", json={"id": order.pk})

    # Mail cannot be unsent.
    send_mail("Your order", "Thanks", "shop@example.com", ["a@example.com"])

    # A reader can now be handed a total the database does not have.
    cache.set(f"order:{order.pk}", order.pk)

    return order


def cancel_order(order_id):
    """Same defects, opened with a `with` block instead of a decorator."""
    with transaction.atomic():
        order = Order.objects.get(pk=order_id)
        order.delete()
        rebuild_search_index.apply_async((order_id,))
        message = EmailMessage("Cancelled", "Sorry", to=["a@example.com"])
        message.send()


@transaction.atomic
def place_order_correctly(customer_id):
    """The same function, written the way it should be.

    If the analysis reports anything in here it is producing noise, which is
    the failure mode that gets a check switched off.
    """
    order = Order.objects.create(customer_id=customer_id)

    transaction.on_commit(lambda: send_confirmation.delay(order.pk))
    transaction.on_commit(
        lambda: requests.post("https://warehouse.example/orders", json={"id": order.pk})
    )
    transaction.on_commit(
        lambda: send_mail("Your order", "Thanks", "shop@example.com", ["a@example.com"])
    )
    rebuild_search_index.delay_on_commit(order.pk)

    return order


def send_receipt(invoice_id):
    """Outside any transaction, so none of this is a finding."""
    invoice = Invoice.objects.get(pk=invoice_id)
    send_mail("Receipt", invoice.number, "shop@example.com", ["a@example.com"])
    requests.post("https://accounting.example/receipts", json={"id": invoice.pk})
    send_confirmation.delay(invoice.pk)


@transaction.atomic
def nested_still_counts(customer_id):
    """The inner block ends, the transaction does not."""
    customer = Customer.objects.get(pk=customer_id)
    with transaction.atomic():
        customer.save()
    # Still inside the outer transaction, even though the `with` block closed.
    send_confirmation.delay(customer.pk)
