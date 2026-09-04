# `missing_indexes`

## Static, where the alternatives are dynamic

`django-indexes` answers this by intercepting requests and watching what the ORM
actually does. That is accurate, and it only ever sees the paths traffic
reached. The monthly report, the admin action, the endpoint one customer uses,
the management command that runs at 3am: none of them appear until somebody runs
them.

Reading the source covers every path in the repository, needs no database and no
traffic, and runs on a branch before it ships.

The cost is that it cannot weigh anything. A `filter(status=...)` on a table
with forty rows looks exactly like one on a table with forty million. So it
reports **where** an index is missing and **how often the code asks for it**, and
leaves the decision to somebody who knows the row counts.

## What counts as indexed

A field is not reported when any of these already cover it:

| | |
| --- | --- |
| `primary_key=True` | |
| `unique=True` | a unique constraint is an index |
| `db_index=True` | |
| a `ForeignKey` | Django indexes them unless told not to |
| `Meta.indexes` | including multi-column, first column only |
| `Meta.constraints` | |
| `Meta.unique_together` | first column only |

Only the **leading** column of a composite index is seekable on its own, which
is why the rest are still reported.

## What is deliberately ignored

**Lookups a btree index cannot serve.** `contains`, `icontains`, `iexact`,
`regex`, `iregex`, `endswith`, `iendswith`. Suggesting `db_index=True` for
`name__icontains` is wrong: that query needs a trigram index or full-text
search, not a btree. They are counted in `ignored_lookups` so the number is
visible rather than silently dropped.

**Relation traversals.** `filter(order__customer=…)` is a question about the
other table's indexes, not this one's. The foreign key on this side is already
indexed.

## Severity

`high` when a field is asked for three or more times, or when it appears in
`order_by` at all. Sorting on an unindexed column costs a sort on every query,
which is usually worse than a scan on a filter that runs rarely.

## Example

```
Files scanned: 10, candidates: 4 (1 high)
Ignored, no btree index would help: icontains=1

  high    shop.Product.name  (CharField, 3x, filter, get, order_by)
          shop/reports.py:14  return Product.objects.filter(name=term)
          shop/reports.py:18  return Product.objects.get(name=term)
          shop/reports.py:23  return Product.objects.order_by("name")
          name = models.CharField(..., db_index=True)

  medium  shop.Order.placed_at  (DateTimeField, 1x, filter)
          shop/reports.py:38  return Order.objects.filter(placed_at__gte=moment)
```

`sku` does not appear: it is `unique=True`. Neither does `OrderLine.order`,
because a foreign key is indexed. Neither does the `icontains` search.

## Use

```bash
django-chainsaw indexes
django-chainsaw indexes --min-occurrences 3      # only what the code leans on
django-chainsaw indexes --max-candidates 10      # exit 1 above the budget
```

## An index is not free

Every index costs write throughput, disk, and time on every insert and update.
A table that is written far more often than it is read can be measurably worse
with the index this tool suggests.

**Treat the output as a shortlist for someone who knows the table**, not a task
list. The tool knows your code; it does not know your data.
