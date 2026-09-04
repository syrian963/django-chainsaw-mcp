"""A view, a service and a repository: three files, one request.

The defect is in the repository. Nothing about the repository says it is on the
path of an HTTP request, and nothing about the view says it queries. Only the
chain between them does, which is what `impact` exists to walk.
"""

from rest_framework import serializers, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

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


def daily_report(request):
    """A plain function view: no decorator, no class, no `serializer_class`.

    Nothing about this function says it serves HTTP. Only the URLconf knows,
    which is why the defect below is invisible to a check that reads only the
    source.
    """
    return Response({"customers": [c.pk for c in decorate(Order.objects.all())]})


class ManualOrderSerializer(serializers.ModelSerializer):
    """Served by a view that never declares it, which is the common shape.

    `serializer_class` is DRF's declarative route and not the one a plain
    APIView takes. This one is built in the method body, so there is no
    attribute for anything to read - only the name in the code.
    """

    class Meta:
        model = Order
        fields = ["id", "customer", "lines"]


class ManualReportView(APIView):
    def get(self, request):
        orders = Order.objects.all()
        return Response(ManualOrderSerializer(orders, many=True).data)
