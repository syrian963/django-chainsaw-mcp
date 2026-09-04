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

## `check`, the one to start with

```bash
django-chainsaw check [--tenant-root app.Model] [--only CHECK] [--skip CHECK]
                      [--fail-on critical|high|medium|low] [--strict] [--sarif FILE]
```

Runs every analysis whose findings are defects, merges them into one list sorted
worst first, and returns one exit code. `--fail-on` defaults to `high`.

**`--strict` exits 2 if a check could not run.** Without it a failed check is
listed but the others still report, which is usually what you want
interactively and never what you want in CI: a check that crashed is not a pass.

**`--sarif FILE`** writes the findings in the format code-scanning UIs read, so
they appear as annotations on the diff rather than in a log.

```yaml
- run: django-chainsaw check --sarif chainsaw.sarif --fail-on critical
  continue-on-error: true
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: chainsaw.sarif
```

The `continue-on-error` there is deliberate: the upload step has to run even
when the gate fails, or the annotations never appear on the pull request that
needs them.

## `fix`, findings as code

```bash
django-chainsaw fix [--tenant-root app.Model] [--skip CHECK]
                    [--write] [--write-generated]
```

Prints the fixes in three groups and changes nothing. `--write` applies the
**mechanical** class only; `--write-generated` writes files such as index
migrations for review. Full reasoning in [fixes.md](fixes.md).

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

### `n+1-serializer`

```bash
django-chainsaw n+1-serializer [--max-depth N] [--max-high N]
```

Relation crossings in DRF serializers, with the `select_related` and
`prefetch_related` for each, grouped per serializer so it can be pasted into a
view. Follows nested serializers, so the lookup it suggests is the full path.

### `datetimes`

```bash
django-chainsaw datetimes [--search-path DIR] [--fail-on-findings]
```

### `serializers`

```bash
django-chainsaw serializers [--include-safe] [--fail-on-findings]
```

Exits `0` and says so if DRF is not importable, rather than pretending the
project has no serializers.

### `cost`

```bash
django-chainsaw cost [--page-size N] [--fan-out N] [--list-only] [--max-queries N]
```

Estimated queries per request, per endpoint. `--max-queries N` exits `1` when
the worst endpoint is above the budget. A budget gates better than a per-finding
threshold here: nobody agrees whether one N+1 is acceptable, everybody agrees
that a four-figure endpoint is not.

### `contract`

```bash
django-chainsaw contract [--snapshot FILE] [--update] [--max-depth N] [--fail-on-breaking]
```

What this branch changes about the API's shape. Run `--update` once on a branch
whose shape clients already rely on and commit the snapshot; after that
`--fail-on-breaking` exits `1` on any change that breaks an existing client, and
on any serializer that could not be read at all.

Exits `2` when no snapshot exists yet, rather than passing silently.

### `on-commit`

```bash
django-chainsaw on-commit [--search-path DIR] [--include-low-confidence] [--fail-on-findings]
```

Side effects inside a transaction that cannot be rolled back. `--fail-on-findings`
exits `1` on any high-severity one; cache writes are medium and do not fail the
gate alone. Also available in the aggregate `check` as `on-commit`.

### `bypass`

```bash
django-chainsaw bypass [--search-path DIR] [--model app.Model] [--fail-on-findings]
```

Bulk writes that skip a model's `save()` chain, with the receivers, overrides
and downstream models named per call. `--fail-on-findings` exits `1` when a
skipped chain writes or sends something. Also in the aggregate `check` as
`bypass`.

### `races`

```bash
django-chainsaw races [--search-path DIR] [--no-parameters] [--fail-on-findings]
```

Read-modify-save races, `select_for_update()` outside a transaction, and
upserts with no unique constraint behind their lookup.
`--fail-on-findings` exits `1` on any high-confidence race or any unprotected
lock; `--no-parameters` drops the medium-confidence tier. Also in the aggregate
`check` as `races`.

### `open`

```bash
django-chainsaw open [--sensitive-only] [--fail-on-findings]
```

Public endpoints whose serializer exposes a sensitive field (critical) or every
field via `__all__`/`exclude` (medium). `--fail-on-findings` exits `1` on any
critical. Also in the aggregate `check` as `open`.

### The names `--only` and `--skip` accept

`deploy-safety`, `tenancy`, `n+1-serializer`, `n+1-template`, `serializers`,
`indexes`, `datetimes`, `on-commit`, `bypass`, `races`, `money`, `open`,
`migrations`, `async`, `routes`, `sqla`.

`check` runs the ones that apply to the project in front of it and reports the
rest as not applicable, with the reason, so an unfamiliar project does not need
this list to get a useful answer.

### `overfetch`

```bash
django-chainsaw overfetch [--include-low-confidence] [--fail-on-findings]
```

Relations the queryset loads and the serializer never reads. An unused
`prefetch_related` is medium (a whole extra query), an unused `select_related`
is low (a JOIN per row). `--fail-on-findings` exits `1` on any high-confidence
one.

### `money`

```bash
django-chainsaw money [--search-path DIR] [--include-low] [--fail-on-findings]
```

Decimal amounts that stop being exact. Low findings — floats that happen to be
exactly representable — are hidden unless `--include-low`. `--fail-on-findings`
exits `1` on any high. Also in the aggregate `check` as `money`, where low
findings are excluded.

### `async`

