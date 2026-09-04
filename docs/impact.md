# `request_impact`

## The problem with a correct list

Run `check` on a large codebase and it returns a few hundred findings, sorted
by severity, every one of them accurate. Nobody knows where to start.

Severity ranks the **defect**. It does not rank the **risk**, because risk is
severity times how often the code runs, and nothing in the list says whether a
line sits on the path of an endpoint served ten thousand times an hour or in a
management command last run in 2023. A `medium` on a hot login path outranks a
`critical` in a migration helper nobody has called since 2021, and the sorted
list puts them the other way round.

## Walking the other way

This takes the merged findings, maps each one to the function containing it,
and walks the call graph **backwards** to the entry points that reach it:

| kind | how it is recognised |
| --- | --- |
| `http` | `@app.get(...)` / `@router.post(...)` / `@app.route(...)`, `@api_view`, `@action`, and HTTP methods, DRF actions and framework hooks on a view class |
| `task` | `@shared_task`, `@periodic_task`, `@<app>.task` |
| `signal` | `@receiver` |
| `command` | `handle` under `management/commands/` |

```
29 entry point(s): 22 http, 4 signal, 3 task
106 finding(s) considered, 12 entry point(s) carry one.

  HIGH          1  list on shop.service_layer.OrderReportViewSet
            high      loops            shop/service_layer.py:20
                      via list -> decorate
```

Three files, one request. The defect is in `decorate`; nothing about `decorate`
says it is on the path of an HTTP request, and nothing about the viewset says
it queries. Only the chain between them does.

## Framework hooks are entry points too

Nothing in a project calls `get_queryset`. Django calls it, on every request.
Without that, a finding inside one is reached by no caller and lands in
`unattributed`, which reads as "probably fine" and is exactly wrong for a
method that runs on every request. So `get_queryset`, `get_object`,
`perform_create`, `filter_queryset`, `form_valid` and their neighbours count as
entry points when they sit on a view class.

The list is closed rather than "every method on a view class". Making a private
helper an entry point would stop the backward walk at the helper and hide the
action that actually serves the request.

## A serializer is not inside a function

An N+1 in a serializer is a field on a class. There is no enclosing function,
so the backward walk has nothing to start from, and on a real project that put
every single serializer finding into `unattributed` - the largest group there
by a wide margin.

A serializer is served by a view, though, and DRF records which. So when no
function holds the line, the class holding it is looked up in the
serializer-to-view map and attributed to the views that declare it:

```
  HIGH          6  shop.viewsets.ProductViewSet (declares the serializer)
            high      n+1-serializer   shop/api_serializers.py:16
                      ProductSerializer.category crosses a relation
                      via ProductViewSet -> ProductSerializer
```

A ViewSet that declares `serializer_class` and overrides nothing is still an
endpoint. There is no method to point at, so the class is named instead, which
is the honest answer rather than a missing one.

A serializer no view declares stays unattributed. It is still in the contract
and still a finding; it is simply not on the path of any request this can see,
and `get_serializer_class` can return anything at runtime.

## Backwards, not forwards

Reachability from every entry point forwards gives the same answer and costs a
full traversal per entry point. Walking back from the few hundred functions
that contain a finding is bounded by the findings rather than by the project,
and the path falls out of the walk for free.

## What `unattributed` does not mean

It does not mean unreachable, and it does not mean safe. It means **no entry
point this can see reaches the finding**, and the two ordinary reasons are:

- A plain Django function view wired up in a URLconf. It carries no decorator
  and belongs to no view class, so nothing in the source marks it as an entry
  point. Its findings land here.
- A call the graph could not resolve — a callable passed as an argument, a
  method looked up by name, a `getattr`.

Findings with no file and line at all — a migration, a template, a model-level
verdict — are counted separately in `without_a_location_count`, because there
is nothing to attribute them from. A serializer used to be in that list and no
longer is.

## Usage

```bash
django-chainsaw impact [--search-path DIR] [--tenant-root app.Model]
                       [--max-depth N] [--top N] [--per-entry N]
                       [--fail-on-findings]
```

`--max-depth` (default 8) bounds how many callers back the walk goes. A finding
nine hops from the nearest view is not attributed, and says so.

`--fail-on-findings` exits 1 when any entry point carries a high or critical
finding — a gate on "something a request can reach" rather than on the
codebase as a whole.

## Limits

- **The call graph's limits are this check's limits.** A dispatch it cannot
  resolve is a chain it cannot walk. `callgraph` reports its own unresolved
  count, and it is the honest denominator for this page.
- **Depth is a cutoff, not a fact.** Raising `--max-depth` finds more; the
  default is a compromise between usefulness and a walk that reaches half the
  project through a logging helper.
- **Every entry point weighs the same.** How often each one is actually called
  is a property of production traffic, not of the source. This narrows a few
  hundred findings to the dozen endpoints that carry them; which of those
  dozen is hot is a question for your metrics.
