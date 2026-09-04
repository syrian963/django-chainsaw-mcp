"""MCP server exposing read-only introspection and analysis of a Django project.

MCPServer is the decorator API of the official MCP Python SDK. In SDK v1 the
same class was called FastMCP; it was renamed in v2. Neither has anything to do
with FastAPI, despite the old name.

Tools are for actions and for anything parameterised. Resources are for stable,
addressable data: the model graph does not change between calls, so it is
exposed as a resource rather than as another tool.
"""

from __future__ import annotations

import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from .cascade import delete_impact as _delete_impact
from .deploy_safety import deploy_safety as _deploy_safety
from .django_env import DjangoBootError, ensure_django
from .introspect import list_models as _list_models
from .migrations import migration_risk as _migration_risk
from .nplusone import analyse_template as _analyse_template
from .scan import scan_templates as _scan_templates

mcp = MCPServer("django-chainsaw")


def _guard(fn, *args, **kwargs) -> dict[str, Any]:
    """Turn expected failures into readable output instead of a dead server."""
    try:
        return fn(*args, **kwargs)
    except (DjangoBootError, ValueError) as exc:
        return {"ok": False, "error": str(exc)}


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
        app_label: Restrict to one app, e.g. "shop". Omit for all apps.
        include_fields: Set False for a short overview without field details.
    """
    return _guard(_list_models, app_label=app_label, include_fields=include_fields)


@mcp.tool()
def delete_impact(model_label: str, max_depth: int = 6) -> dict[str, Any]:
    """Show what deleting one row of a model would take with it.

    Follows on_delete across the whole model graph: which models lose rows
    through CASCADE, which PROTECT relations would block the delete, and which
    fields get set to NULL. Reads the graph only, never the database.

    Args:
        model_label: "app_label.ModelName", e.g. "shop.Customer".
        max_depth: how far to follow chained cascades.
    """
    return _guard(_delete_impact, model_label=model_label, max_depth=max_depth)


@mcp.tool()
def find_n_plus_one(template_path: str, root_models: dict[str, str]) -> dict[str, Any]:
    """Find relation traversals in a template that each cost a query.

    Resolves attribute chains against the real model graph and flags the ones
    that cross a relation inside a loop, which is where N+1 queries come from.
    Reports candidates: whether a crossing really costs a query depends on the
    queryset in the view, which this does not read.

    Args:
        template_path: path to the template file.
        root_models: context variable to model label, e.g.
            {"orders": "shop.Order"}. Loop variables inherit from these.
    """
    return _guard(_analyse_template, template_path=template_path, root_models=root_models)


@mcp.tool()
def scan_templates(
    template_root: str | None = None,
    project_root: str | None = None,
    root_models: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the N+1 analysis over every template in a directory.

    find_n_plus_one needs a context map per template. This resolves it instead
    from class-based views that declare template_name together with model or
    queryset, so a whole project can be scanned without typing anything.

    Args:
        template_root: template directory. Defaults to the project path.
        project_root: where to look for views. Defaults to the project path.
        root_models: context applied to every template, for names no view supplies.
    """
    return _guard(
        _scan_templates,
        template_root=template_root,
        project_root=project_root,
        root_models=root_models,
    )


@mcp.tool()
def deploy_safety(search_path: str | None = None, max_hits_per_symbol: int = 25) -> dict[str, Any]:
    """Is a pending destructive migration safe to deploy yet?

    A migration linter says RemoveField is backward incompatible, always. This
    answers the question that actually decides the deploy: has the code caught
    up? For every unapplied migration that removes or renames a field, model,
    index or constraint, the source tree is searched for code that still refers
    to it, and each one comes back either "blocking" with file and line numbers,
    or "clear".

    Args:
        search_path: directory to scan. Defaults to the configured project path.
        max_hits_per_symbol: stop after this many references per symbol.
    """
    return _guard(_deploy_safety, search_path=search_path, max_hits_per_symbol=max_hits_per_symbol)


@mcp.tool()
def migration_risk(include_applied: bool = False) -> dict[str, Any]:
    """Rate migrations by what they do to a live database.

    Flags operations that block writes, rewrite a table, or break the code that
    is still running during a rolling deploy.

    Args:
        include_applied: also classify migrations that already ran.
    """
    return _guard(_migration_risk, include_applied=include_applied)


@mcp.resource("django://models", mime_type="application/json")
def model_graph() -> str:
    """The full model graph. Stable between calls, so a resource, not a tool."""
    return json.dumps(_guard(_list_models, app_label=None, include_fields=True), indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