```bash
django-chainsaw async [--search-path DIR] [--no-follow] [--max-depth N] [--fail-on-findings]
```

Blocking calls that run on the event loop, directly or reached through a
project function. Needs no Django, so it works on a FastAPI or Starlette
project with only `DJANGO_CHAINSAW_PROJECT_PATH` set.

### `routes`

```bash
django-chainsaw routes [--search-path DIR] [--fail-on-findings]
```

FastAPI endpoints with no `response_model` and no return annotation, and
declared models that carry a sensitive field. Unauthenticated is critical,
behind a dependency is high. Needs no Django.

### `sqla`

```bash
django-chainsaw sqla [--search-path DIR] [--fail-on-findings]
```

SQLAlchemy relationships crossed per row: inside a loop, and during response
serialisation where there is no loop to see. Needs no Django.

### `amplification`

```bash
django-chainsaw amplification [--search-path DIR] [--fail-on-findings]
```

Endpoints that are both reachable without credentials and expensive to answer,
or public and unpaginated. Works on Django (DRF views) and FastAPI. Neither
half is a finding alone.

### `profile`

```bash
django-chainsaw profile [--search-path DIR]
```

What the project is built on, counted from its own imports. Needs no Django,
so it is the first thing to run against an unfamiliar project.

### `explain`

```bash
django-chainsaw explain app_label.ModelName
```

Everything about one model, including the risks only visible when its fields,
relations and migrations are read together.

### `celery`

```bash
django-chainsaw celery [--search-path DIR] [--fail-on-findings]
```

Model instances handed to Celery tasks, and dispatches whose argument count
cannot match the task. Also in the aggregate `check` as `celery`.

### `loops`

```bash
django-chainsaw loops [--search-path DIR] [--no-writes] [--fail-on-findings]
```

Database work inside a loop, split into the query that uses the loop variable
(once per row), the one that does not (the same answer N times), and the write.
Also in the aggregate `check` as `loops`.

### `indexes`

```bash
django-chainsaw indexes [--search-path DIR] [--min-occurrences N] [--max-candidates N]
```

Fields filtered or sorted on without an index. `--max-candidates N` exits `1`
above the budget; without it the command only reports.

### `signals`

```bash
django-chainsaw signals app_label.ModelName [--event save|delete] [--max-depth N]
```

Prints the signal chain a save or delete sets off, indented by hop, with the
model writes and side effects at each step. Always exits `0`.

Worth running next to `delete-impact`: one shows what the schema removes, the
other what the code removes.

### `tenancy`

```bash
django-chainsaw tenancy [--tenant-root app.Model] [--search-path DIR] \
                        [--max-depth N] [--include-exempt] [--fail-on-findings]
```

Reports querysets on tenant-scoped models that carry no ownership filter.
`--fail-on-findings` exits `1` on any candidate.

**Pick the right root.** In a B2B product it is usually `Organisation`, not
`auth.User`, and choosing wrong makes the whole report meaningless.

On an existing codebase this fires immediately, so read the list once and fix or
accept each entry before turning the gate on.

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

      # A ratchet: fails only on N+1 candidates that are not in the baseline.
      - name: N+1 budget
        run: uv run django-chainsaw n+1 --baseline

      # Same for tenant scoping, which is far too noisy to gate without one.
      - name: Tenant scoping
        run: uv run django-chainsaw tenancy --tenant-root shop.Customer --baseline

      # Reporting only, never fails the build.
      - name: Migration risk
        run: uv run django-chainsaw migrations


```

`deploy-safety` needs no database: it reads migration files and source code. If
a database is reachable it also knows which migrations are already applied and
skips them, which makes the result tighter but is not required.

## `--since`, the lighter ratchet

`deploy-safety`, `n+1`, `tenancy` and `indexes` accept `--since REF`. Only
findings in files that changed relative to `REF` are reported.

```bash
django-chainsaw tenancy --tenant-root shop.Customer --since main --fail-on-findings
```

The comparison uses the **merge base**, not a plain two-dot diff. On a branch
that is behind `main`, `git diff main` also reports everything `main` gained in
the meantime, and the branch gets blamed for other people's work.

Uncommitted, staged and untracked files count too: the point is to catch it
before it is pushed.

An unknown ref warns and falls back to the full report rather than failing or,
worse, silently passing. Findings whose path cannot be matched are kept and
listed, because a finding that vanishes because two paths did not line up is
the worst outcome for a tool whose job is to say what is wrong.

**`--since` for a pull request, a baseline for a scheduled run on the default
branch.** They are different ratchets and both are useful.

## Baselines

`deploy-safety`, `n+1` and `tenancy` accept two more flags:

| Flag | Meaning |
| --- | --- |
| `--baseline [FILE]` | compare against a recorded baseline, fail on new findings only. Defaults to `.django-chainsaw-baseline.json` |
| `--update-baseline` | record today's findings and exit `0` |

```bash
django-chainsaw tenancy --baseline --update-baseline
django-chainsaw tenancy --baseline
```

With a baseline the command exits `1` only when something **new** appears, so
`--fail-on-findings` and `--max-high` are not needed alongside it. Full
reasoning in [`baseline.md`](baseline.md).

## Piping

```bash
django-chainsaw --json deploy-safety \
  | jq -r '.blocking[] | "\(.app).\(.migration): \(.symbol)"'

django-chainsaw --json n+1 \
  | jq -r '.results[] | select(.high_severity_count > 3) | .template'
```
