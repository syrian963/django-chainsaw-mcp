# Architecture

## The shape of the thing

```
                    ┌──────────────────┐   ┌──────────────────┐
                    │  MCP client      │   │  CI / shell      │
                    │  (Claude Code)   │   │                  │
                    └────────┬─────────┘   └────────┬─────────┘
                             │ stdio                │ argv, exit code
                    ┌────────▼─────────┐   ┌────────▼─────────┐
                    │  server.py       │   │  cli.py          │
                    │  tools, resource │   │  subcommands     │
                    └────────┬─────────┘   └────────┬─────────┘
                             └──────────┬───────────┘
                                        │
                    ┌───────────────────▼────────────────────┐
                    │  analysis modules                      │
                    │  introspect · cascade · nplusone       │
                    │  migrations · deploy_safety · scan     │
                    └───────────────────┬────────────────────┘
                                        │
                             ┌──────────▼──────────┐
                             │  django_env.py      │
                             │  one-time boot      │
                             └──────────┬──────────┘
                                        │
                             ┌──────────▼──────────┐
                             │  target project     │
                             │  app registry       │
                             └─────────────────────┘
```

Two front ends, one analysis layer. Neither front end contains logic, which is
why the CLI was cheap to add: it is argument parsing, printing and an exit code
over functions that already existed.

## Why the bootstrap is a module of its own

Four of the six tools need a populated Django app registry inside a process
that is not the Django project. Getting there means putting the project on
`sys.path`, setting `DJANGO_SETTINGS_MODULE` and calling `django.setup()`.

That is the only genuinely hard part. Everything else is querying `_meta`,
walking an AST, or reading migration operations.

Three decisions follow from it:

**One process, one project.** `django.setup()` mutates global state and there is
no way back. A server instance stays bound to the first project it loads and
says so explicitly when asked to switch, rather than half-working later.

**The boot happens once, behind a lock.** `ensure_django()` is idempotent and
thread-safe. Tools call it without caring whether they are first.

**Boot failure is data, not an exception.** A broken settings module returns
`{"ok": false, "error": ...}` from the tool. An MCP server that dies because the
user pointed it at the wrong path is worse than one that explains the problem.

## Why analysis, not description

The existing Django MCP servers answer *what exists*: list models, dump schema,
run the ORM, read settings. That is the easy half and it is already covered.

Every tool here answers a question with a consequence:

| Question | Tool |
| --- | --- |
| What does this delete take with it? | `delete_impact` |
| Where are the N+1 queries? | `find_n_plus_one`, `scan_templates` |
| Which migration stops writes or breaks a rolling deploy? | `migration_risk` |
| Is this destructive migration safe to ship *yet*? | `deploy_safety` |
| Which queries read rows the caller does not own? | `find_unscoped_queries` |
| What does this save actually trigger? | `what_happens_on` |

## Static analysis, and being honest about it

Nothing here executes the target project's code or reads its data. Model graph,
templates, migrations and source files are read as structure.

That buys safety and speed, and costs certainty. The cost is stated in the
output of every tool rather than hidden:

- `delete_impact` does not run `pre_delete` signals or custom `delete()`
  overrides, which can remove more than the graph implies.
- `find_n_plus_one` reports **candidates**. It reads the template and the model
  graph, not the queryset in the view.
- `migration_risk` does not know row counts, PostgreSQL version, or deploy
  strategy.
- `deploy_safety` cannot see `getattr(obj, name)`, runtime SQL, or another
  repository. `clear` means nothing was found here.

**A confident wrong answer is worse than an incomplete one.** In this kind of
tooling the failure mode is not a crash, it is a plausible sentence that sends
someone in the wrong direction.

## AST, not grep

`deploy_safety` started as a text search over the source tree. It reported
`contenttypes.0002` removing a field called `name`, with hits in unrelated
models, and counted a sentence in a module docstring as a live reference.

Python files are now parsed with `ast` and matched structurally:

| Shape | Node |
| --- | --- |
| `product.legacy_code` | `ast.Attribute` |
| `values("legacy_code")` | `ast.Constant` with an exact match |
| `filter(legacy_code__startswith=…)` | `ast.keyword`, name or `name__` prefix |
| `Product(legacy_code="")` | `ast.keyword` |

Comments and docstrings cannot match, because a docstring is one `Constant`
holding a sentence and only an exact string equals the symbol.

Templates have no comparable tree, so they fall back to patterns with `{# #}`
comments stripped first.

## Scoping, or how false positives were removed

Two rules keep the noise down:

1. **Only migrations from apps inside the project.** `django.contrib` field
   names are generic and their migrations are not a deploy decision the user
   makes. `include_third_party=True` overrides this.
2. **Generic names are marked, not hidden.** A symbol in the common set
   (`name`, `id`, `type`, `status`, …) still gets analysed but comes back with
   `confidence: low` and a reason.

App ownership is derived from the **project path**, never from `search_path`.
The two were conflated once, and narrowing the scan then excluded every app and
returned an empty analysis instead of a `clear` verdict.

## Tools and resources

`django://models` is a resource; everything else is a tool. Stable, addressable
data belongs in a resource, actions and parameterised queries in tools. Most MCP
servers put everything in tools because it works, not because it is right.

## Layout

```
src/django_chainsaw_mcp/
    django_env.py     boot the target project, once
    introspect.py     models, fields, relations
    cascade.py        on_delete graph walk
    nplusone.py       template parse, chain resolution
    scan.py           directory sweep, context from views
    migrations.py     migration operations by production risk
    deploy_safety.py  migrations against remaining code references
    tenancy.py        ownership paths, unscoped queryset detection
    signals.py        signal chain tracing through receiver bodies
    server.py         MCP tools and resource
    baseline.py       fingerprinting, ratcheting against a recorded state
    cli.py            subcommands and exit codes
testprojects/         throwaway Django project used by the tests
```
