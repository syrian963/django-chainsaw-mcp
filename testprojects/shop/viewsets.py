"""The same data, served two ways.

One view leaves the queryset alone and pays for it per object. The other names
what it needs. The estimator should put several hundred queries between them.
"""

from rest_framework import serializers, viewsets
from rest_framework.pagination import PageNumberPagination

from .models import Order, Product
from .api_serializers import ProductSerializer


class OrderLineSerializer(serializers.ModelSerializer):
    product = ProductSerializer()

    class Meta:
        model = Order.lines.rel.related_model
        fields = ["id", "quantity", "product"]


class OrderDetailSerializer(serializers.ModelSerializer):
    lines = OrderLineSerializer(many=True)

    class Meta:
        model = Order
        fields = ["id", "placed_at", "customer", "lines"]


class SlowOrderViewSet(viewsets.ReadOnlyModelViewSet):
    """No select_related, no prefetch_related. Every relation costs per row."""

    serializer_class = OrderDetailSerializer
    queryset = Order.objects.all()


class FastOrderViewSet(viewsets.ReadOnlyModelViewSet):
    """The same serializer, with the queryset told what it is about to need."""

    serializer_class = OrderDetailSerializer
    queryset = (
        Order.objects
        .select_related("customer")
        .prefetch_related("lines", "lines__product", "lines__product__category")
    )


class ProductViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ProductSerializer
    queryset = Product.objects.all()


class OrderSummarySerializer(serializers.ModelSerializer):
    """A method field that queries, which reads as free and is not."""

    line_count = serializers.SerializerMethodField()
    latest_line = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "placed_at", "line_count", "latest_line"]

    def get_line_count(self, obj):
        # Runs once per object in the page. Looks like an attribute access.
        return obj.lines.count()

    def get_latest_line(self, obj):
        return obj.lines.order_by("-id").first().id if obj.lines.exists() else None


class OrderSummaryViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = OrderSummarySerializer
    queryset = Order.objects.all()


class DeferredOrderViewSet(viewsets.ReadOnlyModelViewSet):
    """only() reads as an optimisation, and here it is the opposite.

    The serializer renders `placed_at`, and only() did not fetch it, so it is
    loaded one row at a time on access.
    """

    serializer_class = OrderSummarySerializer
    queryset = Order.objects.only("id")


class _TwentyPerPage(PageNumberPagination):
    page_size = 20


class PaginatedOrderViewSet(viewsets.ReadOnlyModelViewSet):
    """The only list endpoint here that says how many rows a page holds.

    Every other list view in this project has no pagination_class and the
    project sets no DEFAULT_PAGINATION_CLASS, so they return the whole table.
    """

    serializer_class = OrderDetailSerializer
    pagination_class = _TwentyPerPage
    queryset = Order.objects.select_related("customer").prefetch_related(
        "lines", "lines__product", "lines__product__category"
    )
