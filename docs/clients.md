# Wiring it into an AI tool

Every MCP client launches a process and speaks stdio to it. So the entry is
always the same three things: a command, its arguments, and the environment.

```
command  /srv/app/.venv/bin/python
args     ["-m", "django_chainsaw_mcp.server"]
env      DJANGO_CHAINSAW_PROJECT_PATH, DJANGO_CHAINSAW_SETTINGS_MODULE
```

**Use the absolute path to the interpreter that can import your project.** These
tools do not inherit your shell, so a bare `python` or `uv` usually is not found,
and even when it is, it is rarely the environment your Django project lives in.
See [`usage.md`](usage.md) for why that matters.

## The root key is different in every tool

This is the thing that wastes an afternoon. The structure is identical; the key
wrapping it is not.

| Tool | Config file | Root key |
| --- | --- | --- |
| Claude Code | `claude mcp add`, or `.mcp.json` in the project | `mcpServers` |
| Claude Desktop | `claude_desktop_config.json` | `mcpServers` |
| Cursor | `.cursor/mcp.json`, or global `~/.cursor/mcp.json` | `mcpServers` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | `mcpServers` |
| VS Code | `.vscode/mcp.json` | **`servers`** |
| Zed | `settings.json` | **`context_servers`** |

Copying a working config from Claude Desktop into VS Code and wondering why
nothing appears is the single most common mistake, and it is only ever this.

## Claude Code

```bash
claude mcp add django-chainsaw --scope local \
  --env DJANGO_CHAINSAW_PROJECT_PATH=/srv/app \
  --env DJANGO_CHAINSAW_SETTINGS_MODULE=myproject.settings \
  -- /srv/app/.venv/bin/python -m django_chainsaw_mcp.server

claude mcp list     # django-chainsaw ... ✔ Connected
```

`--scope local` keeps it to the current project, `--scope user` applies it
everywhere, `--scope project` writes `.mcp.json` for the whole team to share.

The CLI is only a faster way to write the same JSON; nothing about it is
special.

## Cursor and Windsurf

Same shape, different file.

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

Cursor reads `.cursor/mcp.json` in the project and `~/.cursor/mcp.json`
globally. Windsurf reads `~/.codeium/windsurf/mcp_config.json`.

## VS Code

`.vscode/mcp.json`, and note the key:

```json
{
  "servers": {
    "django-chainsaw": {
      "type": "stdio",
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

## Zed

```json
{
  "context_servers": {
    "django-chainsaw": {
      "command": {
        "path": "/srv/app/.venv/bin/python",
        "args": ["-m", "django_chainsaw_mcp.server"],
        "env": {
          "DJANGO_CHAINSAW_PROJECT_PATH": "/srv/app",
          "DJANGO_CHAINSAW_SETTINGS_MODULE": "myproject.settings"
        }
      }
    }
  }
}
```

## Anything else

Any client that can launch a process with arguments and environment variables
can drive this. If it cannot, the CLI does the same work with an exit code, and
that path needs no client at all:

```bash
django-chainsaw deploy-safety
django-chainsaw tenancy --tenant-root shop.Customer --since main
```

## Two projects, two entries

`django.setup()` mutates global state and cannot be undone, so one server
instance stays bound to one project. Register it twice with different names and
different environment variables rather than trying to switch:

```json
{
  "mcpServers": {
    "chainsaw-shop":  { "command": "...", "env": { "DJANGO_CHAINSAW_PROJECT_PATH": "/srv/shop",  "...": "" } },
    "chainsaw-admin": { "command": "...", "env": { "DJANGO_CHAINSAW_PROJECT_PATH": "/srv/admin", "...": "" } }
  }
}
```

## Project runs in Docker

The client launches the container command instead of a local binary:

```json
{
  "command": "docker",
  "args": ["compose", "exec", "-T", "web", "python", "-m", "django_chainsaw_mcp.server"],
  "env": {
    "DJANGO_CHAINSAW_PROJECT_PATH": "/app",
    "DJANGO_CHAINSAW_SETTINGS_MODULE": "myproject.settings"
  }
}
```

**`-T` is not optional.** Without it `docker compose exec` allocates a TTY and
the stdio protocol breaks in a way that looks like the server hanging.

Paths are the container's: `/app`, not the host path, and file locations in the
output refer to the container filesystem.

## Checking it worked

Ask for `project_info` first, in any client. It is the smallest call that proves
both halves: that the transport is up, and that the Django project loaded.

```
project_info ok: True  django: 5.1.4  settings: myproject.settings
```

If that is right everything else will work. If it is wrong, nothing else will,
and the error text says which half failed. The table in
[`usage.md`](usage.md#when-something-is-wrong) maps each message to its cause.

## No remote endpoint, on purpose

Hosted, OAuth-secured MCP endpoints are increasingly the norm for services like
GitHub or Linear, and they make sense there: the data already lives on somebody
else's server.

This one reads your source code and your model definitions. Running it locally
means none of that leaves the machine, and there is nothing to authenticate
because there is no server to reach. The CLI covers CI, which is the only other
place it needs to run.
