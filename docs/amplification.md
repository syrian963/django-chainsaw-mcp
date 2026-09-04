# `amplification`

## Two facts, neither of which is wrong

```
GET /orders has no authentication.
GET /orders issues about 2852 queries per request.
```

The first is an authorisation question, and on a public catalogue it is the
right answer. The second is a performance question, and behind a login it is a
backlog item.

Together they are a different thing entirely: **one HTTP request, from anyone,
costs the database roughly three thousand queries.** A single client at ten
requests a second is thirty thousand queries a second — from a laptop, with no
credentials, against an endpoint that returns HTTP 200 every time. Nothing in
the access log looks like an attack.

Neither check finds this alone, because neither is a finding alone. That is the
whole idea.

## The other half is the unbounded list

```
GET /customers has no authentication.
GET /customers has no pagination.
```

Not a load problem. That is **the whole table, to anyone, in one request** — a
quieter way to lose a database than an injection, and it leaves the same trace
as a healthy request.

## What it looks like

```
34 endpoint(s) reachable without credentials were considered

  CRITICAL  shop.viewsets.SlowOrderViewSet  (~2851 queries)
            viewsets.py:29
            nothing narrows who can call this, and about 2851 queries per
            request and no pagination, so the response is the whole table.
            One client at ten requests a second is 28510 queries a second,
            from a laptop, with no credentials
            fix: give the view a permission class, or make the request cheap:
                 add pagination, and select_related/prefetch_related what the
                 serializer reads

  CRITICAL  GET /orders  (Order.items per row)
            app/orders.py:57
            nothing establishes who is calling this, and it walks Order.items
            once per row, so the cost of one request grows with the table and
            the caller needs no credentials
```

## Severity

| | |
| --- | --- |
| **critical** | ≥1000 queries, or unpaginated **and** ≥100 queries |
| **high** | public and unpaginated but cheap, or 100–999 queries |

The split matters. An earlier version made every unpaginated public list
critical, which produced thirteen criticals on the demo project — one costing
2851 queries and eight costing one. A label that everything wears is a label
that says nothing.

## Prior art

The tools that look for this are **DAST scanners**: they probe a running
service, which has to be reachable, deployed, and holding enough rows for the
cost to show up. GraphQL security work has the same idea under "expensive
resolver" analysis, again at runtime.

All of it is derivable from the source: the permission classes say who can
call, and the queryset and serializer say what it costs.

## What the number is worth

The query estimate carries a **stated assumption** about how many child rows a
parent has, so the absolute figure is only as good as that assumption. What
survives every choice of it is the **ratio** — an endpoint costing four figures
next to one costing two is a different kind of thing, and this one is reachable
without a password.

The unpaginated case does not depend on the estimate at all.

## When only half the question can be answered

Both halves come from other checks. If one of them cannot run — no DRF, no
FastAPI routes, a project whose views build their responses by hand — that is
reported under `checks_that_could_not_run` rather than returning an empty list.
An empty result and an unanswerable question look identical from the outside,
and only one of them is good news.

## What it cannot see

- **Rate limiting.** A public expensive endpoint behind a throttle is a
  different risk, and nothing here reads middleware or gateway config.
- **Authentication that is not a permission class or a dependency** — an
  upstream proxy, a WAF rule, a network boundary.
- **Whether the table is big.** The cost estimate counts queries, never rows,
  so an expensive endpoint over an empty table is reported the same as one
  over ten million.
- **Endpoints it could not cost.** A Django view with no `serializer_class`, or
  a FastAPI route whose work happens somewhere the call graph did not reach,
  has no cost side and so cannot be correlated.
