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

## What it cannot see

- **Conditions.** A write inside `if created:` is reported as if it always
  happens. The chain shows what *can* be triggered, not what will be on a
  given call.
- **Receivers connected at runtime**, after the app registry finished loading.
- **Overridden `save()` methods** that write other models. Those are not
  signals; they are ordinary code, and they are invisible here.
- **Dynamic dispatch**, `getattr(obj, name)(), and writes through variables the
  AST cannot resolve to a model. Those are still reported as writes, with
  `resolved_model: null`, so the gap is visible rather than silent.
- **Whether a receiver is registered twice.** Double registration is a common
  bug; this reports what the registry contains, which will show duplicates but
  does not call them out.

`unreadable_receivers` lists any receiver whose source could not be read at all,
usually a lambda or a C callable, so they are not silently skipped.
