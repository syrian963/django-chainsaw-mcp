# django-chainsaw-mcp

![release](https://img.shields.io/badge/release-v0.1.4-1f6feb?style=for-the-badge&labelColor=22272e)
![checks](https://img.shields.io/badge/checks-22-8957e5?style=for-the-badge&labelColor=22272e)
![MCP tools](https://img.shields.io/badge/MCP%20tools-37-8957e5?style=for-the-badge&labelColor=22272e)
![CLI commands](https://img.shields.io/badge/CLI%20commands-36-8957e5?style=for-the-badge&labelColor=22272e)
![prompts](https://img.shields.io/badge/prompts-5-8957e5?style=for-the-badge&labelColor=22272e)

![tests](https://img.shields.io/badge/tests-440-238636?style=for-the-badge&labelColor=22272e)
![coverage](https://img.shields.io/badge/coverage-86%25-238636?style=for-the-badge&labelColor=22272e)
![python](https://img.shields.io/badge/python-3.12%20%7C%203.13%20%7C%203.14-484f58?style=for-the-badge&labelColor=22272e&logo=python&logoColor=white)
![django](https://img.shields.io/badge/django-4.2%20%E2%80%93%206.1-484f58?style=for-the-badge&labelColor=22272e&logo=django&logoColor=white)
![license](https://img.shields.io/badge/license-MIT-484f58?style=for-the-badge&labelColor=22272e)

**An MCP server and CLI that analyses a Django project rather than describing
it.** Not *what is in here* — *what will hurt*: what a delete takes with it,
which migration breaks the pods still running, which query returns another
tenant's row, what a `save()` sets off three hops away.

There is **no model in the loop**. Every answer comes from the AST and Django's
own app registry, so the same input gives the same output, and nothing leaves
the machine. It is an MCP server so an assistant can ask it questions, and a CLI
so CI can gate on the answers.

22 checks: 19 need the app registry (16 Django, three of those DRF as well), one
needs only Python, one reads FastAPI, one reads SQLAlchemy.
**[Why this exists →](docs/why.md)**

## Install

**It has to run in an interpreter that can import your project.** Everything
here reads the app registry, which means `django.setup()`, your settings and
your apps:

```bash
/path/to/project/.venv/bin/python -m pip install django-chainsaw-mcp
```

A plain `uvx django-chainsaw-mcp` starts and then fails every check, because
`uvx` gives it an isolated environment with no trace of your project. To avoid
installing, hand `uv` the dependencies instead:

```bash
uvx --with-requirements requirements.txt --from django-chainsaw-mcp django-chainsaw check
```

Two environment variables point it at the project:

| Variable | Example |
| --- | --- |
| `DJANGO_CHAINSAW_PROJECT_PATH` | `/srv/app` — the directory settings are importable **from** |
| `DJANGO_CHAINSAW_SETTINGS_MODULE` | `myproject.settings` |

`django-chainsaw project-info` proves the setup before anything else, and says
which half is missing. **[Five minutes end to end →](docs/quickstart.md)**

mcp-name: io.github.syrian963/django-chainsaw-mcp

## One command

```bash
django-chainsaw check --tenant-root myapp.Organisation
```

```
44 finding(s): 1 critical, 14 high, 29 medium

CRITICAL
--------
  [deploy-safety] RemoveField drops 'legacy_code' while code still uses it
      shop/0002_remove_product_legacy_code
      During a rolling deploy the old pods keep running against the new
      schema and will fail.
      fix: Ship a release that stops using it, deploy that everywhere,
           then ship this migration.
```

Every analysis, merged, worst first, one exit code.

## On a pull request

This is the part that decides whether a tool like this survives. Point `tenancy`
at a five year old project and it returns two hundred candidates; nobody reads
two hundred candidates, somebody adds `continue-on-error`, and it runs forever
with nobody looking.

```bash
django-chainsaw tenancy --since main     # only what this branch changed
django-chainsaw tenancy --baseline       # everything old, ratcheted
django-chainsaw check --sarif out.json   # annotate the diff, on the line
```

`--since` compares at the **merge base**, so a branch that is behind main is not
blamed for other people's work. `--baseline` keeps existing findings in the
report and stops them blocking; anything new fails the build, and fixing an old
one is reported so the number only ever goes down. Findings are fingerprinted on
file plus identity, never the line, so adding an import does not resurrect
twenty findings nobody touched.

`--sarif` writes the format GitHub and GitLab annotate a pull request with, so
findings land **on the line** instead of in a log nobody opens.

**[baseline.md](docs/baseline.md)** · **[cli.md](docs/cli.md)**

## As an MCP server

```bash
claude mcp add django-chainsaw --scope local \
  --env DJANGO_CHAINSAW_PROJECT_PATH=/srv/app \
  --env DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
  -- /srv/app/.venv/bin/python -m django_chainsaw_mcp.server
```

Ask it `project_info` first: the smallest call that proves both the transport
and the Django boot. Five prompts carry the ordering the tools do not —
`before_deploy`, `why_is_this_slow`, `what_breaks_if_i_delete`, `triage`,
`review_this_branch`.

**[Claude Desktop, Cursor, VS Code, Windsurf, Zed, Docker →](docs/clients.md)**

## Or as one HTML file

```bash
django-chainsaw report --out findings.html --title myproject
```

![The HTML report, grouped by endpoint](docs/assets/report.png)

Grouped by **endpoint** is the view that matters: which pages carry this, and
through what call path. No server, no network, no build step — the CSS, the
script and the data are all in the file, so it works from a CI artifact or an
email attachment. **[report.md](docs/report.md)**

## The checks

<details>
<summary><b>Django projects</b> — these read the app registry, so they need <code>DJANGO_CHAINSAW_SETTINGS_MODULE</code> as well as the project path</summary>

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
| `missing_indexes` | Fields the code filters or sorts on that carry no index. |
| `datetime_audit` | Naive datetimes and field defaults that break when the clock moves. |
| `serializer_exposure` | What DRF serializers expose, including what the next migration will add. |
| `serializer_nplusone` | N+1 in DRF serializers, which is where it lives in an API project. |
| `explain_model` | **Everything about one model, plus the risks only visible combined.** |
| `endpoint_cost` | How many queries one request costs, before anybody sends one. |
| `api_contract` / `api_contract_check` | What this branch changes about the API, and who it breaks. |
| `escaping_side_effects` | Mail and tasks fired inside a transaction that can still roll back. |
| `bypassed_effects` | Bulk writes that skip everything the `save()` chain promised. |
| `race_conditions` | Counters read into Python, changed, and saved. Also unsafe upserts. |
| `money_precision` | **Where a decimal amount stops being exact.** |
| `celery_arguments` | **What the worker actually receives**, and whether it can even be called. |
| `queries_in_loops` | Queries written inside a loop, split by which of three fixes applies. |
| `defeated_prefetches` | Prefetches paid for and then re-queried by the accessor that reads them. |
| `request_impact` | Every finding grouped by the entry points that reach it, so the question becomes which endpoint to fix. |
| `choice_typos` | Literals a field's `choices` will never match: valid SQL, zero rows, no exception. |
| `multiplied_aggregates` | Counts and sums multiplied by a join across two multi-valued relations. |
| `dangling_references` | URL names, templates, signal senders and Celery tasks nothing will resolve. |
| `open_endpoints` | Sensitive fields on endpoints anybody can call. |
| `unused_eager_loading` | Joins and prefetches nothing in the response reads. |
| `check` | Run everything that applies, one severity-sorted list, one exit code. |
| `suggest_fixes` | **Findings turned into code, grouped by how safe each one is to apply.** |

</details>

<details>
<summary><b>Any Python project</b> — no Django, no settings module</summary>

| Tool | Answers |
| --- | --- |
| `project_profile` | What is this built on? Counted from the project's own imports. |
| `blocking_in_async` | **Which synchronous call stops the event loop for every request?** |
| `fastapi_exposure` | Endpoints that serialise more than they declare. |
| `sqlalchemy_nplusone` | Relationships loaded one row at a time, including during serialisation. |
| `amplification` | **Which endpoint can a stranger use to exhaust the database?** |

Plus the resource `django://models`. `check` profiles the project first and runs
what applies, and says **"does not apply, and here is why"** for the rest —
silence would read exactly like a clean result. Nothing about the FastAPI
support imports the project, so those checks run on a checkout with no
dependencies installed at all.

</details>

36 of the 37 tools declare `readOnlyHint`, so a client can stop asking
permission for each call; the exception is `api_contract_check` with
`update=True`, which writes the snapshot and says so.
**[Every tool, argument and output shape →](docs/tools.md)**

## What it will not tell you

Nothing here executes the target project or reads its data, which buys safety
and speed and costs certainty. Every tool states its own blind spots in its own
output:

- `delete_impact` does not run signals or custom `delete()` overrides.
- `find_n_plus_one` reports **candidates**; it reads the template and the model
  graph, not the queryset in the view.
- `migration_risk` does not know row counts, PostgreSQL version, or deploy
  strategy.
- `deploy_safety` cannot see `getattr(obj, name)`, runtime SQL, or another
  repository. `CLEAR` means nothing was found **here**.

**A confident wrong answer is worse than an incomplete one.** In this kind of
tooling the failure mode is not a crash, it is a plausible sentence that sends
someone in the wrong direction. **[limitations.md](docs/limitations.md)**

## What it does to your code

**It imports the target project.** `django.setup()` imports your settings and
every app in `INSTALLED_APPS`, and the checks additionally import the modules
that declare serializers, views and URLs — so anything those do at import time
happens. **Do not point this at code you would not run.**

**It does not run your application**: no view, no task, no management command.
**One check reads the database, read-only** — `MigrationLoader` reads
`django_migrations`, and nothing is written.
**It writes files only when you ask**: `fix --write` applies the mechanical
class of fix only, and a baseline, a contract snapshot or `--sarif` write where
you tell them to. **Nothing leaves the machine** — no network calls, no
telemetry, no uploads.

**[SECURITY.md](SECURITY.md)**

## Documentation

**[docs/](docs/README.md) is the index.**

| | |
| --- | --- |
| [`why.md`](docs/why.md) | why this exists, three checks worth reading about, and the bar a new one clears |
| [`quickstart.md`](docs/quickstart.md) | five minutes from clone to first finding |
| [`usage.md`](docs/usage.md) | installing against a real project, Docker, troubleshooting |
| [`clients.md`](docs/clients.md) | Claude Code, Cursor, VS Code, Windsurf, Zed |
| [`cli.md`](docs/cli.md) | commands, exit codes, CI |
| [`tools.md`](docs/tools.md) | every tool, argument and output shape |
| [`tested-against.md`](docs/tested-against.md) | eighteen public projects, what they found in this tool, and the checks that never fired |
| [`limitations.md`](docs/limitations.md) | what the analysis cannot see |
| [`baseline.md`](docs/baseline.md) | ratcheting, so this survives a legacy codebase |
| [`fixes.md`](docs/fixes.md) | suggestions as real code, and which can be applied |
| [`architecture.md`](docs/architecture.md) | how it is put together, and why the bootstrap drives the design |
| [`performance.md`](docs/performance.md) | where the time goes on a large project |

## Contributing

| | |
| --- | --- |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | workflow, house style, how to run the suites |
| [`CHANGELOG.md`](CHANGELOG.md) | every release, and the reasoning behind the changes |
| [`SECURITY.md`](SECURITY.md) | what this does to the code you point it at |
| [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) | be straight with people and be kind about it |

A proposal for a new check answers four questions, which the
[issue template](.github/ISSUE_TEMPLATE/new_check.yml) asks directly: what the
defect looks like as code, how it fails in production, what already finds it,
and what it must stay silent on. Two finished features were deleted from this
repository after measurement showed they could not tell a real finding from a
correct one.

MIT. One process stays bound to the first project it loads, because
`django.setup()` cannot be undone — run a second instance for a second project.
