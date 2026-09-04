"""A declarative class-based view, the shape scan_templates can resolve."""

from django.views.generic import ListView

from .models import Order


class OrderListView(ListView):
    model = Order
    template_name = "shop/order_list.html"
    context_object_name = "orders"
