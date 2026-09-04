# `endpoint_cost`

## A number, before anybody sends a request

Every tool that counts queries runs the application. The debug toolbar, silk,
`django-showmequeries`, an `assertNumQueries` in a test: all of them need
traffic, and all of them tell you afterwards.

The number is derivable from the source. A list endpoint costs:

- one query for the page, plus one for the pagination count,
- one **per object** for every serializer field that crosses a relation the
  view did not prefetch,
- and that again for each level of nesting.

Every term in that sentence is already known here. The view names its
serializer, the serializer names its fields, the model graph says which fields
are relations, and the view's queryset says what is already optimised. Nothing
is missing except the multiplication.

## What it looks like

```
3 endpoint(s), page size 50, assuming 5 child object(s) per parent

      2852  shop.viewsets.SlowOrderViewSet  (list)
            viewsets.py:29
                   1  the page itself
                   1  pagination count
                  50  customer
                  50  lines
                 250  lines__product
                1250  lines__product__category
                1250  lines__product__tags

         2  shop.viewsets.FastOrderViewSet  (list)
            viewsets.py:36
                   1  the page itself
                   1  pagination count
```

**The same serializer.** The difference is four calls on the queryset.

## The assumption that is not knowable

The first version of this multiplied every nested level by the page size, which
turned a three-level nesting into 50³ and produced **252,602**. That is
arithmetic, not an estimate. Fifty orders with three lines each means 150 line
objects, not 2500.

How many children a parent has is a property of **your data**, and no amount of
reading the code reveals it. So it is a parameter with a stated default:

```bash
django-chainsaw cost --page-size 50 --fan-out 5
```

| `--fan-out` | Slow view | Fast view | Ratio |
| --- | --- | --- | --- |
| 3 | 1152 | 2 | 576× |
| 5 | 2852 | 2 | 1426× |
| 20 | 41102 | 2 | 20551× |

**The ratio is the reliable part. The absolute number is only as good as the
assumption**, and the output says so. What survives every choice of fan-out is
that one of these endpoints is a different kind of thing from the other.

## Arguments

| Argument | Default | |
| --- | --- | --- |
| `page_size` | `50` | objects a list response returns |
| `nested_fan_out` | `5` | assumed children per parent, one level down |
| `list_only` | `false` | skip views that only return a single object |

```bash
django-chainsaw cost --max-queries 500     # exit 1 if the worst is above this
```

A budget is a better gate than a per-finding threshold here. Nobody agrees on
whether one N+1 is acceptable; everybody agrees that an endpoint costing four
figures is not.

## What it counts as optimised

Read from the view's `queryset` attribute and any `get_queryset` body:

- `select_related("customer")` covers `customer` and anything below it
- `prefetch_related("lines__product")` covers `lines__product` and deeper
- `select_related()` with no arguments covers every non-null forward relation
- a path built at runtime sets `dynamic`, and the estimate is then an
  upper bound rather than a guess

## What it cannot see

- **What a `SerializerMethodField` returns.** Its query cost *is* counted, from
  the method body. What it returns is a runtime question and is not.
- **Whether `.all()` on a related manager is free.** It costs nothing if the
  manager was prefetched and one query if it was not, and that depends on a
  queryset built elsewhere. Method-field costs are therefore an upper bound.
- **Cache hit rates.** A `cached_property`, a Redis layer, a `get_or_set`. Where
  a cache is detected inside a method field the endpoint says so and stops
  counting, rather than inventing a number.
- **Conditional fields.** A serializer that drops fields based on the request is
  estimated as if everything renders.

The estimate is deliberately rough. Its job is to separate a four-figure
endpoint from a single-digit one, and that distinction survives every one of
these limitations.
