# `choice_typos`

## Two Ls

```python
class Order(models.Model):
    class Status(models.TextChoices):
        CANCELED = "canceled", "Canceled"
        SHIPPED = "shipped", "Shipped"
```

```python
Order.objects.filter(status="cancelled")
```

Valid Python. Valid SQL. Zero rows. No exception. Wrong forever.

The read side is bad because an empty result looks exactly like "no orders are
cancelled", which is a sentence a reviewer reads past and a test agrees with
unless it happens to cover that branch with real data. The write side is worse:

```python
Order.objects.create(status="cancelled")
```

`choices` is enforced by `full_clean()`. A queryset never calls it and
`create()` never calls it, so the value goes into the column and the
application now holds a state it does not believe exists.

## Why nothing else finds this

`django-stubs` types a `CharField` as `str`, not as a `Literal` union of its
choices, so mypy sees `filter(status=str)` and is satisfied. `flake8-django`
and ruff's `DJ` rules never read the model registry. `django-choices-field`
makes the field an enum, which is the right answer for new code and does
nothing about the literals already written.

## What is checked

| shape | example |
| --- | --- |
| lookups | `filter(status=...)`, `exclude`, `get`, `get_or_create` |
| `__in` | `filter(status__in=["canceled", "dispatched"])` — one wrong value is enough |
| writes | `create(...)`, `update(...)`, `Order(status=...)` |

Only where the model is named in the code, and only literals. An enum member
is the spelling that cannot go wrong — misspell `Order.Status.CANCELED` and
Python raises `AttributeError` on the spot.

## What is deliberately silent

- **`iexact`, `icontains`, `startswith`.** A case-insensitive or substring
  lookup can legitimately match a value spelled differently from the literal,
  so a difference is not evidence of a typo.
- **A string against an integer field.** Django coerces the value to the
  field's type before it reaches the database, so `filter(currency="1")` where
  the choices are `1` and `2` is correct code. The comparison is on the string
  forms, which is what actually happens at runtime; comparing the objects
  reported working code as a typo on the first real project it saw.
- **Migrations.** A historical model carries the choices of its own time, and a
  data migration rewriting an old value to a new one has to name the old one.
- **Anything that is not a literal.** A variable, a call, an f-string.

## What was tried and thrown away

`order.status == "cancelled"` names no model. The obvious move is to judge the
literal against every model that has a field of that name and report it when
none of them allows it. That was built, and then measured on a real codebase of
424 models: **five findings, five false positives.** Every one was an attribute
on an object that is not a model — a plain `Header` class holding a string, and
`datetime.date.month`.

Tightening the rule so that every same-named field had to carry choices took it
from 59 findings to 5 and did not change the ratio. Nothing available here
decides whether `obj.status` is a model field, so the route is gone rather than
shipped behind a caveat.

## Usage

```bash
django-chainsaw choices [--search-path DIR] [--skip-tests] [--fail-on-findings]
```

Tests are scanned by default. A test asserting on a misspelled status is the
same defect wearing a different hat, and it is also the place a wrong literal
survives longest, because the assertion passes.

Also in the aggregate `check` as `choices`.

## Limits

- **A `Q()` object names no model either.** `Q(status="cancelled")` is not
  checked, for the same reason the bare comparison is not.
- **Choices assembled at runtime.** A `choices=` built from a database query or
  a settings value is read as whatever it evaluated to when Django booted.
- **A misspelling that happens to be another valid choice** is not a typo to
  anything that can only read the code.
