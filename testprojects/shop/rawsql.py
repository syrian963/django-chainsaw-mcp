"""SQL written by hand, which the column-reference search cannot read."""

from django.db import connection

from .models import Product


def cheapest_products(limit):
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, name FROM shop_product ORDER BY price LIMIT %s", [limit])
        return cursor.fetchall()


def products_raw(term):
    return Product.objects.raw("SELECT * FROM shop_product WHERE name LIKE %s", [f"%{term}%"])
