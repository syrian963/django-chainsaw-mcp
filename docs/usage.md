# Getting it running on a real project

## The one thing that decides everything

The server calls `django.setup()` on your project. That imports your settings,
which imports every entry in `INSTALLED_APPS`, which imports their dependencies.

**So the interpreter that runs this server needs your project's dependencies
importable.** Not just Django: DRF, `django-storages`, whatever your settings
touch at import time.

Get that wrong and the first call returns a readable error instead of a crash,
but it still will not work:

```json
{"ok": false,
 "error": "django.setup() failed for settings module 'myproject.settings'
           at /srv/app: ModuleNotFoundError: No module named 'rest_framework'"}
```

Everything below is a different answer to the same question: *which Python runs
this, and can it import your project?*

---

## Option 1: install into your project's environment (recommended)

The simplest correct answer. Your project's venv already has every dependency,
so put the analyser there too.

```bash
# inside your project's virtualenv
pip install git+ssh://git@github.com/<owner>/django-chainsaw-mcp.git

# or from a local clone, editable
pip install -e /path/to/django-chainsaw-mcp
```

Then use the venv's own entry points:

```bash
export DJANGO_CHAINSAW_PROJECT_PATH=/srv/app
export DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings

django-chainsaw models --short
django-chainsaw deploy-safety
```

**This path is covered by `install_check.sh`**, which builds a throwaway venv,
installs Django and this package into it, and drives both the CLI and the MCP
server with that interpreter. Run it if you change packaging.

### With uv

```bash
uv add --dev git+ssh://git@github.com/<owner>/django-chainsaw-mcp.git
uv run django-chainsaw deploy-safety
```

---

## Option 2: keep it separate, point it at the project

Works when your settings import little beyond Django, for example a small
service or a settings module written for exactly this.

```bash
git clone <repo> ~/tools/django-chainsaw-mcp
cd ~/tools/django-chainsaw-mcp
uv sync

DJANGO_CHAINSAW_PROJECT_PATH=/srv/app \
DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
uv run django-chainsaw models
```

The moment `INSTALLED_APPS` names a package this environment does not have, go
back to Option 1.

**Half of it works anyway.** `migration_risk` and `deploy_safety` read migration
files and source code, so they need Django but not your third-party stack.
`list_models`, `delete_impact` and the N+1 tools need the app registry, so they
need everything.

---

## Option 3: the project runs in Docker

Run the analyser inside the container, where the dependencies already are.

```bash
docker compose exec web pip install -e /workspace/django-chainsaw-mcp
docker compose exec \
  -e DJANGO_CHAINSAW_PROJECT_PATH=/app \
  -e DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
  web django-chainsaw deploy-safety
```

For the MCP server, the client has to launch the container command instead of a
local binary:

```json
{
  "command": "docker",
  "args": ["compose", "exec", "-T", "web",
           "python", "-m", "django_chainsaw_mcp.server"],
  "env": {
    "DJANGO_CHAINSAW_PROJECT_PATH": "/app",
    "DJANGO_CHAINSAW_SETTINGS_MODULE": "myproject.settings"
  }
}
```

`-T` matters: without it `docker compose exec` allocates a TTY and the stdio
protocol breaks.

Paths inside the container are what count. `DJANGO_CHAINSAW_PROJECT_PATH` is
`/app`, not the host path, and file locations in the output refer to the
container filesystem.

---

## Wiring it into a client

### Claude Code

```bash
claude mcp add django-chainsaw --scope local \
  --env DJANGO_CHAINSAW_PROJECT_PATH=/srv/app \
  --env DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
  -- /srv/app/.venv/bin/python -m django_chainsaw_mcp.server

claude mcp list     # should print: django-chainsaw ... ✔ Connected
```

`--scope local` keeps it to the current project. Use `--scope user` for every
project on the machine, which only makes sense if you always analyse the same
codebase.

### Claude Desktop and other JSON-configured clients

```json
{
  "mcpServers": {
    "django-chainsaw": {
      "command": "/srv/app/.venv/bin/python",
      "args": ["-m", "django_chainsaw_mcp.server"],
      "env": {
        "DJANGO_CHAINSAW_PROJECT_PATH": "/srv/app",
        "DJANGO_CHAINSAW_SETTINGS_MODULE": "myproject.settings"
      }
    }
  }
}
```

**Use the absolute path to the venv's Python.** These clients do not inherit
your shell, so `python` or `uv` on bare `PATH` usually is not found.

### Anything else

The server speaks stdio and nothing else. A client that can launch a process
with arguments and environment variables can drive it.

---

## First run

Ask `project_info` before anything else. It is the smallest call that proves
both halves work: the transport and the Django boot.

```
project_info ok: True  django: 5.1.4  settings: myproject.settings
installed_apps: [admin, auth, contenttypes, rest_framework, myapp, ...]
```

If that is right, everything else will work. If it is not, nothing else will,
and the error text says which half failed.

---

## Questions worth asking it

Once connected, these are the ones the tools were built for:

- *What happens if I delete a customer?* — walks the cascade before you find out
  in production.
- *Which templates have N+1 problems, worst first?* — `scan_templates` resolves
  the context from your views.
- *Can I ship this branch's migrations?* — `deploy_safety`, per migration, with
  the file and line numbers that have to change first.
- *Which pending migration will lock a table?* — `migration_risk`.

---

## Two servers for two projects

`django.setup()` mutates global state and cannot be undone, so one server
instance stays bound to one project. Register a second instance with a different
name and different environment variables:

```bash
claude mcp add chainsaw-shop  --env DJANGO_CHAINSAW_PROJECT_PATH=/srv/shop  ...
claude mcp add chainsaw-admin --env DJANGO_CHAINSAW_PROJECT_PATH=/srv/admin ...
```

Asking one instance to switch returns an explicit error rather than half-working
later.

---

## When something is wrong

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Missing environment variable(s)` | the client did not pass `env` | check the client config, not your shell |
| `ModuleNotFoundError: No module named 'myproject'` | `DJANGO_CHAINSAW_PROJECT_PATH` is not the directory the settings module is importable **from** | if settings live at `/srv/app/myproject/settings.py`, the path is `/srv/app` |
| `ModuleNotFoundError` for a third-party app | the interpreter lacks your project's dependencies | Option 1 |
| `Django is not importable from this interpreter` | same, one step earlier | Option 1 |
| `django.setup() failed ... ImproperlyConfigured` | settings need environment variables (`SECRET_KEY`, `DATABASE_URL`) | pass them in the client's `env` block too |
| Server connects, tools time out | `docker compose exec` without `-T` | add `-T` |
| `deploy_safety` finds nothing | every migration is already applied | it only looks at unapplied ones by design |
| `scan_templates` skips most templates | function-based views, which cannot be resolved statically | pass `root_models`, or check `templates_skipped_no_context` |

A database connection is never required. When one is reachable,
`migration_risk` and `deploy_safety` also know which migrations already ran and
skip them; without it they analyse everything on disk and say
`database_reachable: false`.

---

## Does it touch anything?

No. Every tool reads: the model graph in memory, template files, migration
files, and source files. Nothing writes, nothing runs your views, no queries are
executed against your data.

The one caveat is `django.setup()` itself. If your settings module has side
effects at import time, importing it has those side effects here as well.
