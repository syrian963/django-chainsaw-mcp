"""One example of every call form the graph claims to resolve.

A call graph that silently fails to resolve one of these makes every check
built on top of it quietly incomplete, which is worse than not having it. So
each form gets a case and the suite asserts on it by name.
"""

from django.db import transaction

from . import fulfilment                    # module import
from .fulfilment import finalise            # direct name import


@transaction.atomic
def form_direct_name(pk):
    finalise(pk)


@transaction.atomic
def form_module_attribute(pk):
    fulfilment.finalise(pk)


class Base:
    def inherited_hop(self, pk):
        finalise(pk)


class Service(Base):
    @transaction.atomic
    def form_self_call(self, pk):
        self.own_hop(pk)

    def own_hop(self, pk):
        finalise(pk)

    @transaction.atomic
    def form_inherited_call(self, pk):
        self.inherited_hop(pk)
