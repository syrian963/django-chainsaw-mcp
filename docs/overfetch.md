# `unused_eager_loading`

## The direction nobody looks

Every tool in this space asks the same question: which relation does the
serializer touch that the queryset did not prefetch? That is the N+1, and it is
worth asking.

This asks the opposite one.

```python
class OrderViewSet(ReadOnlyModelViewSet):
    serializer_class = OrderSummarySerializer          # id, placed_at
    queryset = Order.objects.select_related("customer") \
                            .prefetch_related("lines__product__category")
```

Nothing in that serializer renders a customer or a product category. Every
request pays for a JOIN it will not read a column from, plus three extra
queries and the memory to hold every product and category on the page.

It is invisible precisely because it looks like an optimisation — and it
usually *was* one. The field it was added for was removed a year ago and the
`select_related` stayed, because taking one out feels risky in a way that
leaving it in does not.

## The two costs are different

| | |
| --- | --- |
| unused `select_related` | a JOIN on every row, and every column of the joined table carried back |
| unused `prefetch_related` | an entire extra query, plus every object it returns held for the response |

So an unused `prefetch_related` is **medium** and an unused `select_related` is
**low**. Both are real; only one of them shows up in a query count.

## What it looks like

```
2 relation(s) loaded and never read

  MEDIUM  shop.viewsets.OverFetchingOrderViewSet
         .prefetch_related("lines__product__category")
         OrderSummaryOnlySerializer renders nothing under
         'lines__product__category', and it costs one extra query per request,
         plus every object it returns held in memory for the response
         reads: no relation at all
         fix: drop .prefetch_related("lines__product__category") from the
              queryset, unless something outside the serializer reads it
```

## Prior art

`nplusone` detects unused eager loading, at **runtime**, by watching which
loaded objects are never accessed. That needs the code path to execute, so it
covers whatever your tests happen to exercise. Both halves of the question are
already in the source: the queryset says what it loads, the serializer says
what it reads.

## When it stays quiet

This check suggests **deleting** a line, which is a destructive suggestion. A
wrong one reintroduces the N+1 that line was added to fix, so the bar is higher
than for the checks that suggest adding something.

It says nothing by default when:

- the serializer has a **`SerializerMethodField`** — the method can read
  anything, including the relation in question
- the view or serializer overrides **`list`**, **`retrieve`**, `get_queryset`
  or **`to_representation`** — the relation may be read there

Those are reported only with `--include-low-confidence`, tagged with which of
them applies:

```
  LOW    shop.viewsets.OpaqueOrderViewSet  (low confidence: SerializerMethodField)
         .select_related("customer")
```

And a queryset whose paths are **built at runtime** is skipped entirely and
counted under `views_with_runtime_paths`. Calling a path unused when the
declared list is incomplete is the one way this check could cause an N+1.

## What counts as reading a path

A declared path is earned by a read of it, of anything **below** it, or of
anything **above** it:

- `select_related("customer")` is earned by reading `customer__country`
- `prefetch_related("lines")` is earned by reading `lines__product`, because
  the deeper path cannot be traversed without the shallower one

## In CI

```bash
django-chainsaw overfetch --fail-on-findings
```

Exit 1 on any high-confidence finding.

## What it cannot see

- **Anything outside the serializer.** A filter, an ordering, a permission
  class, a signal — none of those need `select_related` to work, but a custom
  `list()` reading `obj.customer.name` does, and that is why an overridden
  `list` drops the finding to low confidence rather than suppressing it.
- **Whether the JOIN is actually expensive.** A `select_related` onto a small
  table is nearly free; onto a wide one it is not. The severity here reflects
  the kind of cost, never its size.
- **`Prefetch()` objects** with a custom queryset, which are recorded as a
  runtime-built path and skip the view.
