# django-chainsaw-mcp

An MCP server and CLI that **analyses** a Django project rather than describing
it.

Several Django MCP servers already exist. They answer *what exists*: list the
models, dump the schema, run the ORM, read the settings. None of the ones I
looked at answer *what will hurt*:

- what a delete actually takes with it,
- where the N+1 queries are,
- which pending migration stops writes or breaks the code that is still running
  during a rolling deploy,
- whether a destructive migration is safe to ship **yet**,
- which queries read tenant-scoped rows without scoping the query,
- and what a single `save()` actually sets off, three hops away.

Everything is read-only, and most of it never touches the database.

## Two front ends, one analysis layer

| | For |
| --- | --- |
| **MCP server** | asking questions while working, from Claude Code or any MCP client |
| **`django-chainsaw` CLI** | the same checks with an exit code, so CI can gate on them |

## Tools

| Tool | Answers |
| --- | --- |
| `project_info` | Does the target project load at all? Run this first when something is broken. |
| `list_models` | Every model with fields, relation kind, direction and `on_delete`. |
| `delete_impact` | Delete one row: what cascades, what blocks, what gets nulled. Transitive. |
| `find_n_plus_one` | Relation traversals in a template that each cost a query, and the fix. |
| `scan_templates` | The same across a directory, resolving context from views. |
| `migration_risk` | Migrations rated: blocks writes, rewrites the table, breaks running code. |
| `deploy_safety` | **Is this destructive migration safe to ship yet?** |
| `find_unscoped_queries` | **Which queries read data the caller may not own?** The IDOR shape. |
| `what_happens_on` | **What does this save actually trigger?** Follows the signal chain. |

Plus the resource `django://models`. Stable addressable data belongs in a
resource; actions belong in tools.

Full reference: [`docs/tools.md`](docs/tools.md).

## The one worth reading about

`django-migration-linter` says `RemoveField` is backward incompatible. Always.
Repeated often enough that stops being read.

`deploy_safety` asks the question that actually decides the deploy: **has the
code caught up yet?**

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
Why and how: [`docs/deploy-safety.md`](docs/deploy-safety.md).

## The second one worth reading about

```python
def order_detail(request, pk):
    return Order.objects.get(pk=pk)
```

Nothing is wrong with that line, and it is how most IDOR reports start. This
class of bug is hard for static analysis because **the defect is the absence of
a filter, and absence has no syntax**: there is no dangerous call to match on.
The tools that work today are runtime or architectural.

The model graph makes it checkable. A generic analyser does not know whether
`Order` belongs to anybody; this one knows it reaches the tenant root through
`customer`, so it can say that filtering on `pk` alone is not enough:

```
high    shop/api.py:14   Order.objects.get(pk=pk)
        shop.Order is owned via 'customer', filtered on ['pk']
        add: .filter(customer=<the request user>)
```

Models with no path to the owner, like the product catalogue, are never
reported. Details and the blind spots: [`docs/tenancy.md`](docs/tenancy.md).

## And the third: what a save really does

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
in its own output that it ignores signals. Details:
[`docs/signals.md`](docs/signals.md).

## Quick start

Install it **into your project's virtualenv**. The server calls
`django.setup()`, which imports your settings and everything in
`INSTALLED_APPS`, so the interpreter running it needs your project's
dependencies:

```bash
# inside your project's venv
pip install -e /path/to/django-chainsaw-mcp
```

Point it at the project with two environment variables:

| Variable | Example |
| --- | --- |
| `DJANGO_CHAINSAW_PROJECT_PATH` | `/srv/app` (the directory settings are importable **from**) |
| `DJANGO_CHAINSAW_SETTINGS_MODULE` | `myproject.settings` |

### As a CLI

```bash
django-chainsaw deploy-safety          # exit 1 if a migration is unsafe
django-chainsaw n+1 --max-high 12      # exit 1 above the budget
django-chainsaw delete-impact shop.Customer
django-chainsaw --json models | jq .
```

Exit codes and a CI workflow: [`docs/cli.md`](docs/cli.md).

### As an MCP server

```bash
claude mcp add django-chainsaw --scope local \
  --env DJANGO_CHAINSAW_PROJECT_PATH=/srv/app \
  --env DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
  -- /srv/app/.venv/bin/python -m django_chainsaw_mcp.server
```

Then ask it `project_info` first: it is the smallest call that proves both the
transport and the Django boot.

**Separate environment, Docker, Claude Desktop, other clients, and what the
error messages mean:** [`docs/usage.md`](docs/usage.md).

## What the analysis does not know

Nothing here executes the target project or reads its data, which buys safety
and speed and costs certainty. Every tool states its own blind spots in its
output rather than hiding them:

- `delete_impact` does not run signals or custom `delete()` overrides.
- `find_n_plus_one` reports **candidates**; it reads the template and the model
  graph, not the queryset in the view.
- `migration_risk` does not know row counts, PostgreSQL version, or deploy
  strategy.
- `deploy_safety` cannot see `getattr(obj, name)`, runtime SQL, or another
  repository. `clear` means nothing was found here.

**A confident wrong answer is worse than an incomplete one.** In this kind of
tooling the failure mode is not a crash, it is a plausible sentence that sends
someone in the wrong direction.

## Documentation

| | |
| --- | --- |
| [`docs/usage.md`](docs/usage.md) | **start here**: installing against a real project, clients, Docker, troubleshooting |
| [`docs/architecture.md`](docs/architecture.md) | how it is put together, and why the bootstrap drives the design |
| [`docs/tools.md`](docs/tools.md) | every tool, argument and output shape |
| [`docs/deploy-safety.md`](docs/deploy-safety.md) | the rolling-deploy problem and how references are found |
| [`docs/tenancy.md`](docs/tenancy.md) | the IDOR shape, and why the model graph makes it checkable |
| [`docs/signals.md`](docs/signals.md) | tracing the signal chain, and the other half of `delete_impact` |
| [`docs/cli.md`](docs/cli.md) | commands, exit codes, CI |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | setup, tests, how to add a tool |
| [`CHANGELOG.md`](CHANGELOG.md) | including every bug and what it looked like |

## Development

```bash
uv run python smoke_test.py      # introspection, called directly
uv run python analysis_test.py   # the analysis tools, with assertions
uv run python client_test.py     # the server over the real MCP transport
bash exitcheck.sh                # CLI exit codes
```

`client_test.py` is the one that counts: it starts the server as a separate
process and speaks stdio to it. The others prove nothing about the protocol.

`testprojects/` is a throwaway Django project shaped to expose bugs: a
self-referencing FK, a `PROTECT` relation, a nested-loop template, and a
migration that removes a field four other places still use.

## One process, one project

`django.setup()` mutates global state and cannot be undone, so a server instance
stays bound to the first project it loads and says so when asked to switch. Run
a second instance for a second project.

## Six bugs, all of which ran without raising

Kept in [`CHANGELOG.md`](CHANGELOG.md) rather than tidied away. Static analysis
fails by being confidently wrong, not by crashing, and every one of these
produced perfectly reasonable-looking output:

1. Reverse relations lost their cardinality: the `one_to_many` case was missing.
2. `delete_impact` returned nothing: `on_delete` is on `field.remote_field`.
3. Nested loops were invisible: loop variables were bound to strings, not models.
4. `deploy_safety` matched docstrings and every `name` in the project.
5. Narrowing the scan emptied the analysis instead of flipping the verdict.
6. The package imported `server` eagerly and warned under `python -m`.

Each has an assertion that fails without the fix.
