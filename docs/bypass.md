# `bypassed_effects`

## True for `save()`, false for the line below it

`what_happens_on shop.Order` says: saving an Order creates an Invoice, which
queues a task and clears a cache. Every word of that is true for
`order.save()`, and every word is false for these:

```python
Order.objects.bulk_create(orders)
Order.objects.filter(status="draft").update(status="placed")
Order.objects.bulk_update(orders, ["status"])
```

`bulk_create`, `bulk_update` and `QuerySet.update` go straight to SQL. They do
not call `save()`, so an overridden `save()` never runs. They do not send
`pre_save` or `post_save`, so no receiver fires. Django documents this — in the
reference for each method, one sentence each. Nothing at the call site says it,
and nothing connects that call site to the list of effects that just did not
happen.

## What it looks like

```
3 bulk write(s) that skip a save() chain

  HIGH   shop/bulk_writes.py:13  shop.Order via .bulk_create()
         return Order.objects.bulk_create([Order(**row) for row in rows])
         no save(), no pre_save, no post_save
         receivers skipped: shop.signals.create_invoice_for_order,
                            shop.signals.announce_invoice
         never written    : shop.Invoice

  HIGH   shop/bulk_writes.py:23  shop.Shipment via .bulk_update()
         return Shipment.objects.bulk_update(shipments, ["tracking"])
         save() not run   : shop.models.AuditedMixin.save()
         receivers skipped: shop.signals.touch_audit, shop.signals.audit_twice (x2)
         never written    : shop.AuditEntry
```

The finding is **not** "bulk_create bypasses signals", which everybody knows in
the abstract and nobody thinks about at the call site. It is *this call, on this
model, skips these four named effects*. That is the difference between a lecture
and a bug report.

Look at `announce_invoice` in the first finding. It is a receiver on **Invoice**,
one hop down the chain. It never fires here because the Invoice it would have
announced was never created. The skipped chain is followed transitively, because
the second-order effect is usually the one that matters — the cache that is
never invalidated, the search index that quietly drifts.

## What is deliberately not reported

**A bulk write on a model with nothing to skip.** `Product.objects.bulk_create`
on a model with no receivers and no `save()` override is just fast. Reporting it
is how a check earns an ignore rule within a month, so `bulk_writes_seen` counts
it and `findings` does not.

**`QuerySet.delete()`.** Django collects the objects and sends `pre_delete` and
`post_delete` for each one, so that chain **does** fire. Listing it would be a
wrong finding dressed up as thoroughness.

**A bulk write on a queryset held in a variable.**

```python
def archive(queryset):
    return queryset.update(archived=True)
```

This is a real bypass on an unknown model. The model, and therefore the effects,
cannot be read from the line, so it is counted under
`bulk_writes_on_unresolved_model` and not reported — inventing a model would be
worse than the gap.

## Severity

**high** when the skipped chain writes another model or produces a side effect
(a task, an email, a cache write) — something is now missing or stale.
**medium** when the chain only touches the instance itself.

## In CI

```bash
django-chainsaw bypass --fail-on-findings
```

Exit 1 on any high-severity finding. It is also part of the aggregate `check`
under `bypass`. When a bypass is intentional — an import that deliberately
skips the audit trail — the honest fix is a comment at the call site, and a
suppression with a stated reason in `pyproject.toml`.

## What it cannot see

- **A bulk write reached through a variable**, as above. Counted, not reported.
- **Whether the skipped effect was wanted.** An import that deliberately skips
  the audit trail looks identical to one that forgot. The finding says what did
  not happen; whether that is a bug is a decision.
- **`raw()` and `cursor.execute()`**, which also bypass everything and are
  opaque here.
- **Receivers connected after startup**, the same limit as `what_happens_on`.
