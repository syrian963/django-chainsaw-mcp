"""Queries that read fields no index covers.

Every filter here is on a real column. The point is which of them the database
can seek on and which force a scan, and that nothing in the code says which is
which.
"""

from .models import Category, Invoice, Order, OrderLine, Product


def products_by_name(term):
    # `name` has no index. Asked for three times across this module, which is
    # what should push it to high severity.
    return Product.objects.filter(name=term)


def product_by_exact_name(term):
    return Product.objects.get(name=term)


def products_sorted_by_name():
    # order_by on an unindexed column is the expensive one: a sort, every time.
    return Product.objects.order_by("name")


def products_by_price(maximum):
    # A range lookup on an unindexed DecimalField.
    return Product.objects.filter(price__lte=maximum)


def search_products(term):
    # icontains cannot use a btree index, so flagging it would be noise.
    # This must be ignored, not reported.
    return Product.objects.filter(name__icontains=term)


def orders_after(moment):
    return Order.objects.filter(placed_at__gte=moment)


def lines_by_quantity(minimum):
    return OrderLine.objects.filter(quantity__gte=minimum)


# --- already indexed, must never be reported ---------------------------------

def product_by_sku(sku):
    # sku is unique and db_index=True.
    return Product.objects.get(sku=sku)


def invoice_by_number(number):
    # number is unique.
    return Invoice.objects.get(number=number)


def lines_of_order(order):
    # order is a ForeignKey, so Django indexed it.
    return OrderLine.objects.filter(order=order)


def category_by_name(name):
    # Category.name is unique.
    return Category.objects.get(name=name)


def orders_across_relation(customer):
    # A traversal belongs to the other table and is skipped here.
    return OrderLine.objects.filter(order__customer=customer)
