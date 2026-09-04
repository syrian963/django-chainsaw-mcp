# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only introspection of a booted Django project."""

from __future__ import annotations

from typing import Any

from .django_env import ensure_django


def _cardinality(field: Any) -> str:
    """Name the relation shape.

    The four flags are set on both forward fields and reverse descriptors, so
    one_to_many has to be checked as well. Leaving it out makes every reverse
    accessor report as unknown, which is wrong in exactly the place that
    matters: reverse relations are the ones that produce N+1 queries.
    """
    if field.many_to_many:
        return "ManyToMany"
    if field.one_to_one:
        return "OneToOne"
    if field.many_to_one:
        return "ManyToOne"
    if field.one_to_many:
        return "OneToMany"
    return "Unknown"


def _field_entry(field: Any) -> dict[str, Any]:
    # Reverse descriptors are ForeignObjectRel, not Field, and carry a
    # different attribute set. is_relation is true for both.
    is_reverse = bool(field.is_relation and getattr(field, "auto_created", False) and not field.concrete)

    entry: dict[str, Any] = {
        "name": field.name,
        "type": type(field).__name__,
        "concrete": bool(getattr(field, "concrete", False)),
    }

    if not is_reverse:
        for attr in ("primary_key", "unique", "db_index", "null"):
            if getattr(field, attr, False):
                entry[attr] = True

    if field.is_relation:
        target = field.related_model
        relation: dict[str, Any] = {
            "kind": _cardinality(field),
            "direction": "reverse" if is_reverse else "forward",
            "to": target._meta.label if target is not None else None,
        }

        remote = getattr(field, "remote_field", None)
        on_delete = getattr(remote, "on_delete", None) if remote is not None else None
        if on_delete is not None:
            relation["on_delete"] = getattr(on_delete, "__name__", str(on_delete))

        if not is_reverse and remote is not None:
            relation["related_name"] = getattr(remote, "related_name", None)

        entry["relation"] = relation

    return entry


def list_models(app_label: str | None = None, include_fields: bool = True) -> dict[str, Any]:
    """Return every concrete model in the project, optionally one app only."""
    config = ensure_django()
    from django.apps import apps

    if app_label:
        try:
            app_configs = [apps.get_app_config(app_label)]
        except LookupError as exc:
            known = sorted(cfg.label for cfg in apps.get_app_configs())
            raise ValueError(f"Unknown app label '{app_label}'. Known labels: {', '.join(known)}") from exc
    else:
        app_configs = list(apps.get_app_configs())

    result: list[dict[str, Any]] = []
    for app_config in app_configs:
        for model in app_config.get_models():
            meta = model._meta
            entry: dict[str, Any] = {
                "label": meta.label,
                "app_label": meta.app_label,
                "object_name": meta.object_name,
                "db_table": meta.db_table,
                "abstract": meta.abstract,
                "proxy": meta.proxy,
                "field_count": len(meta.get_fields()),
            }
            if include_fields:
                entry["fields"] = [_field_entry(f) for f in meta.get_fields()]
            result.append(entry)

    result.sort(key=lambda item: item["label"])
    return {
        "settings_module": config.settings_module,
        "project_path": str(config.project_path),
        "model_count": len(result),
        "models": result,
    }
