"""Stock adjustments, written every way people actually write them.

Three of these lose sales under load. Three are correct. The point of the
fixture is that they are almost the same code, and the analysis has to be
silent on the correct ones or nobody will leave it switched on.
"""

from django.db import transaction
from django.db.models import F

from .models import Order, Product


def reserve(product_id, quantity):
    """The classic. Two requests read 10, both write 7, one sale is gone."""
    product = Product.objects.get(pk=product_id)
    product.stock -= quantity
    product.save()


def reserve_long_hand(product_id, quantity):
    """Same race, spelled out."""
    product = Product.objects.get(pk=product_id)
    product.stock = product.stock - quantity
    product.save(update_fields=["stock"])


def bump_retries(order):
    """On a parameter. The caller might hold a lock, so medium confidence."""
    order.retries += 1
    order.save()


def reserve_with_update(product_id, quantity):
    """Correct: the database does the arithmetic."""
    Product.objects.filter(pk=product_id).update(stock=F("stock") - quantity)


def reserve_with_f(product_id, quantity):
    """Correct: F() on the instance, resolved at write time."""
    product = Product.objects.get(pk=product_id)
    product.stock = F("stock") - quantity
    product.save(update_fields=["stock"])


def reserve_locked(product_id, quantity):
    """Correct: the row is held from the read to the write."""
    with transaction.atomic():
        product = Product.objects.select_for_update().get(pk=product_id)
        product.stock -= quantity
        product.save()


def lock_without_transaction(product_id):
    """Not a race, a crash: TransactionManagementError on evaluation."""
    return Product.objects.select_for_update().get(pk=product_id)


@transaction.atomic
def lock_under_decorator(product_id):
    """Correct: the decorator is the transaction."""
    return Product.objects.select_for_update().get(pk=product_id)


def reprice(product_id, new_price):
    """Not a race. A plain assignment is not read-modify-write."""
    product = Product.objects.get(pk=product_id)
    product.price = new_price
    product.save()
