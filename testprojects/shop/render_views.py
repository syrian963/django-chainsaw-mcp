"""Function views, which is how most Django views are actually written.

None of these was resolvable before: the class-based reader only understands
`template_name` plus `model`, and these declare neither.
"""

from django.shortcuts import render
from django.template.response import TemplateResponse

from .models import Customer, Order


def order_list(request):
    """The plain form: a literal template name and a literal context dict."""
    orders = Order.objects.all()
    return render(request, "shop/render_order_list.html", {"orders": orders})


def order_list_inline(request):
    """The queryset built inside the dict."""
    return render(request, "shop/render_order_inline.html", {"orders": Order.objects.all()})


def order_list_via_variable(request):
    """A context dict assembled first, then handed over."""
    context = {"orders": Order.objects.filter(placed_at__isnull=False)}
    return render(request, "shop/render_order_variable.html", context)


def customer_detail(request, pk):
    """TemplateResponse, and a single object rather than a list."""
    customer = Customer.objects.get(pk=pk)
    return TemplateResponse(request, "shop/render_customer.html", {"customer": customer})


def built_at_runtime(request, name):
    """The template name is computed, so nothing here is resolvable."""
    return render(request, f"shop/{name}.html", {"orders": Order.objects.all()})
