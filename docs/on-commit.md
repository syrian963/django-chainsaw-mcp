# `escaping_side_effects`

## Two bugs that look like one line of ordinary code

```python
with transaction.atomic():
    order = Order.objects.create(...)
    send_order_confirmation.delay(order.pk)
```

**The famous one is a race.** `.delay()` hands the task to the broker
immediately. A worker can pick it up in milliseconds. The row is not committed
yet, so the worker queries for `order.pk` and finds nothing.

It passes every test. The test runs inside a transaction that never commits,
against a worker configured to run eagerly in-process, so the ordering problem
cannot occur. It fails in production, intermittently, under load, and the
traceback points at the worker rather than at the line that caused it.

**The quiet one is the rollback.** If anything after that line raises, the
order is gone and the customer has the confirmation email. That one no amount
of retrying fixes.

## Why nothing catches this today

The ecosystem's answer is entirely at runtime:

| | |
| --- | --- |
| `django-celery-oncommit` | a wrapper that defers your tasks for you |
| `django-post-request-task` | same idea, tied to the request cycle |
| Celery 5.4's `delay_on_commit()` | the same fix, built in |

Every one of those fixes the code you write **next**. None of them find the
calls already in the codebase. And the static linters do not look: the `DJ`
rules in `flake8-django` and in ruff are about models, forms and admin
conventions, not transaction boundaries.

Everything needed is in the source — the transaction boundaries, the calls that
reach outside the process, and whether they are deferred — so it can be found
without running anything.

## What it looks like

```
6 call(s) inside a transaction that cannot be rolled back

  HIGH   shop/notifications.py:39  task
         send_confirmation.delay(order.pk)
         inside @transaction.atomic on place_order()
         the broker has the task now, and a worker can start before this
         transaction commits. It will query for a row that is not there yet
         fix: transaction.on_commit(lambda: send_confirmation.delay(order.pk)),
              or .delay_on_commit() on Celery 5.4+

  HIGH   shop/notifications.py:42  outbound_http
         requests.post("https://warehouse.example/orders", json={"id": order.pk})
         inside @transaction.atomic on place_order()
         the request leaves the process immediately. If this transaction rolls
         back, the other system has been told about something that never happened
```

The fix quotes your actual call, so it is paste-ready rather than a category
name and a link to the docs.

## The case with nothing to see at the call site

```python
DATABASES = {"default": {..., "ATOMIC_REQUESTS": True}}
```

That wraps **every view** in a transaction. There is no `with` block, no
decorator, nothing at the call site to suggest a transaction is open at all. A
project with that setting has this defect in every view that sends anything,
and the code looks completely innocent.

So the tool volunteers it rather than waiting to be asked:

```
ATOMIC_REQUESTS is on for: default
Every view runs inside a transaction, with no atomic() block
anywhere in sight. The findings below are the visible cases.
```

## What counts as escaping

| Kind | Matched on | Severity |
| --- | --- | --- |
| `task` | `.delay`, `.apply_async`, `.send_task`, `.enqueue`, `.enqueue_at`, `.enqueue_in`, `.schedule` | high |
| `outbound_http` | `requests.*`, `httpx.*`, `urlopen` | high |
| `email` | `send_mail`, `send_mass_mail`, `mail_admins`, `mail_managers` | high |
| `cache_write` | `cache.set`, `cache.delete`, and the `_many` variants | medium |

Task methods are matched on the attribute name alone, because the object they
hang off is almost never resolvable — `send_welcome.delay(...)`,
`self.notify.apply_async(...)`, `get_task().delay(...)`. HTTP and cache calls
are matched on the **full dotted path**, deliberately: `.set()` is also a
related-manager method, and treating a bare `set` as a cache write already
produced one wrong finding elsewhere in this project.

`.send()` is the awkward one. It is `EmailMessage.send`, `Signal.send`,
`socket.send`, and every mock in the test suite. It is only treated as mail
when the receiver looks mail-shaped, it is always marked low confidence, and it
is hidden unless you ask:

```bash
django-chainsaw on-commit --include-low-confidence
```

## Silence is the point

Three things are deliberately **not** reported, and the check suite asserts
each one:

- a call already wrapped in `transaction.on_commit(...)`, including inside the
  lambda that is usually passed to it
- `.delay_on_commit(...)`, which is Celery 5.4's built-in version of the same
- the identical call sitting outside any transaction, where it is simply
  correct code

A check that flags the fixed version alongside the broken one gets switched off
within a week, so the fixture in `testprojects/` contains the same function
written both ways and the suite fails if the correct one produces anything.

## Nesting

```python
@transaction.atomic
def outer(pk):
    with transaction.atomic():
        obj.save()
    send_confirmation.delay(pk)   # still inside the outer transaction
```

The inner block closes. The transaction does not — Django's nested `atomic()`
is a savepoint, not a second transaction. The analysis carries a depth rather
than a flag for exactly this, and the suite has a case for it.

## In CI

```bash
django-chainsaw on-commit --fail-on-findings
```

Exit 1 when anything high severity escapes a transaction. Cache writes are
medium and do not fail the gate on their own, because writing a cache key
slightly early is a different order of problem from sending an email that
should never have gone out.

It is also part of the aggregate `check` under the name `on-commit`, so
`--skip on-commit` and `--only on-commit` both work.

## What it cannot see

- **A call reached in a way the graph cannot resolve.** Dispatch through a
  dictionary, a callback stored on an instance, `getattr(module, name)()`. The
  graph resolves direct imports, module attributes and `self.x()` including
  inherited, and counts what it could not resolve so the gap is visible.
- **Whether the transaction can actually roll back.** A call at the very end of
  an `atomic()` block with nothing after it that can raise is reported the same
  as one in the middle. The race against the worker applies either way; the
  rollback half may not.
- **A dispatch name nowhere in the tables.** A queue library nobody here has
  heard of will not match. The tables are plain dictionaries at the top of
  `on_commit.py` and are meant to be edited — though a project wrapping its own
  helper around a known one is followed into without any configuration.
