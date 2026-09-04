"""Endpoints with and without authentication, serving the same serializers.

The exposure check knows CustomerExportSerializer leaks a password reset
token. The permission check knows which views are open. Neither on its own is
a security finding; the intersection is.
"""

from rest_framework import permissions, viewsets

from .api_serializers import ProductSerializer
from .models import Customer, Product
from .reporting_serializers import CustomerExportSerializer


class PublicCustomerExport(viewsets.ReadOnlyModelViewSet):
    """Explicitly open, serving every Customer field. The one to catch."""

    permission_classes = [permissions.AllowAny]
    serializer_class = CustomerExportSerializer
    queryset = Customer.objects.all()


class StaffCustomerExport(viewsets.ReadOnlyModelViewSet):
    """Same serializer, behind authentication. Not a finding."""

    permission_classes = [permissions.IsAdminUser]
    serializer_class = CustomerExportSerializer
    queryset = Customer.objects.all()


class ImplicitlyOpenCustomers(viewsets.ReadOnlyModelViewSet):
    """No permission_classes at all: whatever the default is, and the default
    default is AllowAny. This is the shape most leaks actually have."""

    serializer_class = CustomerExportSerializer
    queryset = Customer.objects.all()


class PublicCatalogue(viewsets.ReadOnlyModelViewSet):
    """Open on purpose, exposing nothing sensitive. Not a finding."""

    permission_classes = [permissions.AllowAny]
    serializer_class = ProductSerializer
    queryset = Product.objects.all()


class DecidedAtRuntime(viewsets.ReadOnlyModelViewSet):
    """get_permissions() can return anything. Labelled, not judged."""

    serializer_class = CustomerExportSerializer
    queryset = Customer.objects.all()

    def get_permissions(self):
        if self.action == "list":
            return [permissions.IsAuthenticated()]
        return [permissions.AllowAny()]
