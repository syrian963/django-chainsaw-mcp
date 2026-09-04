"""get_or_create, on fields the database does and does not make unique.

Product.sku is unique. Product.name is not. Under concurrency the difference
is one row or two, and the code is one word apart.
"""

from .models import Customer, Product, Tag


def tag_by_name(label):
    """Not unique. Two requests, two tags, then MultipleObjectsReturned."""
    return Tag.objects.get_or_create(name=label)


def product_by_name(name, price):
    """Not unique, and update_or_create is the same race with a defaults= on it."""
    return Product.objects.update_or_create(name=name, defaults={"price": price})


def product_by_sku(sku):
    """Covered: sku is unique. Correct, and must be silent."""
    return Product.objects.get_or_create(sku=sku)


def product_by_sku_and_name(sku, name):
    """Covered: a superset of a unique field still matches at most one row."""
    return Product.objects.get_or_create(sku=sku, name=name)


def product_by_pk(pk):
    """Covered: the primary key."""
    return Product.objects.get_or_create(pk=pk)


def customer_by_related(email):
    """Through a relation. Another model's business; listed nowhere."""
    return Customer.objects.get_or_create(user__email=email)
