from django.apps import AppConfig


class ShopConfig(AppConfig):
    name = "shop"

    def ready(self):
        # The line that makes the signals invisible: they are connected here,
        # far from anything that calls save().
        from . import signals  # noqa: F401

        # Serializers must be imported for the subclasses to exist.
        try:
            from . import api_serializers  # noqa: F401
            from . import viewsets  # noqa: F401
        except ModuleNotFoundError:
            pass  # DRF not installed in this environment
