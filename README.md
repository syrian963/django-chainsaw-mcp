# django-chainsaw-mcp

An MCP server that introspects Django projects: models, relations, and (later)
querysets, migrations and N+1 candidates. Read-only.

The point is to answer questions about a Django codebase without reading it
file by file: what models exist, how they relate, and where the expensive
relations are.

## Status

Two tools work end to end over stdio:

| Tool | What it does |
| --- | --- |
| `project_info` | Boots the target project and reports Django version, settings module, installed apps, database engines. Run this first when something is broken. |
| `list_models` | Every concrete model with its fields, relation kind, direction and `on_delete`. Optional `app_label` filter. |

Planned, in this order: `migration_state`, `explain_queryset`,
`find_n_plus_one`, `find_pattern`.

## Why `list_models` was built first

Four of the five planned tools need the same thing: a populated Django app
registry inside a process that is not the Django project. That bootstrap is the
actual work; everything after it is ordinary querying.

`list_models` is the smallest tool that proves both halves work, the MCP
transport and the Django boot. Starting with `find_n_plus_one` would have meant
debugging three unknowns at once.

## Configuration

The server is told which project to inspect through two environment variables:

| Variable | Example |
| --- | --- |
| `DJANGO_CHAINSAW_PROJECT_PATH` | `/path/to/project` (the directory the settings module is importable from) |
| `DJANGO_CHAINSAW_SETTINGS_MODULE` | `myproject.settings` |

Django must be importable from the interpreter that runs the server, together
with whatever the target project's settings import.

### Registering with a client

```bash
claude mcp add django-chainsaw --scope local \
  --env DJANGO_CHAINSAW_PROJECT_PATH=/path/to/project \
  --env DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
  -- uv run --directory /path/to/django-chainsaw-mcp python -m django_chainsaw_mcp.server
```

## One process, one project

`django.setup()` mutates global state and cannot be undone, so a server
instance stays bound to the first project it loads. Asking it to switch raises
a clear error instead of failing in a confusing way later. Run a second
instance for a second project.

## Development

```bash
uv sync
uv run python smoke_test.py     # calls the functions directly
uv run python client_test.py    # drives the server over the real MCP transport
```

`testprojects/` holds a small throwaway Django project with deliberately
relation-heavy models: self-referencing FK, one-to-one, many-to-many and
several reverse accessors. It exists so the introspection output has something
real to show.

## A bug worth keeping in the README

The first version reported every **reverse** relation as `Unknown`. It checked
`many_to_many`, `many_to_one` and `one_to_one` but not `one_to_many`. Nothing
raised, the output just quietly lost the cardinality of exactly the relations
that matter, because reverse accessors are where N+1 queries come from. The
planned `find_n_plus_one` tool would have been built on incomplete data.

Fixed in `introspect._cardinality`, and `client_test.py` now fails if any
relation comes back without a cardinality.
