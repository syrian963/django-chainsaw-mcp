# `celery_arguments`

## What the worker actually receives

```python
order = Order.objects.get(pk=pk)
send_confirmation.delay(order)
```

The worker does not get that order. It gets whatever the serialiser made of
it, rehydrated later, on another machine, at a time nobody controls. Three
things follow, and all of them are quiet:

**The data is stale.** Between the `delay()` and the worker picking it up, the
row can change. The task acts on a snapshot, and the newer write is the one
that gets overwritten.

**It may not serialise at all.** Celery's default serialiser has been JSON
since 4.0, and a model instance is not JSON. Depending on configuration that is
a `TypeError` at the call site, a pickle of the entire object graph, or a
worker that fails to decode — and the first is the lucky case.

**The payload is the whole object.** Every field, including the ones the task
does not read, through the broker, on every call.

The fix is one attribute: pass `order.pk` and let the task load what it needs
at the moment it runs. Django's and Celery's documentation both say so.

## The argument count

```python
@shared_task
def reconcile(order_id, customer_id):
    ...

reconcile.delay(pk)          # one argument, two expected
```

Celery's `strict_typing` catches this — at **call time**. For a nightly job or
an error branch, call time is production, months later, in a log nobody reads.
Before the deploy is a better time.

A `@shared_task(bind=True)` task takes `self` from Celery, never from the
caller, and that is accounted for.

## What it looks like

```
3 task(s), 8 dispatch(es) resolved to one of them

  HIGH   shop/tasks.py:35  send_confirmation(order_id)
         send_confirmation.delay(order)
         the worker does not get this Order. It gets whatever the serialiser
         made of it, rehydrated later on another machine: the row may have
         changed in between, the whole object crosses the broker, and with the
         JSON serialiser it may not encode at all. The parameter is called
         'order_id', which is asking for an identifier
         fix: pass the identifier and let the task load it:
              send_confirmation.delay(<obj>.pk)

  HIGH   shop/tasks.py:51  reconcile given 1, expects 2
```

## Prior art

`flake8-pie` ships Celery lints — explicit task names, crontab arguments,
expirations. None of them look at what is passed.

## A name is not a task

The first version matched dispatches to tasks by bare name, and this
repository's own fixtures broke it immediately: `notifications.py` has a plain
object called `send_confirmation`, `tasks.py` has a Celery task with the same
name, and every call to the first was reported against the second's signature —
eighteen findings that were not real.

A dispatch is now only checked when the name resolves to a task **this project
defines**, through the calling file's own imports. The cost is that a task
reached some other way is skipped rather than guessed at, which is the right
direction to be wrong in.

## What is deliberately silent

- **`task.delay(order.pk)`** — the identifier, which is what the task asked for.
- **A keyword call**, `apply_async(kwargs={...})`. Arity cannot be judged from
  keywords, so it is not judged.
- **A task taking `*args`.**
- **`self` on a bound task.**

## What it cannot see

- **A task dispatched through a variable or a registry** — `TASKS[name].delay()`.
- **An instance that reached the call from another function.** The fetch and
  the dispatch have to be visible together.
- **Whether the instance was safe anyway.** A task that only reads `obj.pk`
  from a serialised dict works, and is still paying for the whole payload.
- **A custom serialiser** that handles model instances correctly. If your
  project has one, this check is telling you about the payload size and the
  staleness, not about a crash.
