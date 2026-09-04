# django-chainsaw-mcp

An MCP server that **analyses** a Django project, not just describes it.

There are already several Django MCP servers. They answer *what exists*: list
the models, dump the schema, run the ORM, read the settings. None of the ones I
looked at answer *what will hurt*:

- what a delete actually takes with it,
- where the N+1 queries are,
- which pending migration stops writes or breaks the code that is still running
  during a rolling deploy.

That is the gap this fills. Everything is read-only and most of it never touches
the database.

## Tools

| Tool | What it answers |
| --- | --- |
| `project_info` | Does the target project load at all? Django version, settings module, installed apps, database engines. Run this first when something is broken. |
| `list_models` | Every model with fields, relation kind, direction and `on_delete`. |
| `delete_impact` | Delete one row of this model: which models lose rows through CASCADE, which `PROTECT` relations block it, which fields get nulled. Follows the graph transitively. |
| `find_n_plus_one` | Relation traversals in a template that each cost a query, with the `select_related` / `prefetch_related` that would fix them. |
| `migration_risk` | Pending migrations rated by what they do to a live database: blocks writes, rewrites the table, or breaks the running code mid-deploy. |

## Resource

`django://models` returns the full model graph as JSON.

Stable, addressable data belongs in a resource; actions and parameterised
queries belong in tools. Most MCP servers put everything in tools.

## Configuration

| Variable | Example |
| --- | --- |
| `DJANGO_CHAINSAW_PROJECT_PATH` | `/path/to/project` (the directory the settings module is importable from) |
| `DJANGO_CHAINSAW_SETTINGS_MODULE` | `myproject.settings` |

Django must be importable from the interpreter that runs the server, along with
whatever the target project's settings import.

```bash
claude mcp add django-chainsaw --scope local \
  --env DJANGO_CHAINSAW_PROJECT_PATH=/path/to/project \
  --env DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
  -- uv run --directory /path/to/django-chainsaw-mcp python -m django_chainsaw_mcp.server
```

## What the analysis does and does not know

Being wrong quietly is worse than being incomplete loudly, so each tool says
what it cannot see.

- **`delete_impact`** reads `on_delete` across the graph. It does not count rows
  and does not run custom `delete()` overrides or `pre_delete` / `post_delete`
  signals, which can delete more than the graph implies.
- **`find_n_plus_one`** reports **candidates**. It reads the template and the
  model graph, not the view. If the queryset already prefetches that path there
  is no extra query. Traversals inside a loop are `high`, outside a loop `low`.
- **`migration_risk`** reads migration operations. It does not know your row
  counts, your PostgreSQL version or your deploy strategy, all of which change
  how bad a given operation really is. `django-migration-linter` covers similar
  ground as a standalone linter; this is the same idea reachable from an
  assistant, plus the deploy-ordering problem.

## One process, one project

`django.setup()` mutates global state and cannot be undone, so a server instance
stays bound to the first project it loads. Asking it to switch raises a clear
error instead of failing confusingly later. Run a second instance for a second
project.

## Why `list_models` was built first

Four of the five tools need the same thing: a populated Django app registry
inside a process that is not the Django project. That bootstrap is the actual
work; everything after it is ordinary querying.

`list_models` is the smallest tool that proves both halves work, the MCP
transport and the Django boot. Starting with `find_n_plus_one` would have meant
debugging three unknowns at once.

## Development

```bash
uv sync
uv run python smoke_test.py      # calls the introspection functions directly
uv run python analysis_test.py   # the three analysis tools, with assertions
uv run python client_test.py     # drives the server over the real MCP transport
```

`testprojects/` holds a throwaway Django project with deliberately
relation-heavy models (self-referencing FK, one-to-one, many-to-many, several
reverse accessors) and a template written the way a slow page is written.

## Three bugs worth keeping in the README

Each of these ran without raising anything. That is the point: the failure mode
in this kind of tooling is not a crash, it is a confident wrong answer.

**1. Reverse relations reported as `Unknown`.** The cardinality check tested
`many_to_many`, `many_to_one` and `one_to_one` but not `one_to_many`. Every
reverse accessor lost its cardinality, which is exactly the half that matters,
because reverse relations are where N+1 queries come from.

**2. `delete_impact` found nothing at all.** `on_delete` lives on
`field.remote_field`, not on the field. `getattr(field, "on_delete", None)`
returned `None` silently, every relation classified as `UNKNOWN`, and the tool
cheerfully reported that deleting a customer had no consequences.

**3. Nested loops were invisible.** Loop variables were bound to the *string*
they iterated, so `{% for line in order.lines.all %}` left `line` unresolvable
and everything inside that loop went unreported. Loop variables now carry the
resolved element model, which is also what makes `prefetch_related('lines')`
show up.

`analysis_test.py` and `client_test.py` fail on all three.
