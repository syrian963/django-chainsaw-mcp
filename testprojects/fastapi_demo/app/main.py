"""Endpoints written both ways, only one of which stops the server."""

import httpx
from fastapi import FastAPI
from sqlalchemy.orm import Session

from .services import build_report, notify_partner, wait_a_moment

app = FastAPI()
session: Session = None  # type: ignore[assignment]


@app.get("/users/{pk}")
async def get_user(pk: int):
    """The classic: a blocking driver call straight on the loop."""
    return session.query("User").get(pk)


@app.get("/reports/{pk}")
async def get_report(pk: int):
    """Nothing here looks blocking. build_report does the damage."""
    return build_report(pk)


@app.post("/orders/{pk}/notify")
async def notify(pk: int):
    """Two hops: notify_partner uses requests."""
    notify_partner(pk)
    wait_a_moment()
    return {"ok": True}


@app.get("/sync-users/{pk}")
def get_user_sync(pk: int):
    """A `def` endpoint runs in a threadpool. Blocking here is fine, and
    telling somebody to make this async would cause the outage."""
    return session.query("User").get(pk)


@app.get("/proxy")
async def proxy(url: str):
    """Correct: an async client, awaited."""
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
    return response.json()


@app.get("/counts")
async def counts(values: list[int]):
    """`.count()` on a list is not a database call and must stay silent."""
    return {"ones": values.count(1)}


@app.get("/offloaded/{pk}")
async def offloaded(pk: int):
    """Correct: the blocking call is moved to a thread and awaited."""
    import asyncio

    return await asyncio.to_thread(lambda: session.query("User").get(pk))


@app.get("/executor/{pk}")
async def via_executor(pk: int):
    """Also correct, the older spelling."""
    import asyncio

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, build_report, pk)
