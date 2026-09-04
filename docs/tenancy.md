# `find_unscoped_queries`

## The bug with no syntax

```python
def order_detail(request, pk):
    return Order.objects.get(pk=pk)
```

Nothing is wrong with that line. It is also how most IDOR reports start: any
authenticated user can read any order by changing a number in the URL.

This class of defect is genuinely hard for static analysis, and the industry
says so plainly. There is no dangerous call to match on, no tainted input, no
suspicious import. **The defect is the absence of a filter, and absence has no
syntax.** That is why generic scanners miss it and why the working solutions so
far have been runtime (parse every SQL statement and check it carries a tenant
predicate) or architectural (PostgreSQL row-level security, `django-scopes`).

## Why it is tractable here

A generic analyser reading `Order.objects.filter(pk=pk)` has no idea whether
`Order` belongs to anybody.

**This server already knows the model graph.** So it can ask a much sharper
question: *this model reaches the tenant root through `customer`, and this
queryset filters on nothing that leads there.*

That is the whole idea. The model graph turns an unanswerable general question
into a narrow, checkable one.

## How it works

**1. Which models are tenant-scoped?**

Breadth-first from the tenant root outward along reverse relations, keeping the
shortest path. Walking outward from the root is the same as a forward path from
each model back to it.

```
shop.Order       owner path: customer            1 hop
shop.OrderLine   owner path: order__customer     2 hops
shop.Invoice     owner path: order__customer     2 hops
```

`shop.Product` and `shop.Category` never appear, because no chain of foreign
keys connects them to a customer. An unfiltered read of the catalogue is
correct, and is not reported.

**2. Which querysets touch them unscoped?**

Every queryset chain in the source is unwound with the AST:

```python
Order.objects.filter(customer=u).exclude(archived=True)
#   -> model "Order", methods [filter, exclude], keys [customer, archived]
```

A chain is **scoped** when any filter key is the ownership path or its first
hop. `filter(customer=…)` and `filter(order__customer=…)` both count.
`filter(pk=pk)` does not.

## What it reports

```
high    shop/api.py:14
        return Order.objects.get(pk=pk)
        shop.Order is owned via 'customer', filtered on ['pk']
        add: .filter(customer=<the request user>)

medium  shop/api.py:29
        return Invoice.objects.get(number=number)
        shop.Invoice is owned via 'order__customer', filtered on ['number']
        add: .filter(order__customer=<the request user>)
```

`high` means one hop from the owner, `medium` further away. The distance
matters: the closer a model is to the tenant root, the more obviously it is
somebody's data.

## Arguments

| Argument | Default | Meaning |
| --- | --- | --- |
| `tenant_root` | `auth.User` | the model that owns data |
| `search_path` | project path | directory to scan |
| `max_depth` | `4` | how many relation hops still count as owned |
| `include_exempt` | `false` | also scan admin, management commands, tests |

Pick the tenant root that matches the system. In a B2B product it is usually
`Organisation` or `Tenant`, not `User`, and choosing wrong makes the whole
report meaningless.

`admin.py`, management commands, tests, `conftest.py` and factories are skipped
by default: an unscoped queryset there is normal.

## As a CI gate

```bash
django-chainsaw tenancy --tenant-root shop.Customer --fail-on-findings
```

Exits `1` on any candidate. On an existing codebase that will fire immediately,
so the honest way to adopt it is to read the list once, fix or accept each
entry, and only then turn on the gate for new code.

## What it cannot see

This is the part that decides whether the tool is useful or misleading, so it
is stated in the output of every run:

- **A filter in a base class or mixin.** `get_queryset()` on a shared
  `TenantScopedViewSet` is invisible; every subclass looks unscoped.
- **A custom manager.** `Order.objects` may already be scoped if `objects` is a
  manager that filters by default.
- **A filter further down the same function**, applied to a variable rather than
  chained onto the queryset.
- **`Q()` objects and positional arguments.** Their contents cannot be read
  statically, so a chain that uses them is treated as scoped. That is a
  deliberate bias towards silence: better to miss one than to cry wolf on every
  complex query.
- **Object-level permission checks after the fetch.** Loading by pk and then
  calling a permission class is a legitimate pattern this cannot see.

**Candidates, not vulnerabilities.** The value is shortening the list a human
has to read, not closing the question.

## Where it fits

`deploy_safety` answers a question about time: has the code caught up with the
schema? This one answers a question about reach: does this query touch rows the
caller does not own?

Both work the same way. Take something the model graph knows, cross-reference it
against something the source code says, and report only where they disagree.
