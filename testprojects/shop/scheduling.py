"""Datetime handling, written both ways.

USE_TZ is True in this project, so everything in the first half is a defect
that will not show up until the clock moves.
"""

import datetime
from datetime import datetime as dt

from django.utils import timezone

from .models import Order


# --- naive, the ones that break on a DST boundary -----------------------------

def orders_today_wrong():
    # naive: compares a local wall clock against UTC in the database
    return Order.objects.filter(placed_at__gte=datetime.datetime.now())


def stamp_wrong():
    # naive and holding UTC, which looks correct and compares wrong
    return dt.utcnow()


def cutoff_wrong():
    # a wall clock that does not exist on the night the clock jumps forward
    return datetime.datetime(2026, 3, 29, 2, 30)


def from_epoch_wrong(value):
    # uses whatever zone the server happens to be in
    return datetime.datetime.fromtimestamp(value)


# --- correct ------------------------------------------------------------------

def orders_today_right():
    return Order.objects.filter(placed_at__gte=timezone.now())


def stamp_right():
    return timezone.now()


def from_epoch_right(value):
    return datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc)


def cutoff_right():
    return timezone.make_aware(datetime.datetime(2026, 3, 29, 2, 30))
