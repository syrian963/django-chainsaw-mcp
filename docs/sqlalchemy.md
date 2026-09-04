# `sqlalchemy_nplusone`

## One query, then one more per row

```python
orders = db.query(Order).all()          # one query
for order in orders:
    print(order.customer.name)          # one more, per order
```

SQLAlchemy fires a query the first time an unloaded relationship is touched.
On its own that is a feature. Inside a loop it is the N+1, and the loop and the
query are usually far enough apart that neither line looks wrong on its own.

## The shape with no loop to see

```python
@app.get("/orders", response_model=list[OrderOut])
def list_orders(db=Depends(get_db)):
    return db.query(Order).all()
```

`OrderOut` declares `items: list[ItemOut]`, so **serialisation** walks the
relationship — once per order, *after the endpoint has returned*. Nothing in
the function body touches `items` at all. There is no loop to notice, no
attribute access to grep for, and the profiler blames the framework.

This is the same defect as the Django `serializer_nplusone` case, in a
different framework, and it is the one that survives review.

## What exists already

| | |
| --- | --- |
| `nplusone` | watches lazy loads as they happen — runtime, covers what the tests exercise |
| `lazy="raise"` | turns the mistake into an exception — the right fix, still runtime |
| query-count tests | the documentation's own advice |

Nothing reads it out of the source, and both halves are there: the query says
what it eagerly loaded, the model says which attributes are relationships, and
the loop or the response model says which ones get touched.

## What it looks like

```
5 relationship(s) across 2 model(s)

1 in a loop:

  HIGH   app/orders.py:29  in report()
         print(order.customer.name)
         Order.customer is a relationship and the query on line 27 did not
         load it, so touching it inside the loop fires one query per row
         fix: .options(selectinload(Order.customer)) on that query

1 during response serialisation:

  HIGH   app/orders.py:58  in list_orders()
         OrderOut -> Order.items
         OrderOut declares 'items', so serialising the response walks
         Order.items once per row - after this function has returned, which
         is why nothing in the body touches it
         fix: .options(selectinload(Order.items)) on the query this returns
```

## What is never reported

- **`lazy="selectin"`, `"joined"`, `"subquery"`, `"immediate"`** on the
  relationship. It is loaded up front every time, so it cannot be an N+1.
- **`lazy="raise"`.** The mistake is already an exception at runtime, which is
  the recommended fix. Reporting it would be telling somebody to fix the thing
  they fixed.
- **A query that loaded what the loop touches**, via `selectinload`,
  `joinedload`, `subqueryload`, `immediateload` or `contains_eager` — including
  through a chain like `selectinload(Order.items).selectinload(Item.product)`.
- **A response model declaring no relationship at all.**

## Nothing is imported

The same reason as the rest of the FastAPI support: a project that wants a
database URL before it will import is not a project this can boot. Models,
relationships, `lazy=` and loader options are all read from the AST, so it runs
on a checkout with nothing installed:

```bash
DJANGO_CHAINSAW_PROJECT_PATH=/path/to/api django-chainsaw sqla
```

## What it cannot see

- **A variable that crosses a function boundary.** The query and the access
  have to be in the same function. A helper that takes a list of already-loaded
  rows and walks a relationship is invisible.
- **A query whose model is not a literal** — `db.query(model_class)` where the
  class came from a variable or a registry.
- **`Query.options` applied later**, to a variable, rather than chained onto
  the query being assigned.
- **Whether the relationship is small.** A one-to-one crossed per row is still
  N+1 queries and may still be nothing worth changing; the check counts
  queries, never rows.
- **`joinedload` set as a default via `relationship(lazy="joined")` on the
  *other* side** of the relationship, which is unusual but legal.
