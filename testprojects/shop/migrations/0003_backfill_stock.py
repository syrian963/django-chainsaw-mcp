"""Three data migrations: one with no way back, one with noop, one real."""

from django.db import migrations


def fill_stock(apps, schema_editor):
    apps.get_model("shop", "Product").objects.filter(stock=0).update(stock=1)


def unfill_stock(apps, schema_editor):
    apps.get_model("shop", "Product").objects.filter(stock=1).update(stock=0)


def tag_orders(apps, schema_editor):
    apps.get_model("shop", "Order").objects.update(status="placed")


class Migration(migrations.Migration):
    dependencies = [("shop", "0002_remove_product_legacy_code")]

    operations = [
        # No reverse at all: migrating backwards past this raises.
        migrations.RunPython(tag_orders),
        # A real reverse.
        migrations.RunPython(fill_stock, unfill_stock),
        # noop is a decision, and it reads as one.
        migrations.RunPython(fill_stock, migrations.RunPython.noop),
        # Raw SQL with no reverse_sql.
        migrations.RunSQL("UPDATE shop_product SET stock = stock + 0;"),
    ]
