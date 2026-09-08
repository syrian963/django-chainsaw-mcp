# `blocking_in_async`

## One synchronous call, and the whole server stops

FastAPI runs an `async def` endpoint **on the event loop itself**. A `def`
endpoint it hands to a threadpool. So this:

```python
@app.get("/users/{pk}")
async def get_user(pk: int):
    return session.query(User).get(pk)      # blocking driver
```

does not serve one slow request. It stops **every** request in the process for
as long as the query takes, because nothing else can run while the loop is
blocked.

At one request a second in development it is invisible. At two hundred a second
it is an outage — and the traceback points at whichever unlucky endpoint timed
out, never at this line.

The maddening part is that deleting one word fixes it. A `def` endpoint runs in
a threadpool where blocking is fine.

## What ruff covers, and what it does not

`ruff`'s `ASYNC` rules (from flake8-async) catch a fixed list inside an async
function: `open`, `time.sleep`, `subprocess`, `os.popen`. Real, and the easy
half.

They do not catch the common one — a **synchronous database call** — and they
do not follow a call:

```python
@app.get("/reports/{pk}")
async def get_report(pk: int):
    return build_report(pk)                  # nothing here looks blocking

def build_report(pk):
    return session.query(Order).all()        # and here it is
```

Neither file is suspicious on its own. The call graph already knows the edge,
so the endpoint is reported **with the path that reaches it**:

```
HIGH   app/services.py:13  [database]  session.query.filter_by.all
       reached from async get_report()
       path:  app.main.get_report -> app.services.build_report
       `async def get_report` reaches a synchronous call 1 step(s) away.
       Nothing at the call site looks blocking, and the loop stops all the same
```

## No Django required

This is the first check that needs no Django at all. Point
`DJANGO_CHAINSAW_PROJECT_PATH` at a FastAPI or Starlette project and leave
`DJANGO_CHAINSAW_SETTINGS_MODULE` unset:

```bash
DJANGO_CHAINSAW_PROJECT_PATH=/path/to/api django-chainsaw async
django-chainsaw profile          # what the project is built on
```

`profile` counts frameworks by how many of the **project's own files** import
them, not by what is installed — a package sitting in the virtualenv that
nothing imports is a fact about the environment, not the code.

## What is deliberately silent

**A `def` endpoint.** FastAPI runs it in a threadpool precisely so blocking is
safe there. Telling somebody to make a working synchronous endpoint async is
the advice that causes the outage.

**Anything awaited.** Awaiting is what yields, so an `await client.get(url)` on
an async client is correct and reported as nothing.

**A method that only looks like a query.** `values.count(1)` on a list is not a
database call.

## How a database call is recognised

A terminal method — `all`, `first`, `get`, `execute`, `commit`, `fetchall` and
the rest — on a receiver named like a session, a manager or a cursor:
`session`, `db`, `objects`, `query`, `cursor`, `conn`, `engine`, `queryset`.

The receiver chain is read **through intermediate calls**, which matters more
than it sounds: `session.query(User).get(pk)` has a call in the middle, and a
plain dotted-name walk returns nothing for it — which is how the single
commonest form of this bug stayed invisible in the first version.

The trade is stated: a session bound to an unusual name is missed, and a
variable that merely shares a name with one is reported.

## In CI

```bash
django-chainsaw async --fail-on-findings
```

Exit 1 if anything blocking runs on the loop. `--no-follow` reports only what
is written directly in an async function; `--max-depth` bounds how far a call
is followed (3 by default).


## A decorator that moves the body to a thread ends the walk

`@sync_to_async` and `@database_sync_to_async` exist to take a synchronous
body off the event loop, so a call into one of them is awaited and blocks
nothing. The walk stops there: the function is neither a finding nor a route
to one.

This is not a corner case. It is the standard answer in the whole channels
ecosystem, and getting it wrong made the check wrong about the correct way to
write the code it exists to check. On channels itself, `channels/auth.py` puts
the decorator on every function that touches the session, and following the
call graph through it produced **25 findings out of 25** — every one of them
the idiom rather than the defect. After the fix that project reports nothing,
which is the right answer for the reference implementation of doing this
properly.

Both the bare and the called forms are recognised, dotted or not:
`@database_sync_to_async`, `@sync_to_async(thread_sensitive=True)`,
`@asgiref.sync.sync_to_async`.

## `settings_dict` is configuration, not a round trip

The ORM receivers are matched anywhere in a call chain, which is what lets
`session.query(User).all()` be recognised through the call in the middle. It
also meant `db.connections["default"].settings_dict.get("NAME")` read as a
database receiver: `db` and `connections` are both in the list, and `get` is a
terminal. That is a dictionary of settings being read, and channels' own test
suite does it twice.

## What it cannot see

- **A blocking call in a third-party library.** Only the project's own
  functions are followed; a library that blocks internally looks like any
  other call.
- **Whether the call is actually slow.** A cached lookup and a full table scan
  are reported the same. The event loop does not care either, but you might.
- **Dispatch it cannot resolve** — a callable stored on an object, a handler
  looked up in a dict. The call graph reports its resolution rate for exactly
  this reason.
- **A blocking call assigned to a variable and invoked later.** The call and
  the assignment have to be visible together.

`asyncio.to_thread(...)` and `loop.run_in_executor(...)` are **not** false
positives, which is worth stating because the first version of this page
claimed they were. Both are awaited, and awaiting is exactly what makes a
call safe — so they fall under the rule above rather than being a special
case. The fixture covers both.
