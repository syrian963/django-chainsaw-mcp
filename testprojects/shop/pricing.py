"""Money handled every way people handle it, right and wrong.

Product.price and Invoice.amount_due are DecimalFields. The difference
between the correct and the incorrect lines here is a fraction of a cent,
which is exactly why they survive review.
"""

from decimal import ROUND_HALF_UP, Decimal

from .models import Invoice, Product

VAT = Decimal("0.19")


def wrong_from_birth():
    """0.19 has no exact binary form, so this is not 0.19."""
    return Decimal(0.19)


def harmless_but_worth_tidying():
    """0.5 is exactly representable, so nothing is lost. Low, not high."""
    return Decimal(0.5)


def one_way_door(product):
    """float() on a DecimalField: everything after this is approximate."""
    return float(product.price) * 1.19


def bankers_rounding(invoice):
    """Exact, but round() gives 0.12 for 0.125 where an invoice wants 0.13."""
    return round(invoice.amount_due, 2)


def laundered(value):
    """Throw the precision away and then ask for it back."""
    return Decimal(float(value))


def correct_gross(product):
    """Nothing here should be reported."""
    return (product.price * (Decimal("1") + VAT)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def not_money(order):
    """`quantity` is not a DecimalField anywhere, so this is left alone."""
    return float(order.quantity)
