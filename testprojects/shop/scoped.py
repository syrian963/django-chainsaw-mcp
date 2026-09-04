"""Two ways a queryset is already scoped that reading one line cannot show.

Both of these look exactly like an unscoped query at the point the analysis
sees them, and both are correct code. Reporting them is how an authorisation
check earns a reputation for crying wolf and gets switched off.
"""

from rest_framework import viewsets

from .models import Invoice, Order, ScopedNote


class TenantScopedViewSet(viewsets.ModelViewSet):
    """The mixin every subclass relies on and none of them mention."""

    def get_queryset(self):
        return super().get_queryset().filter(customer=self.request.user.customer)


class OrderViewSet(TenantScopedViewSet):
    # Looks unscoped. Is not: the base class narrows it on every request.
    queryset = Order.objects.all()


class LeakyOrderViewSet(viewsets.ModelViewSet):
    # The same line, without the mixin. This one really is unscoped.
    queryset = Order.objects.all()


def invoices_for_everyone():
    """No class, no mixin, no manager. Still a leak."""
    return Invoice.objects.all()


def notes_look_unscoped():
    """Identical shape to the leak above, and already narrowed by the manager."""
    return ScopedNote.objects.all()


def narrowed_further_down(request):
    """The first line reads as a leak. The third line is the whole story."""
    orders = Order.objects.all()
    if not request.user.is_staff:
        orders = orders.filter(customer=request.user.customer)
    return orders


def guarded_after_fetch(request, pk):
    """Loading by pk and then asking a permission class is legitimate."""
    order = Order.objects.get(pk=pk)
    request.user.has_perm("shop.view_order", order)
    return order
