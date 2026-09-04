"""Celery tasks, and the four ways people call them.

The correct and the incorrect calls are one attribute apart, which is why
they survive review.
"""

from celery import shared_task
from django.shortcuts import get_object_or_404

from .models import Customer, Order


@shared_task
def send_confirmation(order_id):
    """The parameter is called order_id. It is asking for an identifier."""
    order = Order.objects.get(pk=order_id)
    return order.pk


@shared_task
def reconcile(order_id, customer_id):
    """Two required arguments."""
    return order_id, customer_id


@shared_task(bind=True)
def retryable(self, order_id):
    """Bound: `self` is supplied by Celery, never by the caller."""
    return order_id


def dispatch_wrong(pk):
    """The whole object, where an id was asked for."""
    order = Order.objects.get(pk=pk)
    send_confirmation.delay(order)


def dispatch_wrong_inline(pk):
    """Same thing without the intermediate variable."""
    send_confirmation.delay(Order.objects.get(pk=pk))


def dispatch_wrong_shortcut(pk):
    """get_object_or_404 hands back an instance just the same."""
    customer = get_object_or_404(Customer, pk=pk)
    send_confirmation.apply_async(args=[customer])


def dispatch_too_few(pk):
    """reconcile takes two. This passes one, and Celery finds out at runtime."""
    reconcile.delay(pk)


def dispatch_too_many(pk):
    """And this passes three."""
    reconcile.delay(pk, pk, pk)


def dispatch_bound_correctly(pk):
    """`self` is Celery's, so one argument is right here."""
    retryable.delay(pk)


def dispatch_correctly(pk):
    """The identifier, which is what the task asked for."""
    order = Order.objects.get(pk=pk)
    send_confirmation.delay(order.pk)


def dispatch_by_keyword(pk):
    """Keywords are not checked for arity, and this is fine anyway."""
    reconcile.apply_async(kwargs={"order_id": pk, "customer_id": pk})
