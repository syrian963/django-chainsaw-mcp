# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Sensitive fields on endpoints anybody can call.

Two facts that are each harmless on their own:

    CustomerExportSerializer exposes password_reset_token.
    PublicCustomerExport has permission_classes = [AllowAny].

The first is `serializer_exposure`'s finding and it is a judgement call: maybe
that serializer only ever feeds an admin-only export. The second is a Semgrep
rule and it is also a judgement call: maybe that view serves a product
catalogue. Put them together and there is nothing left to judge.

Neither check can make the connection alone, because the serializer does not
know which views use it and the view does not know what its serializer leaks.
That connection is the finding, and it is the whole of what this module does.

## The default is open

DRF's own default for `DEFAULT_PERMISSION_CLASSES` is `AllowAny`. A project
that never set `REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"]` has every view
without an explicit `permission_classes` open to the world, and none of those
views say so. That default is volunteered in the output before anything else,
because a project with it set wrong has this problem everywhere and the code
looks completely innocent.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

from .discovery import load_serializer_modules
from .django_env import ensure_django

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}
_VIEW_HINTS = ("View", "ViewSet")

_loaded_views: dict[str, dict[str, Any]] = {}


def _module_name(path: Path, root: Path) -> str:
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def load_view_modules(root: Path) -> dict[str, Any]:
    """Import every module declaring a View subclass, for the same reason
    `load_serializer_modules` exists: a view no URLconf reaches in this
    environment is not in `__subclasses__()`, and an unreached view is still
    a served view in production."""
    key = str(root)
    if key in _loaded_views:
        return _loaded_views[key]

    import sys

    imported: list[str] = []
    failed: list[dict[str, str]] = []
    for path in sorted(root.rglob("*.py")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        declares = any(
            isinstance(node, ast.ClassDef)
            and any(
                (base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", ""))
                .endswith(_VIEW_HINTS)
                for base in node.bases
            )
            for node in ast.walk(tree)
        )
        if not declares:
            continue
        module = _module_name(path, root)
        if not module or module in sys.modules:
            continue
        try:
            importlib.import_module(module)
            imported.append(module)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            failed.append({"module": module, "error": f"{type(exc).__name__}: {exc}"})

    _loaded_views[key] = {"imported": imported, "failed": failed}
    return _loaded_views[key]


def _default_permissions() -> tuple[list[Any], str]:
    """What applies when a view says nothing, and where that came from."""
    from django.conf import settings
    from rest_framework.settings import api_settings

    configured = getattr(settings, "REST_FRAMEWORK", {}).get("DEFAULT_PERMISSION_CLASSES")
    classes = list(api_settings.DEFAULT_PERMISSION_CLASSES)
    if configured:
        return classes, "REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES']"
    return classes, "DRF's built-in default, because the setting is not configured"


def _is_open(permission_classes: list[Any]) -> bool:
    from rest_framework.permissions import AllowAny

    if not permission_classes:
        return True
    return any(
        isinstance(p, type) and issubclass(p, AllowAny) for p in permission_classes
    )


def _names(classes: list[Any]) -> list[str]:
    return [getattr(c, "__name__", str(c)) for c in classes]


def _project_views() -> list[Any]:
    from rest_framework.generics import GenericAPIView
    from rest_framework.views import APIView

    out: list[Any] = []
    seen: set[int] = set()

    def walk(cls: Any) -> None:
        for sub in cls.__subclasses__():
            if id(sub) in seen:
                continue
            seen.add(id(sub))
            if not sub.__module__.startswith("rest_framework"):
                out.append(sub)
            walk(sub)

    walk(APIView)
    # GenericAPIView is under APIView, so the walk above already covers it;
    # the import is kept so a future DRF that moves it fails loudly here.
    assert issubclass(GenericAPIView, APIView)
    return out


def open_endpoints(include_unbounded: bool = True) -> dict[str, Any]:
    """Endpoints anyone can call, crossed with what their serializer exposes.

    Args:
        include_unbounded: also report open endpoints whose serializer uses
            `fields = "__all__"` or `exclude`, even with nothing sensitive on
            the model today - the next migration decides.
    """
    config = ensure_django()

    try:
        import rest_framework  # noqa: F401
    except ModuleNotFoundError:
        return {
            "rest_framework_installed": False,
            "findings": [],
            "note": "djangorestframework is not importable; there are no DRF endpoints to check.",
        }

    from .serializers import serializer_exposure

    load_serializer_modules(config.project_path)
    view_discovery = load_view_modules(config.project_path)

    exposure = serializer_exposure(include_safe=True)
    by_serializer: dict[str, dict[str, Any]] = {}
    for finding in exposure.get("findings", []):
        by_serializer[finding["serializer"]] = finding
    for entry in exposure.get("safe", []) if isinstance(exposure.get("safe"), list) else []:
        by_serializer.setdefault(entry.get("serializer", ""), entry)

    default_classes, default_source = _default_permissions()
    default_open = _is_open(default_classes)

    findings: list[dict[str, Any]] = []
    open_views: list[str] = []
    runtime_decided: list[str] = []
    protected = 0

    for view in _project_views():
        label = f"{view.__module__}.{view.__qualname__}"

        if "get_permissions" in view.__dict__:
            runtime_decided.append(label)
            continue

        if "permission_classes" in view.__dict__:
            classes, source = list(view.permission_classes), "set on the view"
        else:
            # Inherited from a project base class, or fallen through to the
            # default. Distinguishing the two matters: one is a decision, the
            # other is the absence of one.
            inherited = None
            for base in view.__mro__[1:]:
                if "permission_classes" in base.__dict__ and not base.__module__.startswith("rest_framework"):
                    inherited = base
                    break
            if inherited is not None:
                classes, source = list(inherited.permission_classes), f"inherited from {inherited.__name__}"
            else:
                classes, source = default_classes, default_source

        if not _is_open(classes):
            protected += 1
            continue
        open_views.append(label)

        serializer = getattr(view, "serializer_class", None)
        if serializer is None:
            continue
        serializer_label = f"{serializer.__module__}.{serializer.__qualname__}"
        exposed = by_serializer.get(serializer_label)
        if exposed is None:
            continue

        sensitive = exposed.get("sensitive") or []
        sensitive_names = [s["field"] if isinstance(s, dict) else str(s) for s in sensitive]
        unbounded = exposed.get("mode") not in (None, "explicit")

        if not sensitive_names and not (unbounded and include_unbounded):
            continue

        findings.append({
            "view": label,
            "permission_classes": _names(classes),
            "permission_source": source,
            "serializer": serializer_label,
            "model": exposed.get("model"),
            "mode": exposed.get("mode"),
            "sensitive_fields": sensitive_names,
            "severity": "critical" if sensitive_names else "medium",
            "why": (
                f"anyone can call this and the response includes {', '.join(sensitive_names)}"
                if sensitive_names else
                f"anyone can call this and the serializer exposes every field on "
                f"{exposed.get('model')} ({exposed.get('mode')}), so the next migration "
                "decides what leaks"
            ),
            "fix": (
                "set permission_classes on the view, or if it must be public, give it a "
                "serializer with an explicit field list that omits "
                + (", ".join(sensitive_names) if sensitive_names else "anything internal")
            ),
        })

    order = {"critical": 0, "high": 1, "medium": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["view"]))

    return {
        "rest_framework_installed": True,
        "default_permission_classes": _names(default_classes),
        "default_permission_source": default_source,
        "default_is_open": default_open,
        "views_checked": len(open_views) + protected + len(runtime_decided),
        "open_view_count": len(open_views),
        "open_views": sorted(open_views),
        "protected_view_count": protected,
        "views_deciding_at_runtime": sorted(runtime_decided),
        "finding_count": len(findings),
        "critical_count": sum(1 for f in findings if f["severity"] == "critical"),
        "findings": findings,
        "view_discovery": view_discovery,
        "note": (
            (
                f"The default permission is open ({', '.join(_names(default_classes))}, "
                f"{default_source}). Every view without its own permission_classes is "
                "therefore public, and none of them say so. That is the single "
                "highest-value thing to know here.\n\n"
                if default_open else ""
            )
            + "A finding needs both halves: a view anyone can call, and a serializer "
            "that exposes something sensitive - or everything, via __all__ or "
            "exclude, in which case the next migration decides what leaks. Views "
            "that override get_permissions() choose at runtime and are listed, not "
            "judged. A view whose serializer is chosen in get_serializer_class() is "
            "checked against serializer_class only, which may be the wrong one. "
            "authentication_classes are not consulted: a view with IsAuthenticated "
            "and no authenticators rejects everyone, which is a different bug."
        ),
    }
