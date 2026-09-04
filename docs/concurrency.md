# `race_conditions`

## Three lines and a window

```python
product = Product.objects.get(pk=pk)
product.stock -= quantity
product.save()
```

Two requests read `stock = 10`. Both subtract 3. Both write 7. One sale is
gone, and nothing raised.

A transaction does not help here — neither request sees the other's
uncommitted write, so both still read 10. Under load this is not a rare event;
it is the normal one, because the requests that collide are exactly the ones for
the product everybody is buying at once. Counters, balances, stock levels, retry
counts: the fields where being off by one costs money are the fields this
happens to.

The fix is to let the database do the arithmetic, or to hold the row:

```python
Product.objects.filter(pk=pk).update(stock=F("stock") - quantity)

with transaction.atomic():
    product = Product.objects.select_for_update().get(pk=pk)
    product.stock -= quantity
    product.save()
```

Both are **silent** here. So is `product.stock = F("stock") - quantity` on the
instance, and so is a plain assignment (`product.price = new_price`), which is
an overwrite, not a read-modify-write.

## What it looks like

```
3 read-modify-save race(s)

  shop/stock.py:17  product.stock in shop.stock.reserve
         product.stock -= quantity
         saved at line 18; two requests read the same value, both change it in
         Python, both write their own result; one change is lost
         fix: Product.objects.filter(pk=product.pk).update(stock=F("stock") <op> delta)

  shop/stock.py:30  order.retries in shop.stock.bump_retries (medium confidence: parameter)
         order.retries += 1
```

## What has to be true before it says anything

All three parts, on the **same object**, in the **same function**:

1. where the instance came from — a fetch (`get`, `first`, `get_or_create`,
   `get_object_or_404`…) or a parameter
2. an in-Python change to one of its fields — `-=`, `+=`, or the long-hand
   `x.f = x.f - n` — that does not use `F()`
3. a `save()` on it **after** the change

An instance passed in as a parameter is **medium confidence**: the caller may
hold the lock, or the object may be unsaved. `--no-parameters` drops that tier.

## `select_for_update()` with nothing to hold the lock in

```python
def lock_without_transaction(pk):
    return Product.objects.select_for_update().get(pk=pk)
```

Outside `atomic()` there is no transaction for the lock to live in, and Django
raises `TransactionManagementError` when the queryset is evaluated. That is a
crash, not a race, found by the first request that reaches the line.

Whether a transaction is open is exactly what the call graph already knows. A
`with atomic()` block, a `@transaction.atomic` decorator, a caller in another
module that opened one, and the implicit transaction `ATOMIC_REQUESTS` puts
around every view **all count**, so none of those produce a finding.

## `get_or_create()` on fields nothing makes unique

```python
Tag.objects.get_or_create(name=label)
```

Two requests miss the `get` at the same moment. Both `create`. There are now
two tags called `label`, and the next `get_or_create` raises
`MultipleObjectsReturned` — which the method does not catch, because it only
catches `DoesNotExist`.

Django's documentation says this in so many words: the method is safe only when
the lookup fields carry a database uniqueness constraint. The tickets about it
(#12579, #29499) are years old and will stay open, because the database is the
only thing that can enforce it. So the check reads the model:

```
2 get_or_create/update_or_create on a lookup nothing makes unique

  shop/upserts.py:12  shop.Tag.get_or_create(name=...)
         unique on Tag: slug
         fix: add a UniqueConstraint(fields=['name']) to Tag.Meta and a migration;
              or look up by a field that is already unique
```

A lookup is covered by a unique field, a `unique_together`, or an
**unconditional** `UniqueConstraint` — a conditional one only holds where its
condition is true, and whether the lookup satisfies it is not decidable here.
A superset of a unique set is covered too: if `sku` is unique, `sku, name`
matches at most one row. `defaults=` is not a lookup. A lookup through a
relation (`customer__email`) is another model's business and is not judged.

## In CI

```bash
django-chainsaw races --fail-on-findings
```

Exit 1 on any high-confidence race, any unprotected lock, or any unsafe upsert. Also in the
aggregate `check` as `races`.

## What it cannot see

- **A lock held some other way.** An advisory lock, a distributed lock, a
  Celery task with concurrency 1 — a read-modify-save under any of those is
  safe and looks identical.
- **The same object mutated across functions.** Fetched in one, changed in
  another, saved in a third. The three parts have to be visible together.
- **Whether the field actually matters.** `product.view_count += 1` losing one
  increment is a rounding error; `account.balance -= amount` losing one is
  money. Both are reported the same; the severity is a decision.
- **`update_fields` narrowing.** `save(update_fields=["stock"])` still writes
  the Python value, so it is still a race and is reported. It is mentioned
  because people reach for it believing it helps.
