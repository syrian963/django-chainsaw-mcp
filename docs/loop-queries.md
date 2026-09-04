# `queries_in_loops`

## The N+1 nobody's framework caused

The template and serializer checks here find the N+1 a **framework** causes: a
relation crossed while rendering. This one finds the N+1 somebody wrote by
hand, which is where it lives in a codebase whose views build their responses
themselves instead of handing a queryset to a serializer.

On a large real project, 152 of the DRF views declare no `serializer_class` at
all — every cost check was silent about all of them.

## Three shapes, one appearance, three fixes

```python
for order in orders:
    customer = Customer.objects.get(pk=order.customer_id)
```

**Uses the loop variable.** One query per row. Fetch them all before the loop
and index by key, or let the ORM do it with `select_related` /
`prefetch_related` on the queryset being iterated.

```python
for order in orders:
    config = Config.objects.get(key="vat")
```

**Ignores the loop variable.** The same question, asked N times, for the same
answer. Move it above the loop. There is nothing clever to do here and nothing
to trade off — which is exactly why it is worth separating from the first case.

```python
for order in orders:
    order.status = "closed"
    order.save()
```

**A write per row.** N round trips, each in its own transaction unless
something wraps the loop. `bulk_update` fixes it and skips the signals, which
is its own decision — [`bypass.md`](bypass.md) is the check for that side.

## What it looks like

```
2 once per row:

  CRITICAL  shop/loops.py:35  (loop at line 34)
            Invoice.objects.filter(order_id=order.pk).first()
            this query uses the loop variable, so it runs once per row. The
            loop is nested, so it runs once per row of the outer loop as well
            fix: fetch them all before the loop and index by key, or let the
                 ORM do it with select_related / prefetch_related

1 the same query every iteration:

  MEDIUM    shop/loops.py:19  (loop at line 18)
            default = Category.objects.get(name="Uncategorised")
            this query does not use the loop variable, so it asks the same
            question every iteration and gets the same answer
            fix: move it above the loop
```

A nested loop is **critical** rather than high: the query runs once per row of
the inner loop, once per row of the outer one.

## Prior art, honestly

`django-check` does static N+1 detection for relation access inside a loop, as
an LSP and a CLI, and it is the closest existing tool. `nplusone` and the Django
debug toolbar find it at runtime, once the code path has actually run.

What is added here is the **separation**. "There is a query in this loop" is one
finding. "This query does not use the loop variable and belongs three lines
higher" is a different one, with a fix nobody has to think about, and it is
worth being told apart from the one that needs a real decision.

## What is deliberately silent

- **A loop over a literal list.** `for sku in ("a", "b")` is two queries, not N.
  Writes are still reported, because two writes is still two transactions.
- **`dict.get(k)` and `[1, 2].count(x)`.** A method name shared with the ORM is
  not an ORM call, and the receiver has to look like a manager, a known model,
  or an instance being saved.
- **Tests.** A query per row in a fixture is not a production problem.
- **A chained lookup, counted once.** `Invoice.objects.filter(...).first()` is
  two qualifying calls and one query; reporting both doubled every chained
  lookup in the first version.

## What it cannot see

- **A query inside a function the loop calls.** The loop and the query have to
  be in the same function. A helper called per row is invisible here — the call
  graph knows the edge, and joining the two is the obvious next step.
- **Whether the loop is short.** Three rows and three million rows produce the
  same finding, because the row count is a property of the data.
- **A queryset already evaluated.** Iterating a list that was fetched once, and
  iterating a lazy queryset, look identical.
- **`bulk_create` in a loop**, which is a different and worse shape, and is not
  currently separated from a plain write.
