# `fastapi_exposure`

## The bug that is an absent line

```python
@app.get("/users/{pk}")
async def get_user(pk: int):
    return session.get(User, pk)
```

No `response_model`, no return annotation. FastAPI serialises whatever it is
handed — the whole ORM object, every column: `password_hash`, `reset_token`,
`is_superuser`, the internal notes.

This is the FastAPI shape of `fields = "__all__"`, and it is worse in one way.
A Django serializer at least lists what it exposes *somewhere*. Here **the
absence of one line is the entire bug**, so there is nothing in the file to
read and nothing to review. And it gets worse on its own: the next column added
to the model joins the response without anybody touching this endpoint.

Every FastAPI guide says to set `response_model`. Several say a CI rule should
enforce it. No linter ships one.

## What it looks like

```
14 route(s), 2 model(s)

  CRITICAL  GET /users/{pk}  -> leaky_user()  [no authentication]
            app/routes.py:22
            no response_model and no return annotation, so FastAPI serialises
            whatever the function returns, in full. When that is an ORM object
            it is every column including the ones added next month; when it is
            an upstream response it is whatever that service sent, and nothing
            here establishes who is asking
            fix: declare a response_model, or annotate the return type

  CRITICAL  GET /admin/users/{pk}  -> admin_user()  [no authentication]
            UserAdminOut declares password_hash (credential), reset_token
            (credential), is_superuser (privilege flag)

  HIGH      GET /me  -> me()  [authenticated]
```

## Severity is about who can reach it

| | |
| --- | --- |
| **critical** | unbounded or sensitive response, and nothing establishes who is asking |
| **high** | the same, behind a dependency — it still leaks to everyone who can log in |
| **medium** | the `response_model` is computed, so what leaves cannot be read here |

Authentication is judged from `Depends(...)` and `Security(...)` in the
signature and `dependencies=[...]` on the route, matched **loosely by name**:
`Depends(get_db)` supplies a session and is not authentication, while
`Depends(get_current_user)` and `Depends(require_admin)` are.

## What is deliberately silent

- **`response_model=UserOut`** — bounded, and narrow.
- **`-> UserOut`** — FastAPI has used the return annotation as the response
  model since 0.89, so it is exactly as good.
- **`return {"ok": True}`** — a dict or a literal is something the author
  wrote out. The risk here is returning an object somebody else's code built.

## Nothing is imported

A FastAPI app usually wants a database URL, a settings object and a secret
before it will import at all, and none of that is needed to read a decorator.
So this check parses the source and never imports the project — which is also
why it runs with no Django configured and no application dependencies
installed:

```bash
DJANGO_CHAINSAW_PROJECT_PATH=/path/to/api django-chainsaw routes
```

The cost is that a `response_model` computed at runtime cannot be read. That is
reported as `unreadable_response_model` at medium, so a clean result is not
mistaken for a complete one.

## What it cannot see

- **A response shaped after the return.** Middleware, a custom
  `APIRoute.get_route_handler`, or a `Response` built by hand.
- **`response_model_exclude`** and its relatives, which narrow a declared model
  at the route. A model reported as carrying a sensitive field may already
  exclude it there.
- **Which fields the ORM object actually has.** The unbounded case is reported
  on the shape of the return, not on a resolved list of columns — resolving
  that would need the project imported, which is the thing this avoids.
- **A router mounted under a prefix.** Paths are reported as written on the
  decorator, so the full URL may have a prefix in front of it.
- **Whether a sensitive-looking name is sensitive.** A field called `token`
  might be a public share link. The name list is the one
  `serializer_exposure` uses, shared deliberately so both frameworks get the
  same judgement.
