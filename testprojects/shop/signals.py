"""Signal receivers that form a chain, which is the point.

Saving an Order writes an Invoice. Saving an Invoice queues a task and clears a
cache. Nothing at the `order.save()` call site says any of that, and that is
exactly what the tracer is for.
"""

from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Invoice, Order, OrderLine


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
