# `defeated_prefetches`

A prefetch that was paid for and then thrown away.

## The shape

```python
orders = Order.objects.prefetch_related("lines")
for order in orders:
    for line in order.lines.filter(active=True):
        ...
```

Two queries look like one optimisation. What actually runs is three things:
the query for the orders, the prefetch query for every line belonging to
them — and then one more query per order, because `.filter()` cannot be
answered from the cache the prefetch filled.

So the loop costs more than it would have with no `prefetch_related` at all:
the same N+1, plus a query, plus the memory to hold every line on the page.
The line reads like somebody already thought about performance here, which is
why it survives review.

## Which accessors read the cache and which do not

Measured on Django 6.1 against a real model, ten parents with three children
each, counting queries with `CaptureQueriesContext`:

| Written on a prefetched related manager | Queries | Reads the cache |
|---|---|---|
| `order.lines.all()` | 2 | yes |
| `order.lines.all()[0]`, `.all()[:2]` | 2 | yes |
| `order.lines.count()` | 2 | yes |
| `order.lines.exists()` | 2 | yes |
| `order.lines.filter(...)` | 12 | no |
| `order.lines.exclude(...)` | 12 | no |
| `order.lines.order_by(...)` | 12 | no |
| `order.lines.first()`, `.last()` | 12 | no |
| `order.lines.only(...)`, `.defer(...)` | 12 | no |
| `order.lines.values_list(...)`, `.values(...)` | 12 | no |
| `order.lines.distinct()` | 12 | no |
| `order.lines.select_related(...)` | 12 | no |

`.count()` and `.exists()` being in the first group is the part that is worth
knowing and is not obvious. Since Django 4.1 the related manager answers both
from the prefetched result, so rewriting `order.lines.count()` as
`len(order.lines.all())` changes nothing. They are not reported, because a
finding whose fix is a no-op is how a check earns the reputation of being
noise.

Everything in the second group re-queries on every Django version, so a
finding here does not depend on which one the project runs.

## How it decides two things are the same object

Only within one scope. The name has to be bound to a prefetched queryset in
the same function, or be the loop variable iterating one:

```python
orders = Order.objects.prefetch_related("lines")   # bound here
for order in orders:                               # iterating it
    order.lines.filter(...)                        # reported
```

A prefetch in a view and the accessor in a template tag two files away is the
same defect and is **not** reported. That is deliberate. The suggested fix is
to delete or rewrite a line, and a guess is not a good enough reason to
suggest that.

The measurement behind the rule: matching on the relation name anywhere in the
same file found 79 sites across nine large open-source projects. Five were
read by hand and four of the five were coincidence — the same relation name,
unrelated objects. The scope-local rule finds 7 in the same nine projects, and
all 7 were read and confirmed.

Three consequences follow from the same rule, and each has a test:

- a name reassigned to a plain queryset stops carrying the prefetch from that
  line on;
- a comprehension reads the value the enclosing assignment is about to
  replace, so `users = [... for u in users]` resolves against the old `users`;
- a name bound inside one function does not match an accessor in another, even
  in the same file. Saleor has exactly that pair a hundred lines apart, and an
  earlier version of this check reported it.

## `Prefetch(..., to_attr=...)` is the fix, and it stays quiet

```python
orders = Order.objects.prefetch_related(
    Prefetch("lines", queryset=OrderLine.objects.filter(active=True),
             to_attr="active_lines")
)
for order in orders:
    for line in order.active_lines:      # a plain list, no query
        ...
```

`to_attr` puts the rows on a new attribute and leaves `order.lines` exactly as
it was. So a `Prefetch` with `to_attr` does not count as prefetching `lines`,
and a `.filter()` on `order.lines` afterwards is an ordinary query with no
prefetch behind it to throw away — not a finding.

Where the condition cannot move into the prefetch, the other fix is to read
the cached rows and do the work in Python over `order.lines.all()`. That is
the right answer for `.order_by()` and `.first()`, which sort and index a list
that is already in memory.

## Severity

| | |
|---|---|
| `high` | the object came from a loop over a prefetched queryset, so the re-query runs once per row |
| `medium` | a single object: no N+1, but a prefetch query is bought and never read |

Nothing here is `critical`. It is a performance defect, not a correctness one,
and marking it critical would push it above findings that lose data.

## What exists

[`nplusone`](https://github.com/jmcarp/nplusone) finds the neighbouring
problem at runtime — a relation eagerly loaded and then never touched — by
watching a request go past. It needs the code path to run, so it covers what
the tests happen to exercise.

[`unused_eager_loading`](overfetch.md) in this repository answers that same
question statically, for DRF viewsets, by comparing what the queryset loads
against what the serializer reads.

Neither answers this one. Here the prefetch *is* used — it is reached through
an accessor that cannot read it. Both halves sit in the same function, so
nothing has to run to see it.

## Where it stays quiet

- Across function or file boundaries, as above.
- A name a nested function closes over: each function is scanned as its own
  scope, which is the price of not matching across functions.
- A relation reached through a custom manager method, since the method body is
  not followed.
- Templates. `{% for line in order.lines.all %}` reads the cache and is
  correct; a template cannot call `.filter()` with arguments anyway.
- A relation that was never prefetched. That is a plain N+1 and belongs to
  [`queries_in_loops`](loop-queries.md).

## Running it

```bash
django-chainsaw prefetch
django-chainsaw prefetch --include-tests --fail-on-findings
```

`--fail-on-findings` exits 1 only on the once-per-row findings. Also in the
aggregate `check` as `prefetch`.
