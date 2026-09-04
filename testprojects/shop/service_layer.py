"""A view, a service and a repository: three files, one request.

The defect is in the repository. Nothing about the repository says it is on the
path of an HTTP request, and nothing about the view says it queries. Only the
chain between them does, which is what `impact` exists to walk.
"""

from rest_framework import viewsets
from rest_framework.response import Response

from .models import Customer, Order


def customer_for(order):
    """One query, and its caller runs it once per row."""
    return Customer.objects.get(pk=order.customer_id)


def decorate(orders):
    return [customer_for(order) for order in orders]


class OrderReportViewSet(viewsets.ViewSet):
    def list(self, request):
        return Response({"customers": [c.pk for c in decorate(Order.objects.all())]})

    def get_queryset(self):
        """Called by the framework on every request, by nothing in the project."""
        return Order.objects.filter(status=self.request.query_params.get("status"))
