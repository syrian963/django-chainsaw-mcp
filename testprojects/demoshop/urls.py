"""A URLconf, which is the only place some views are ever named.

`shop.views_plain.confirm` is a plain function view. It carries no decorator
and belongs to no class, so nothing in its source says it serves requests. The
URLconf is the one thing that knows, and a check that does not read it will
report every defect on that view's path as reached by nothing.
"""

from django.urls import path
from rest_framework.routers import DefaultRouter

from shop.service_layer import OrderReportViewSet, daily_report
from shop.views_plain import OrderActionViewSet, confirm

router = DefaultRouter()
router.register("reports", OrderReportViewSet, basename="report")
router.register("actions", OrderActionViewSet, basename="action")

urlpatterns = [
    path("orders/<int:order_id>/confirm/", confirm, name="confirm-order"),
    path("reports/daily/", daily_report, name="daily-report"),
    *router.urls,
]
