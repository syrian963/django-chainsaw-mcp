"""Two hops from the transaction boundary, and nothing here mentions one."""

import requests
from django.core.mail import send_mail


def finalise(order_id):
    """Called from inside a transaction by checkout(), which this cannot know."""
    _tell_warehouse(order_id)
    send_mail("Order", str(order_id), "shop@example.com", ["a@example.com"])


def _tell_warehouse(order_id):
    """Three hops out. Still inside the caller's transaction."""
    requests.post("https://warehouse.example/orders", json={"id": order_id})


def notify_only(customer_id):
    """Reached only from browse(), which opens no transaction."""
    send_mail("Hello", str(customer_id), "shop@example.com", ["a@example.com"])


def notify(user_id, template):
    """A project's own dispatch wrapper. Nothing here is named `.delay`."""
    from .notifications import send_confirmation

    send_confirmation.delay(user_id, template)
