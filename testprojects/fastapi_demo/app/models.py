"""SQLAlchemy models, with relationships loaded three different ways."""

from sqlalchemy import ForeignKey
from sqlalchemy.orm import DeclarativeBase, Mapped, relationship


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customer"

    id: Mapped[int]
    name: Mapped[str]


class Order(Base):
    __tablename__ = "order"

    id: Mapped[int]
    customer_id: Mapped[int] = ForeignKey("customer.id")

    # Lazy by default: touching it fires a query.
    customer = relationship("Customer")
    items = relationship("Item")

    # Always eager, so it is never an N+1 and must never be reported.
    tags = relationship("Tag", lazy="selectin")

    # Turns the mistake into an exception, which is the recommended fix.
    audit = relationship("Audit", lazy="raise")


class Item(Base):
    __tablename__ = "item"

    id: Mapped[int]
    order_id: Mapped[int] = ForeignKey("order.id")
    product = relationship("Product")


class Product(Base):
    __tablename__ = "product"
    id: Mapped[int]


class Tag(Base):
    __tablename__ = "tag"
    id: Mapped[int]


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int]
