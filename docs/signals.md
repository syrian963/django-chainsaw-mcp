# `what_happens_on`

## The most invisible thing in a Django codebase

```python
line.save()
```

That line queues a Celery task. Nothing about it says so, because the task is
three hops away:

```
OrderLine.save()
  post_save  touch_order                on shop.OrderLine
      writes instance.order -> shop.Order
    post_save  create_invoice_for_order  on shop.Order
        writes Invoice.create() -> shop.Invoice
      post_save  announce_invoice        on shop.Invoice
          cache write: set()
          celery task: delay()
```

The receivers live in `signals.py`, connected in `AppConfig.ready()`, which is
read once when the app is set up and never again.

## What already exists, and what does not

Tools that **list** registered receivers exist and are good: `dj-signals-panel`,
the debug toolbar's signals panel, `django-signalmap`. They answer *what is
connected to what*.

None of them follow the chain. A list tells you `create_invoice_for_order` is
connected to `post_save` on `Order`. It does not tell you that the receiver
writes an `Invoice`, that writing an `Invoice` fires its own `post_save`, and
that the receiver there queues work. **The second hop is where the surprise
lives**, and the second hop is what this traces.

## How it works

**1. Resolve the receivers.** Read from the live signal registry after
`django.setup()`, so anything connected at import time is included, wherever it
was connected from.

**2. Read each receiver's body with the AST.** Model writes (`save`, `create`,
`delete`, `update`, `bulk_create`, …) and side effects (`delay`, `apply_async`,
`send_mail`, cache writes, outbound HTTP) are collected with line numbers.

**3. Resolve the write target through the model graph.** Two ways:

| In the receiver | Resolved by |
| --- | --- |
| `Invoice.objects.create(...)` | the class name |
| `instance.order.save()` | walking `order` on the sending model |

The second one matters more than it looks. `instance.something.save()` is an
ordinary thing to write, and without following it through the relations the
chain stops at the first hop. The model graph is what makes it resolvable.

**4. Follow the write into that model's own signals**, until nothing new is
written or `max_depth` is reached. A model already on the path is reported and
not expanded again, so a receiver that writes back to something upstream does
not loop.

## Arguments

| Argument | Default | Meaning |
| --- | --- | --- |
| `model_label` | required | `app_label.ModelName` |
| `event` | `save` | `save` or `delete` |
| `max_depth` | `4` | how far to follow writes into further signals |

```bash
django-chainsaw signals shop.OrderLine
django-chainsaw signals shop.Order --event delete
```

## The other half of `delete_impact`

`delete_impact` walks `on_delete` across the model graph and says so explicitly
about its own blind spot:

> does not run `pre_delete` / `post_delete` signals or custom `delete()`
> overrides, which can remove more than the graph implies.

This is that other half. Run both before deleting anything that matters:

```bash
django-chainsaw delete-impact shop.Customer     # what the schema removes
django-chainsaw signals shop.Customer --event delete   # what the code removes
```

## Not only signals

An overridden `save()` is not a signal. It is ordinary code, it runs on every
write, before any `post_save` receiver does, and it is very often where the
largest effect in the chain lives:

```python
class AuditedMixin(models.Model):
    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        AuditEntry.objects.create(what=self.__class__.__name__)

class Shipment(AuditedMixin):
    ...          # nothing in this class says an audit row gets written
```

The chain walks the MRO for `save`/`delete` defined anywhere above the model
and below `django.db.models.Model`, reads it the same way it reads a receiver,
and follows whatever it writes. `"nothing else happens"` is the one answer this
tool must never give wrongly.

## Connected twice

```
duplicate_receivers:
  shop.signals.audit_twice  connected 2 times
      everything it does happens 2 times per event
```

Django's `connect()` deduplicates on the **identity** of the receiver, so
connecting the same function object twice is a harmless no-op that never
appears. The one that bites is a module imported under two names — once as
`shop.signals`, once as `myproject.shop.signals` — which produces two distinct
function objects Django cannot tell apart. The symptom is a side effect
happening twice, which reads like a race and is not.

## What it cannot see

- **Which way a condition goes.** A write inside `if created:` is marked
  `conditional`, so the chain no longer overstates it, but whether that branch
  is taken on a given call is a runtime question.
- **Receivers connected at runtime**, after the app registry finished loading.
- **Dynamic dispatch**, `getattr(obj, name)(), and writes through variables the
  AST cannot resolve to a model. Those are still reported as writes, with
  `resolved_model: null`, so the gap is visible rather than silent.
- **A receiver connected after startup.** The registry is read once, after
  `django.setup()`. One connected inside a request, a test fixture or a
  conditional import is not in it.

`unreadable_receivers` lists any receiver whose source could not be read at all,
usually a lambda or a C callable, so they are not silently skipped.
