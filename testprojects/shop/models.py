"""Relation-heavy toy models. The point is the shape, not the domain."""

import datetime

from django.db import models


class Category(models.Model):
    name = models.CharField(max_length=120, unique=True)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="children",
    )


class Product(models.Model):
    sku = models.CharField(max_length=32, unique=True, db_index=True)
    name = models.CharField(max_length=200)
    price = models.DecimalField(max_digits=10, decimal_places=2)
    stock = models.PositiveIntegerField(default=0)
    category = models.ForeignKey(
        Category,
        on_delete=models.PROTECT,
        related_name="products",
    )
    tags = models.ManyToManyField("Tag", related_name="products", blank=True)


class Tag(models.Model):
    slug = models.SlugField(unique=True)


class Customer(models.Model):
    email = models.EmailField(unique=True)
    # The field that turns fields = "__all__" into a data leak.
    password_reset_token = models.CharField(max_length=64, blank=True, default="")
    date_of_birth = models.DateField(null=True, blank=True)


class Order(models.Model):
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name="orders",
    )
    placed_at = models.DateTimeField(auto_now_add=True)


class OrderLine(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="lines")
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="order_lines")
    quantity = models.PositiveIntegerField(default=1)


class Invoice(models.Model):
    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name="invoice")
    number = models.CharField(max_length=32, unique=True)
    internal_note = models.TextField(blank=True, default="")
    amount_due = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    # A FloatField holding money: the column itself cannot be exact.
    legacy_total_fee = models.FloatField(default=0.0)


class Reminder(models.Model):
    """Three datetime defaults, each wrong in a different way."""

    # A naive default: produces local wall clock, stored as if it were UTC.
    due_at = models.DateTimeField(default=datetime.datetime.now)

    # Evaluated once at import time, so every row gets the moment the process
    # started rather than the moment the row was created.
    created_at = models.DateTimeField(default=datetime.datetime(2026, 1, 1, 12, 0))

    # auto_now wins and auto_now_add is silently ignored.
    touched_at = models.DateTimeField(auto_now=True, auto_now_add=True)

    order = models.ForeignKey("Order", on_delete=models.CASCADE, related_name="reminders")


def _current_customer():
    """Stand-in for whatever the project uses to know who is asking."""
    return None


class ScopedNoteManager(models.Manager):
    """Narrows to the caller's own rows on every access."""

    def get_queryset(self):
        return super().get_queryset().filter(customer=_current_customer())


class ScopedNote(models.Model):
    """`objects` is only a plain manager by convention, and here it is not.

    Every `ScopedNote.objects.all()` in the project is already scoped, and not
    one of them looks it.
    """

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    body = models.TextField(blank=True, default="")

    objects = ScopedNoteManager()


class AuditedMixin(models.Model):
    """A save() on a base class, which is not a signal and runs on every write."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        AuditEntry.objects.create(what=self.__class__.__name__)


class AuditEntry(models.Model):
    what = models.CharField(max_length=64)


class Shipment(AuditedMixin):
    """Saving one writes an AuditEntry, and nothing in this class says so."""

    order = models.ForeignKey("Order", on_delete=models.CASCADE, related_name="shipments")
    tracking = models.CharField(max_length=64, blank=True, default="")
