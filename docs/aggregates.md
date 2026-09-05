# `multiplied_aggregates`

## Two counts, both wrong

```python
Order.objects.annotate(
    lines=Count("lines"),
    shipments=Count("shipments"),
)
```

An order with 3 lines and 2 shipments produces 6 rows, because joining two
multi-valued relations gives the cartesian product of them. `lines` comes back
as 6. So does `shipments`.

Nothing raises. Two plausible-looking numbers, both the product of the two, and
the only way to notice is to already know the real answer. This reaches
production through a dashboard, which is exactly where a number nobody can
check by hand goes unquestioned for years.

Django's own aggregation documentation warns about it. No linter checks for it:
`flake8-django` and ruff's `DJ` rules never resolve a field path against the
model registry, and nothing else reads which relations are multi-valued.

## Which aggregates a join actually breaks

Only `Count` and `Sum`.

| | under a join | |
| --- | --- | --- |
| `Count` | counts the duplicates | **wrong** |
| `Sum` | adds each value once per copy | **wrong** |
| `Min` | the smallest value is still the smallest | correct |
| `Max` | likewise | correct |
| `Avg` | a multiplied total over a multiplied count | correct |

The first version of this check included `Min`, `Max` and `Avg`. Run against a
real project it reported a `Min("items__begin")` beside a join on a second
relation — and the number was right. A check that flags a correct query is
worse than one that reports less, so the set is the two that are genuinely
wrong.

## What fixes it

`Count(..., distinct=True)` collapses the duplicates and is treated as correct
here. `Sum` has no equivalent — the duplicates are real rows as far as it is
concerned — and needs its own `Subquery` with `OuterRef` and an empty
`order_by()`. So a query with two distinct-`Count`s is fine and the same query
with one `Sum` added is not, which is not a distinction anybody remembers under
deadline.

## The same multiplication from a filter

```python
Order.objects.annotate(lines=Count("lines")).filter(shipments__tracking="X")
```

The filter joins a second multi-valued relation and the count is multiplied by
however many shipments matched. Same mechanism, same silence, reported in its
own bucket because the fix is different — the condition usually wants to become
an `Exists()`.

## Where the chain can start

```python
Order.objects.annotate(...)      # a manager called objects
Order.available.annotate(...)    # any manager
qs = Order.objects.filter(...)   # a local, assigned earlier in the function
qs.annotate(...)
```

On a real project only **27 of 247** `annotate()` calls started from a model
directly. The other 220 started from a local variable, so a check that
understood only the first spelling saw 11% of the code and said "nothing found"
about the rest. Locals are resolved per function, so two functions using `qs`
for two different models stay two different models, and a name assigned from
two models is dropped rather than guessed at.

`annotate_calls_in_source` and `annotate_calls_seen` are in every report. A
clean result means nothing without that denominator.

## Usage

```bash
django-chainsaw aggregates [--search-path DIR] [--fail-on-findings]
```

Also in the aggregate `check` as `aggregates`.

## Limits

- **A queryset that crosses a function boundary** — `self.get_queryset()`, a
  manager method, a queryset passed as an argument — is not resolved.
- **A path that does not resolve to a field** (an annotation alias, a
  transform) is skipped rather than guessed at.
- **Three or more relations** are reported as one finding, not three.
- **`distinct=True` passed as a variable** is not read as distinct.
