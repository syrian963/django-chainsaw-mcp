"""Code that has NOT caught up with migration 0002 yet.

Every reference below is to Product.legacy_code, which 0002 removes. This is
the situation deploy_safety exists to catch: the migration is ready, the code
is not, and during a rolling deploy the old pods keep executing exactly these
lines against a table where the column is already gone.

Each reference uses a different shape on purpose, so the scanner has to do more
than match a bare substring.
"""

from .models import Product


def export_rows():
    # string field name inside .values()
    return Product.objects.values("id", "sku", "legacy_code")


def find_by_legacy(code: str):
    # ORM lookup with a double underscore
    return Product.objects.filter(legacy_code__startswith=code)


def label_for(product: Product) -> str:
    # plain attribute access
    return f"{product.sku} / {product.legacy_code}"


def create_stub(sku: str):
    # keyword argument
    return Product(sku=sku, legacy_code="")


# A comment mentioning legacy_code must not count as a reference.
