"""A serializer nothing imports at startup, which is the dangerous kind.

No URLconf reaches this module, no `models.py` or `admin.py` pulls it in. Only
a management command uses it. Django therefore never imports it, so a walk over
`ModelSerializer.__subclasses__()` cannot see it, and the exposure check
reported that this project has no such problem.

It exposes a password reset token.
"""

from rest_framework import serializers

from .models import Customer


class CustomerExportSerializer(serializers.ModelSerializer):
    """Every field, including the one that lets you take over an account."""

    class Meta:
        model = Customer
        fields = "__all__"
