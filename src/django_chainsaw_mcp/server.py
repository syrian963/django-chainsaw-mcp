"""MCP server exposing read-only introspection of a Django project.

MCPServer is the decorator API of the official MCP Python SDK. In SDK v1 the
same class was called FastMCP; it was renamed in v2. Neither has anything to do
with FastAPI, despite the old name.
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from .django_env import DjangoBootError, ensure_django
from .introspect import list_models as _list_models

mcp = MCPServer("django-chainsaw")


@mcp.tool()
def project_info() -> dict[str, Any]:
    """Check that the target Django project loads, and report what it is.

    Run this first when something is not working. It is the smallest call that
    proves both halves of the setup: the MCP transport and the Django boot.
    """
    try:
        config = ensure_django()
    except DjangoBootError as exc:
        return {"ok": False, "error": str(exc)}

    import django
    from django.apps import apps
    from django.conf import settings

    return {
        "ok": True,
        "django_version": django.get_version(),
        "settings_module": config.settings_module,
        "project_path": str(config.project_path),
        "debug": settings.DEBUG,
        "installed_apps": [cfg.label for cfg in apps.get_app_configs()],
        "database_engines": {
            alias: conf.get("ENGINE", "") for alias, conf in settings.DATABASES.items()
        },
    }


@mcp.tool()
def list_models(app_label: str | None = None, include_fields: bool = True) -> dict[str, Any]:
    """List the project's models with their fields and relations.

    Args:
        app_label: Restrict to one app, e.g. "catalogue". Omit for all apps.
        include_fields: Set False for a short overview without field details.
    """
    try:
        return _list_models(app_label=app_label, include_fields=include_fields)
    except (DjangoBootError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
