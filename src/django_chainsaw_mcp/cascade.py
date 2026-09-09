# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""What happens if you delete one row of this model.

Answers the question statically, from the model graph, without touching the
database. Django decides deletion behaviour per relation through on_delete, and
the effect is transitive: a CASCADE can pull in a whole subtree that nobody
sees when looking at a single model.
"""

from __future__ import annotations

from typing import Any

from .django_env import ensure_django

# What each on_delete handler does to the *referencing* row.
_EFFECT = {
    "CASCADE": "deleted",
    "PROTECT": "blocks",
    "RESTRICT": "blocks",
    "SET_NULL": "nulled",
    "SET_DEFAULT": "reset",
    "SET": "reset",
    "DO_NOTHING": "untouched",
}


def _handler_name(on_delete: Any) -> str:
    if on_delete is None:
        return "UNKNOWN"
    # functools.partial for SET(...) has no __name__.
    name = getattr(on_delete, "__name__", None)
    if name:
        return name
    func = getattr(on_delete, "func", None)
    return getattr(func, "__name__", "SET") if func else "UNKNOWN"


def _incoming_relations(model: Any) -> list[dict[str, Any]]:
    """Every concrete FK/O2O pointing at this model, with its on_delete."""
    found = []
    for rel in model._meta.related_objects:
        field = rel.field
        # on_delete lives on the relation descriptor, not on the field itself.
        # Reading field.on_delete silently yields None, which used to make every
        # relation come back as UNKNOWN and the whole analysis empty.
        handler = _handler_name(getattr(field.remote_field, "on_delete", None))
        found.append(
            {
                "from_model": rel.related_model._meta.label,
                "via_field": field.name,
                "on_delete": handler,
                "effect": _EFFECT.get(handler, "unknown"),
                "nullable": bool(getattr(field, "null", False)),
            }
        )
    found.sort(key=lambda item: (item["from_model"], item["via_field"]))
    return found


def delete_impact(model_label: str, max_depth: int = 6) -> dict[str, Any]:
    """Walk the CASCADE graph outward from one model.

    Args:
        model_label: e.g. "shop.Customer" (app_label.ModelName).
        max_depth: how far to follow chained cascades.
    """
    ensure_django()
    from django.apps import apps

    try:
        root = apps.get_model(model_label)
    except (LookupError, ValueError) as exc:
        known = sorted(m._meta.label for m in apps.get_models())
        raise ValueError(
            f"Unknown model '{model_label}'. Expected app_label.ModelName. "
            f"Known: {', '.join(known[:40])}{' ...' if len(known) > 40 else ''}"
        ) from exc

    cascades: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    nulled: list[dict[str, Any]] = []
    untouched: list[dict[str, Any]] = []
    truncated = False

    # (label, depth, path) breadth-first, tracking visited to survive cycles
    # such as a self-referencing parent FK.
    queue: list[tuple[Any, int, list[str]]] = [(root, 0, [root._meta.label])]
    seen: set[str] = set()

    while queue:
        model, depth, path = queue.pop(0)
        if depth >= max_depth:
            truncated = True
            continue

        for rel in _incoming_relations(model):
            record = dict(rel, depth=depth + 1, path=" -> ".join(path))
            effect = rel["effect"]

            if effect == "deleted":
                cascades.append(record)
                key = rel["from_model"]
                if key in seen:
                    record["note"] = "already reached on another path, not expanded again"
                    continue
                seen.add(key)
                child = apps.get_model(rel["from_model"])
                queue.append((child, depth + 1, [*path, key]))
            elif effect == "blocks":
                blockers.append(record)
            elif effect in {"nulled", "reset"}:
                nulled.append(record)
            else:
                untouched.append(record)

    m2m = [
        {
            "field": f.name,
            "to": f.related_model._meta.label,
            "through": f.remote_field.through._meta.label,
        }
        for f in root._meta.local_many_to_many
    ]

    return {
        "model": root._meta.label,
        "summary": (
            f"{len(cascades)} model(s) lose rows, "
            f"{len(blockers)} can block the delete, "
            f"{len(nulled)} get fields cleared"
        ),
        "cascades": cascades,
        "blocked_by": blockers,
        "fields_cleared": nulled,
        "left_untouched": untouched,
        "many_to_many_through_rows_removed": m2m,
        "max_depth_reached": truncated,
        "note": (
            "Static analysis of on_delete over the model graph. It does not "
            "count rows and does not run custom delete() overrides or "
            "pre_delete/post_delete signals, which can delete more."
        ),
    }
