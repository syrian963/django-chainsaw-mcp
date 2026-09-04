# Quickstart

Five minutes from a clone to a finding you can act on.

## 1. Install it into your project's environment

The server calls `django.setup()` on your project, which imports your settings
and everything in `INSTALLED_APPS`. So it has to run in an interpreter that can
import your dependencies, which in practice means your project's virtualenv.

```bash
# inside your project's venv
pip install -e /path/to/django-chainsaw-mcp
```

If that constraint is a problem, [usage.md](usage.md) covers the alternatives
and what still works without it.

## 2. Point it at the project

```bash
export DJANGO_CHAINSAW_PROJECT_PATH=/srv/app          # where settings is importable FROM
export DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings
```

`DJANGO_CHAINSAW_PROJECT_PATH` is the directory the settings module is
importable **from**. If your settings live at `/srv/app/myproject/settings.py`,
the path is `/srv/app`.

## 3. Check that it loaded

```bash
django-chainsaw models --short
```

If that lists your models, everything else will work. If it does not, the error
says which half failed, and the [troubleshooting
table](usage.md#when-something-is-wrong) maps each message to its cause.

No database is needed.

## 4. Run everything

```bash
django-chainsaw check --tenant-root myapp.Organisation
```

One command, every analysis, one list sorted worst first:

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

**Pick the right `--tenant-root`.** In a B2B product it is usually
`Organisation` or `Account`, not `auth.User`. Get it wrong and the ownership
half of the report is meaningless.

## 5. Ask about one model

```bash
django-chainsaw explain myapp.Invoice --tenant-root myapp.Organisation
```

Structure, ownership, what a delete removes, what a save triggers, what the API
exposes, which fields lack an index, and the risks that only appear when those
are combined:

```
CORRELATED RISKS
  [CRITICAL] A full path from a URL to another owner's row
      Invoice belongs to an owner through 'order__organisation'.
      2 queryset(s) read it without scoping, and 1 serializer(s) return it
      over the API. Each of those is a warning on its own; together they are
      a route from a request to somebody else's data.
      seen by: find_unscoped_queries, serializer_exposure
```

## 6. Put it in CI

Do not gate on everything from day one. On an existing codebase the first run
finds a lot, the build turns red, somebody adds `continue-on-error`, and the
tool is then running forever with nobody looking.

**Start with the one check that is always a real bug:**

```yaml
- name: A migration must not drop something the code still uses
  run: django-chainsaw deploy-safety
```

Then add the rest as a ratchet, either against a branch:

```yaml
- run: django-chainsaw check --since main --fail-on high
```

or against a recorded baseline:

```bash
django-chainsaw tenancy --baseline --update-baseline   # once, commit the file
```

```yaml
- run: django-chainsaw tenancy --baseline
```

`--since` for pull requests, a baseline for scheduled runs on the default
branch. Both in [baseline.md](baseline.md) and [cli.md](cli.md).

## 7. Connect it to your assistant

```bash
claude mcp add django-chainsaw --scope local \
  --env DJANGO_CHAINSAW_PROJECT_PATH=/srv/app \
  --env DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
  -- /srv/app/.venv/bin/python -m django_chainsaw_mcp.server
```

Then ask it things like *what happens if I delete a customer*, *can I ship this
branch's migrations*, or *tell me about the Invoice model*. Cursor, VS Code,
Windsurf and Zed are in [clients.md](clients.md), including the detail that the
config root key is different in three of them.

## What to read next

- Everything found is a **candidate** from static analysis, and each check says
  what it cannot see. That is not modesty, it is the difference between a tool
  people trust and one they mute.
- [`cli.md`](cli.md) for exit codes and the full CI workflow.
- [`tools.md`](tools.md) for the MCP tools and their arguments.
