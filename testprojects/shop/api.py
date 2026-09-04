"""Views written both ways, so the tenant analysis has real cases.

The tenant root here is shop.Customer. Order belongs to it directly, OrderLine
and Invoice through Order.
"""

from .models import Invoice, Order, OrderLine, Product


# --- unscoped: the IDOR shape -------------------------------------------------

def order_detail(request, pk):
    # Loads by primary key alone. Any authenticated user can read any order.
    return Order.objects.get(pk=pk)


def order_list(request):
    # Returns every order in the system.
    return Order.objects.all()


def line_detail(request, pk):
    # Two hops from the owner: OrderLine -> order -> customer.
    return OrderLine.objects.filter(pk=pk).first()


def invoice_by_number(request, number):
    # Filters on something real, but nothing that ties the row to a customer.
    return Invoice.objects.get(number=number)


# --- scoped: the same reads, done properly ------------------------------------

def my_order_detail(request, pk):
    return Order.objects.filter(customer=request.user.customer).get(pk=pk)


def my_lines(request):
    return OrderLine.objects.filter(order__customer=request.user.customer)


def my_invoices(request):
    return Invoice.objects.filter(order__customer=request.user.customer)


# --- not tenant data at all ---------------------------------------------------

def catalogue(request):
    # Product does not belong to a customer, so an unfiltered read is correct.
    return Product.objects.all()
