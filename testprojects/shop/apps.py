from django.apps import AppConfig


class ShopConfig(AppConfig):
    name = "shop"

    def ready(self):
        # The line that makes the signals invisible: they are connected here,
        # far from anything that calls save().
        from . import signals  # noqa: F401
