"""Queries written the way they accumulate. Half of them are N+1."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import selectinload

from .models import Order

router = APIRouter()


def get_db():
    return None


class OrderOut(BaseModel):
    id: int
    items: list[int]


class OrderIdOnly(BaseModel):
    id: int


def report(db):
    """The classic: one query, then one more per row."""
    orders = db.query(Order).all()
    for order in orders:
        print(order.customer.name)
    return orders


def report_eager(db):
    """Told the query what it was about to need. Silent."""
    orders = db.query(Order).options(selectinload(Order.customer)).all()
    for order in orders:
        print(order.customer.name)
    return orders


def report_always_eager(db):
    """`tags` is lazy="selectin" on the model, so this is never an N+1."""
    orders = db.query(Order).all()
    for order in orders:
        print(order.tags)
    return orders


def report_raising(db):
    """`audit` is lazy="raise": the mistake is already an exception."""
    orders = db.query(Order).all()
    for order in orders:
        print(order.audit)
    return orders


@router.get("/orders", response_model=list[OrderOut])
def list_orders(db=Depends(get_db)):
    """No loop here at all. Serialising OrderOut walks `items` per row,
    after this function has returned."""
    return db.query(Order).all()


@router.get("/orders/ids", response_model=list[OrderIdOnly])
def list_order_ids(db=Depends(get_db)):
    """The response model declares no relationship. Nothing to load."""
    return db.query(Order).all()


@router.get("/orders/full", response_model=list[OrderOut])
def list_orders_eager(db=Depends(get_db)):
    """Declares the same model and loads it. Silent."""
    return db.query(Order).options(selectinload(Order.items)).all()
