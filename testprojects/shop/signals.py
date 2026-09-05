"""Signal receivers that form a chain, which is the point.

Saving an Order writes an Invoice. Saving an Invoice queues a task and clears a
cache. Nothing at the `order.save()` call site says any of that, and that is
exactly what the tracer is for.
"""

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import AuditEntry, Invoice, Order, OrderLine, Shipment


@receiver(post_save, sender=Order)
def create_invoice_for_order(sender, instance, created, **kwargs):
    """First hop: an Order write becomes an Invoice write."""
    if created:
        Invoice.objects.create(order=instance, number=f"INV-{instance.pk}")


@receiver(post_save, sender=Invoice)
def announce_invoice(sender, instance, **kwargs):
    """Second hop: the Invoice write has effects of its own."""
    cache.set(f"invoice:{instance.pk}", instance.number)
    notify_accounting.delay(instance.pk)


@receiver(post_delete, sender=Order)
def clear_order_cache(sender, instance, **kwargs):
    cache.set(f"order:{instance.pk}", None)


@receiver(post_save, sender=OrderLine)
def touch_order(sender, instance, **kwargs):
    """A write back onto a model already in the chain, to exercise cycle handling."""
    instance.order.save()


class notify_accounting:  # stand-in for a Celery task, no broker needed here
    @staticmethod
    def delay(invoice_id):
        return invoice_id


def touch_audit(sender, instance, **kwargs):
    """Connected twice below, so everything in it happens twice per save."""
    AuditEntry.objects.create(what="shipment touched")


post_save.connect(touch_audit, sender=Shipment)


def _build_receiver():
    """Two calls give two distinct function objects with one qualname.

    Django's connect() deduplicates on the identity of the receiver, so
    connecting the *same* object twice is a harmless no-op and never shows up.
    The bug that does happen is a module imported under two names - once as
    `shop.signals` and once as `myproject.shop.signals` - which produces two
    separate function objects that Django cannot tell apart. This reproduces
    that faithfully, and weak=False stands in for the module-level reference
    the real case would have.
    """

    def audit_twice(sender, instance, **kwargs):
        AuditEntry.objects.create(what="counted twice")

    return audit_twice


post_save.connect(_build_receiver(), sender=Shipment, weak=False)
post_save.connect(_build_receiver(), sender=Shipment, weak=False)


@receiver(post_save, sender="shop.Tag")
def string_sender_is_fine(sender, instance, **kwargs):
    """A label that resolves. Connected, and it fires.

    Deliberately on a model no other fixture uses, so this does not lengthen
    a receiver chain another check asserts the shape of.
    """


@receiver(post_save, sender="shop.Ordr")
def string_sender_typo(sender, instance, **kwargs):
    """Never connected, never called, and nothing anywhere says so.

    Django resolves a string sender lazily through the app registry. A label
    that never appears simply never resolves: no exception on connect, no
    system check message, no receiver.
    """


post_save.connect(string_sender_typo, sender="shop.Shpiment")
