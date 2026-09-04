# Command line

The MCP server is for asking questions while working. The CLI is the same
analysis with an exit code, which is what turns it from something you consult
into something that stops a bad deploy.

```bash
django-chainsaw [--project-path PATH] [--settings MODULE] [--json] <command>
```

`--project-path` and `--settings` override `DJANGO_CHAINSAW_PROJECT_PATH` and
`DJANGO_CHAINSAW_SETTINGS_MODULE`. `--json` prints the raw payload instead of
the human-readable summary, for piping into `jq`.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | nothing found at or above the threshold |
| `1` | findings at or above the threshold |
| `2` | the project could not be loaded, or the arguments were wrong |

Commands only return `1` when a gate is requested. Without a gate they are
reporting tools and always exit `0`, so adding one to a pipeline never breaks it
by surprise.

## Commands

### `deploy-safety`

```bash
django-chainsaw deploy-safety [--search-path DIR] [--include-third-party]
```

Exits `1` if any pending destructive migration still has references in the code.
**This is the one worth gating on.**

### `n+1`

```bash
django-chainsaw n+1 [--templates DIR] [--context name=app.Model] [--max-high N]
```

Scans every template under `--templates`, resolving context from class-based
views. `--context` adds a variable applied to every template and can be repeated
for names no view supplies.

`--max-high N` exits `1` when more than N high severity candidates are found.
Without it the command only reports.

Use a ratchet rather than zero on an existing project: set N to today's count
and lower it as you go.

### `migrations`

```bash
django-chainsaw migrations [--include-applied] [--fail-on-risk]
```

Rates migrations by production impact. `--fail-on-risk` exits `1` if any
migration blocks writes, rewrites a table, or breaks running code.

Expect this to be noisy on a project with third-party migrations; it is more
useful as a review aid than as a gate.

### `delete-impact`

```bash
django-chainsaw delete-impact app_label.ModelName [--max-depth N]
```

Always exits `0`. Answers a question, does not gate anything.

### `models`

```bash
django-chainsaw models [--app LABEL] [--short]
```

Always exits `0`.

## In CI

```yaml
name: schema
on: [pull_request]

jobs:
  schema:
    runs-on: ubuntu-latest
    env:
      DJANGO_CHAINSAW_PROJECT_PATH: .
      DJANGO_CHAINSAW_SETTINGS_MODULE: myproject.settings
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5

      - name: Install project and analyser
        run: uv sync

      # The gate: a migration must not drop something the code still uses.
      - name: Deploy safety
        run: uv run django-chainsaw deploy-safety

      # A ratchet, not a wall. Lower the number as the codebase improves.
      - name: N+1 budget
        run: uv run django-chainsaw n+1 --max-high 12

      # Reporting only, never fails the build.
      - name: Migration risk
        run: uv run django-chainsaw migrations
```

`deploy-safety` needs no database: it reads migration files and source code. If
a database is reachable it also knows which migrations are already applied and
skips them, which makes the result tighter but is not required.

## Piping

```bash
django-chainsaw --json deploy-safety \
  | jq -r '.blocking[] | "\(.app).\(.migration): \(.symbol)"'

django-chainsaw --json n+1 \
  | jq -r '.results[] | select(.high_severity_count > 3) | .template'
```
