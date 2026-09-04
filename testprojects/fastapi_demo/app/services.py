"""Helpers an endpoint calls. Nothing here mentions the event loop."""

import time

import requests
from sqlalchemy.orm import Session

session: Session = None  # type: ignore[assignment]


def build_report(user_id):
    """Two steps away from the endpoint, and it blocks."""
    return session.query("Order").filter_by(user_id=user_id).all()


def notify_partner(order_id):
    """An outbound call with a synchronous client."""
    return requests.post("https://partner.example/orders", json={"id": order_id})


def wait_a_moment():
    time.sleep(0.2)


async def fetch_async(client, url):
    """Correct: awaits, so it yields."""
    return await client.get(url)
