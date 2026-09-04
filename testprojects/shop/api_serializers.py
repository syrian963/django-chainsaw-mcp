"""Serializers written three ways, so the exposure check has real cases."""

from rest_framework import serializers

from .models import Customer, Invoice, Order, Product


class CustomerSerializer(serializers.ModelSerializer):
    """The one that goes wrong quietly: every field, forever."""

    class Meta:
        model = Customer
        fields = "__all__"


class ProductSerializer(serializers.ModelSerializer):
    """exclude names what must not go out, so anything added later goes out."""

    class Meta:
        model = Product
        exclude = ["id"]


class OrderSerializer(serializers.ModelSerializer):
    """Explicit and clean, which is the shape to aim for."""

    class Meta:
        model = Order
        fields = ["id", "placed_at"]


class InvoiceSerializer(serializers.ModelSerializer):
    """Explicit, but names a field that looks like it should stay internal."""

    class Meta:
        model = Invoice
        fields = ["id", "number", "internal_note"]
