"""Views with no transaction in sight, which is the ATOMIC_REQUESTS trap.

With ATOMIC_REQUESTS on, every one of these runs inside a transaction. There is
no `with` block, no decorator, and nothing at the call site to suggest it.
"""

import requests
from django.core.mail import send_mail
from django.http import JsonResponse
from rest_framework import viewsets

from .models import Order


def confirm(request, order_id):
    """A function view. Django wraps it; the source says nothing."""
    send_mail("Confirmed", str(order_id), "shop@example.com", ["a@example.com"])
    return JsonResponse({"ok": True})


def helper(order_id):
    """Not a view: no request argument. Must not be reported."""
    send_mail("Internal", str(order_id), "shop@example.com", ["ops@example.com"])


class OrderActionViewSet(viewsets.ViewSet):
    def create(self, request):
        requests.post("https://warehouse.example/orders", json={})
        return JsonResponse({"ok": True})

    def _internal(self, order_id):
        """Not a handler, so not wrapped as one. Must not be reported."""
        send_mail("Internal", str(order_id), "shop@example.com", ["ops@example.com"])
