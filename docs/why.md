# Why this exists

I work on a large Django codebase, and the questions that cost real time are
never *what is in this project*. They are: if I delete this customer, what else
goes with it? Is this migration safe to deploy while the old pods are still
running? Which of these three hundred findings can a request actually reach?

Every tool I could point at that codebase answered the first kind of question.
I would still be reading through `models.py` by hand to answer the second kind,
and so was everyone else. So I wrote something that answers the second kind, and
kept it honest by pointing it at other people's code.

Eighteen public Django projects, from channels at 55 files to Saleor at 4332.
They found **twelve defects in this tool** that neither its own fixtures nor a
single private codebase had shown — a virtualenv inside a project directory
making every installed package look like a project app, a decorator that meant
the exact opposite of what a check assumed, a check that presented a project's
entire migration history as unshipped. Each one is in the changelog with the
measurement that found it. [`tested-against.md`](tested-against.md) has the
whole list, including the two checks that have **never** fired on real code and
the number of things they examined before finding nothing.

That is the part worth judging this on. Writing a check is easy; knowing whether
it is right, and saying so when you cannot tell, is the work.

Several Django MCP servers already exist. They answer *what exists*: list the
models, dump the schema, run the ORM, read the settings. None of the ones I
looked at answer *what will hurt*.

## Three checks worth reading about

### `deploy_safety`: has the code caught up yet?

`django-migration-linter` says `RemoveField` is backward incompatible. Always.
Repeated often enough, that stops being read.

This asks the question that actually decides the deploy:

```
BLOCKING shop.0002_remove_product_legacy_code  RemoveField 'legacy_code'
     shop/services.py:17  [string field name]  values("id", "sku", "legacy_code")
     shop/services.py:22  [keyword argument]   filter(legacy_code__startswith=code)
     shop/services.py:27  [attribute access]   f"{product.sku} / {product.legacy_code}"
     shop/services.py:32  [keyword argument]   Product(sku=sku, legacy_code="")
```

and the other verdict, which is the point:

```
CLEAR    shop.0002_remove_product_legacy_code  RemoveField 'legacy_code'
         no remaining reference found outside migrations
```

Python is parsed with the AST, so comments and docstrings cannot produce a hit.
Full page: [`deploy-safety.md`](deploy-safety.md).

### `find_unscoped_queries`: absence has no syntax

```python
def order_detail(request, pk):
    return Order.objects.get(pk=pk)
```

Nothing is wrong with that line, and it is how most IDOR reports start. This
class of bug is hard for static analysis because **the defect is the absence of
a filter**, and there is no dangerous call to match on. The tools that work
today are runtime or architectural.

The model graph makes it checkable. A generic analyser does not know whether
`Order` belongs to anybody; this one knows it reaches the tenant root through
`customer`:

```
high    shop/api.py:14   Order.objects.get(pk=pk)
        shop.Order is owned via 'customer', filtered on ['pk']
        add: .filter(customer=<the request user>)
```

Models with no path to the owner, like the product catalogue, are never
reported. Full page and blind spots: [`tenancy.md`](tenancy.md).

### `what_happens_on`: what a save really does

```python
line.save()
```

That queues a Celery task. Nothing about the line says so, because the task is
three hops away:

```
OrderLine.save()
  post_save  touch_order                writes instance.order -> shop.Order
    post_save  create_invoice_for_order  writes Invoice.create() -> shop.Invoice
      post_save  announce_invoice        cache write: set()
                                         celery task: delay()
```

Tools that **list** signal receivers exist and are good. None of them follow the
chain, and the second hop is where the surprise lives. Resolving
`instance.order` needs the model graph, which is why it fits here.

This is also the other half of `delete_impact`, which walks `on_delete` and says
in its own output that it ignores signals. Full page: [`signals.md`](signals.md).

## Correlated risks

The part no single check can produce. Three separate warnings, each ordinary on
its own:

```
[CRITICAL] A full path from a URL to another owner's row
    shop.Invoice belongs to an owner through 'order__customer'.
    2 queryset(s) read it without scoping, and 1 serializer(s) return it
    over the API.
    seen by: find_unscoped_queries, serializer_exposure
```

`explain_model` runs every analysis for one model and looks for the overlaps: a
cascade that crosses into a different owner's subtree, a save that reaches
external systems several hops away, a sensitive field on owned data exposed by a
wildcard serializer. Correlation is hard to get anywhere else, because it needs
all the analyses in one process over one model graph.

## On the name

It is a Django tool, and the name is not a historical accident to apologise for:
of the 22 checks in the aggregate run, **19 need the app registry** — 16 of them
need Django itself and three more need Django REST Framework on top. One needs
only Python, one is FastAPI-specific and one is for SQLAlchemy. The reach beyond
Django is real and it is small.

An earlier version of this paragraph claimed the name stays because renaming the
repository would break every link to it. That is not true — GitHub permanently
redirects a renamed repository — and it was the wrong reason for the right
conclusion. The name stays because it is accurate.

## Bugs that ran without raising

Kept in [`../CHANGELOG.md`](../CHANGELOG.md) rather than tidied away. Static
analysis fails by being confidently wrong, not by crashing, and every one of
these produced perfectly reasonable-looking output:

1. Reverse relations lost their cardinality: the `one_to_many` case was missing.
2. `delete_impact` returned nothing: `on_delete` is on `field.remote_field`.
3. Nested loops were invisible: loop variables were bound to strings, not models.
4. `deploy_safety` matched docstrings and every `name` in the project.
5. Narrowing the scan emptied the analysis instead of flipping the verdict.
6. The package imported `server` eagerly and warned under `python -m`.
7. A prefetch check matched a name bound in one function against an accessor in
   another, a hundred lines away in Saleor, and called them the same object.

Each has an assertion that fails without the fix.

## The bar a new check has to clear

Every check in here documents **what it cannot see**, in its own output and on
its own page, and every suppression carries the reason next to it. That is not
politeness — it is the difference between a tool somebody trusts and one they
learn to ignore. Two finished features were deleted from this repository after
measurement showed they could not tell a real finding from a correct one, and
two more were abandoned before a line was written when the measurement said the
defect was too rare or the premise was wrong.

So a proposal for a new check answers four questions, which the
[issue template](../.github/ISSUE_TEMPLATE/new_check.yml) asks directly: what
the defect looks like as code, how it fails in production, what already finds
it, and what it must stay silent on.
