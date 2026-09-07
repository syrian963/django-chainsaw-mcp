# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What the API promises, and whether this branch breaks it.

Removing a field from a serializer is a one line diff that reads as tidying up.
Somewhere there is a mobile client on a version people have not updated, and it
reads that field. Nothing in the pull request says so.

The tools that catch this today all need the application running.
`drf-api-checker` records real responses during a test run and compares later
ones against them, so it only covers what the tests exercise. Diffing OpenAPI
schemas needs the schema generated, which needs the app booted and the
generator configured.

The contract is already in the class definitions. A `ModelSerializer` resolves
its fields at import time: names, types, whether each is read only, required,
nullable, and what a nested serializer expands to. That is the promise, and it
can be captured, committed, and compared without sending a request.

The classification is where the value is. Not every change is equal:

    removing a read field       breaks every existing reader
    adding a required write     breaks every existing writer
    adding an optional field    breaks nobody
    widening nullability        breaks readers that assumed non-null
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BREAKING = "breaking"
UNREADABLE = "unreadable"
RISKY = "risky"
ADDITIVE = "additive"
NEUTRAL = "neutral"

CONTRACT_FILE = ".django-chainsaw-contract.json"


def _describe_field(name: str, field: Any, depth: int, max_depth: int) -> dict[str, Any]:
    from rest_framework import serializers as drf

    entry: dict[str, Any] = {
        "type": type(field).__name__,
        "read_only": bool(getattr(field, "read_only", False)),
        "write_only": bool(getattr(field, "write_only", False)),
        "required": bool(getattr(field, "required", False)),
        "allow_null": bool(getattr(field, "allow_null", False)),
    }

    # Reading .choices off a related field iterates its queryset, which opens a
    # database connection. The whole point of resolving the contract statically
    # is that it works on a checkout with no database, so the related fields
    # report the model they point at instead, which is just as stable and is
    # already known.
    # ManyRelatedField is not a RelatedField subclass, it wraps one, and it has
    # its own .choices that iterates just as eagerly. Both have to be covered.
    if isinstance(field, (drf.RelatedField, drf.ManyRelatedField)):
        related = getattr(field, "child_relation", field)
        queryset = getattr(related, "queryset", None)
        model = getattr(queryset, "model", None)
        if model is not None:
            entry["relates_to"] = model._meta.label
    else:
        choices = getattr(field, "choices", None)
        if choices:
            try:
                entry["choices"] = sorted(str(c) for c in choices)
            except TypeError:
                pass

    max_length = getattr(field, "max_length", None)
    if max_length:
        entry["max_length"] = max_length

    child = getattr(field, "child", None)
    nested = child if isinstance(child, drf.BaseSerializer) else field
    if isinstance(nested, drf.BaseSerializer) and depth < max_depth:
        entry["many"] = child is not None
        entry["serializer"] = f"{type(nested).__module__}.{type(nested).__qualname__}"
        try:
            entry["fields"] = {
                inner_name: _describe_field(inner_name, inner, depth + 1, max_depth)
                for inner_name, inner in nested.get_fields().items()
            }
        except Exception:
            entry["fields"] = {}
            entry["unreadable"] = True

    return entry


def contract(max_depth: int = 3) -> dict[str, Any]:
    """The shape every ModelSerializer currently promises."""
    from .discovery import (
        dynamic_serializer_views,
        load_serializer_modules,
        serializers_used_by_views,
    )
    from .django_env import ensure_django

    config = ensure_django()
    # Django imports models.py, admin.py and whatever the URLconf
    # reaches. A serializer outside that is real, served, and was
    # previously invisible to a __subclasses__() walk.
    discovered = load_serializer_modules(config.project_path)

    try:
        from rest_framework import serializers as drf
    except ModuleNotFoundError:
        return {
            "rest_framework_installed": False,
            "serializers": {},
            "note": "djangorestframework is not importable; there is no contract to capture.",
        }

    seen: set[int] = set()
    classes: list[Any] = []

    def walk(cls: Any) -> None:
        for subclass in cls.__subclasses__():
            if id(subclass) in seen:
                continue
            seen.add(id(subclass))
            classes.append(subclass)
            walk(subclass)

    walk(drf.ModelSerializer)

    # A serializer no view names is still in the contract, so removing it gets
    # reported as breaking when nobody was reading it. Labelling which views
    # serve what turns that from a wrong verdict into a qualified one.
    served = serializers_used_by_views(config.project_path)
    dynamic = dynamic_serializer_views(config.project_path)

    captured: dict[str, Any] = {}
    unreadable: list[str] = []

    for cls in sorted(classes, key=lambda c: f"{c.__module__}.{c.__qualname__}"):
        name = f"{cls.__module__}.{cls.__qualname__}"
        meta = getattr(cls, "Meta", None)
        model = getattr(meta, "model", None)
        try:
            fields = cls().get_fields()
        except Exception as exc:
            unreadable.append(f"{name}: {type(exc).__name__}")
            continue

        # A to_representation override rewrites the output after the fields
        # have had their say, so the captured shape is the shape before that
        # runs. It cannot be read, but its presence can, and a contract with an
        # unannounced rewrite sitting on top of it is a contract with a hole.
        # DRF defines get_fields and to_representation on its own base classes,
        # so walking the whole MRO flagged every serializer in existence. Only
        # an override written in this project reshapes anything.
        reshapes = sorted(
            m for m in ("to_representation", "get_fields")
            if any(
                m in klass.__dict__
                and not klass.__module__.startswith("rest_framework")
                for klass in cls.__mro__
            )
        )

        captured[name] = {
            "model": model._meta.label if model is not None else None,
            "reshapes_output": reshapes,
            "served_by": sorted(served.get(name, [])),
            "fields": {
                field_name: _describe_field(field_name, field, 0, max_depth)
                for field_name, field in fields.items()
            },
        }

    return {
        "rest_framework_installed": True,
        "captured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "serializer_count": len(captured),
        "serializers": captured,
        "unreadable": unreadable,
        # If no view anywhere declares a serializer_class, every serializer is
        # "unserved" and the field means nothing. Saying so is the difference
        # between an empty answer and a wrong one.
        "attribution_possible": bool(served),
        "unserved": sorted(_unserved(captured, served)) if served else [],
        "reshaped": sorted(n for n in captured if captured[n]["reshapes_output"]),
        "views_choosing_at_runtime": dynamic,
        "discovery": discovered,
        "note": (
            "Resolved from the serializer classes Python has imported, so a "
            "serializer in a module nothing imports at startup is not in here. "
            "A SerializerMethodField appears with its declared type and nothing "
            "about what it returns, because that is only knowable at runtime. "
            "No view in this project declares a serializer_class, so which "
            "serializer serves what could not be determined at all and "
            "'unserved' is empty rather than listing everything - an empty "
            "list here means the question could not be asked. "
            if not served else
            "A serializer listed under 'unserved' is named by no view's "
            "serializer_class, so a change to it probably reaches nobody - but "
            "only probably, because a view in 'views_choosing_at_runtime' picks "
            "its serializer in get_serializer_class() and could return any of "
            "them. A serializer listed under 'reshaped' overrides "
            "to_representation or get_fields, so what it actually returns is "
            "decided at runtime and the captured shape is only the shape going "
            "in - that one is a real hole and it is named rather than papered "
            "over."
        ),
    }


def _nested_serializers(fields: dict[str, Any]) -> set[str]:
    out: set[str] = set()
    for entry in fields.values():
        name = entry.get("serializer")
        if name:
            out.add(name)
        inner = entry.get("fields")
        if isinstance(inner, dict):
            out |= _nested_serializers(inner)
    return out


def _unserved(captured: dict[str, Any], served: dict[str, list[str]]) -> set[str]:
    """Serializers no view reaches, directly or through nesting.

    OrderLineSerializer is named by no view and is rendered on every order
    response, because OrderDetailSerializer embeds it. Calling that "unserved"
    would invite somebody to delete a field a client is reading, which is the
    exact failure this whole check exists to prevent.
    """
    reachable = {name for name in captured if served.get(name)}
    changed = True
    while changed:
        changed = False
        for name in list(reachable):
            entry = captured.get(name)
            if entry is None:
                continue
            for nested in _nested_serializers(entry.get("fields", {})):
                if nested in captured and nested not in reachable:
                    reachable.add(nested)
                    changed = True
    return set(captured) - reachable


def _compare_field(path: str, old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []

    if old.get("type") != new.get("type"):
        changes.append(
            {
                "path": path,
                "kind": BREAKING,
                "change": f"type {old.get('type')} -> {new.get('type')}",
                "why": "a client parsing the old type may fail on the new one",
            }
        )

    # A field that was readable and is now write-only has vanished from every
    # response, which is the same thing as removing it for a reader.
    if not old.get("write_only") and new.get("write_only"):
        changes.append(
            {
                "path": path,
                "kind": BREAKING,
                "change": "became write_only",
                "why": "it no longer appears in responses, so readers lose it",
            }
        )

    if not old.get("required") and new.get("required") and not new.get("read_only"):
        changes.append(
            {
                "path": path,
                "kind": BREAKING,
                "change": "became required",
                "why": "every existing writer that omits it now gets a 400",
            }
        )

    if not old.get("allow_null") and new.get("allow_null"):
        changes.append(
            {
                "path": path,
                "kind": RISKY,
                "change": "became nullable",
                "why": "readers that assumed a value will now sometimes get null",
            }
        )

    if old.get("required") and not new.get("required"):
        changes.append(
            {
                "path": path,
                "kind": ADDITIVE,
                "change": "no longer required",
                "why": "relaxing a requirement breaks nobody",
            }
        )

    old_choices, new_choices = old.get("choices"), new.get("choices")
    if old_choices and new_choices:
        removed = sorted(set(old_choices) - set(new_choices))
        if removed:
            changes.append(
                {
                    "path": path,
                    "kind": BREAKING,
                    "change": f"choices removed: {', '.join(removed)}",
                    "why": "a client sending a removed value now gets a 400",
                }
            )

    old_length, new_length = old.get("max_length"), new.get("max_length")
    if old_length and new_length and new_length < old_length:
        changes.append(
            {
                "path": path,
                "kind": BREAKING,
                "change": f"max_length {old_length} -> {new_length}",
                "why": "values that were accepted are now rejected",
            }
        )

    old_nested = old.get("fields")
    new_nested = new.get("fields")
    if isinstance(old_nested, dict) and isinstance(new_nested, dict):
        changes.extend(_compare_fields(path, old_nested, new_nested))

    return changes


def _compare_fields(prefix: str, old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []

    for name in sorted(set(old) - set(new)):
        entry = old[name]
        changes.append(
            {
                "path": f"{prefix}.{name}",
                "kind": ADDITIVE if entry.get("write_only") else BREAKING,
                "change": "removed",
                "why": (
                    "it was write only, so no reader depended on it"
                    if entry.get("write_only")
                    else "every client reading this field stops getting it"
                ),
            }
        )

    for name in sorted(set(new) - set(old)):
        entry = new[name]
        required_write = entry.get("required") and not entry.get("read_only")
        changes.append(
            {
                "path": f"{prefix}.{name}",
                "kind": BREAKING if required_write else ADDITIVE,
                "change": "added, required" if required_write else "added",
                "why": (
                    "an existing writer that does not send it now gets a 400"
                    if required_write
                    else "a new optional field breaks nobody"
                ),
            }
        )

    for name in sorted(set(old) & set(new)):
        changes.extend(_compare_field(f"{prefix}.{name}", old[name], new[name]))

    return changes


def diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Classify what changed between two captured contracts."""
    old_serializers = old.get("serializers", {})
    new_serializers = new.get("serializers", {})

    # A serializer that raised on instantiation is absent from the capture, and
    # calling that "removed" would be a confident wrong answer: the class is
    # still there, it just no longer resolves. Usually it names a model field
    # that does not exist. Whatever it is, its contract is unknown, and saying
    # so is the only honest option.
    unreadable_reason = {
        entry.split(":", 1)[0].strip(): entry.split(":", 1)[1].strip()
        for entry in new.get("unreadable", [])
        if ":" in entry
    }

    changes: list[dict[str, Any]] = []

    for name in sorted(set(old_serializers) - set(new_serializers)):
        if name in unreadable_reason:
            changes.append(
                {
                    "path": name,
                    "kind": UNREADABLE,
                    "change": f"no longer resolves ({unreadable_reason[name]})",
                    "why": (
                        "the class is still defined but instantiating it raised, "
                        "so its contract could not be compared. Fix that first; "
                        "until then this branch's effect on clients is unknown"
                    ),
                }
            )
            continue
        changes.append(
            {
                "path": name,
                "kind": BREAKING,
                "change": "serializer removed",
                "why": "whatever endpoint used it either changed shape or is gone",
            }
        )

    for name in sorted(set(new_serializers) - set(old_serializers)):
        changes.append(
            {
                "path": name,
                "kind": ADDITIVE,
                "change": "serializer added",
                "why": "new surface, nothing depended on it yet",
            }
        )

    for name in sorted(set(old_serializers) & set(new_serializers)):
        changes.extend(
            _compare_fields(
                name,
                old_serializers[name].get("fields", {}),
                new_serializers[name].get("fields", {}),
            )
        )

    order = {BREAKING: 0, UNREADABLE: 1, RISKY: 2, ADDITIVE: 3, NEUTRAL: 4}
    changes.sort(key=lambda c: (order.get(c["kind"], 9), c["path"]))

    counts: dict[str, int] = {}
    for change in changes:
        counts[change["kind"]] = counts.get(change["kind"], 0) + 1

    return {
        "baseline_captured_at": old.get("captured_at"),
        "change_count": len(changes),
        "by_kind": counts,
        "breaking_count": counts.get(BREAKING, 0),
        "unreadable_count": counts.get(UNREADABLE, 0),
        "changes": changes,
        "note": (
            "Breaking means an existing client stops working: a field it reads "
            "disappears, a field it omits becomes required, a type or a "
            "constraint narrows. Risky means it still parses but the values may "
            "surprise it, which is mostly nullability. Additive means nobody "
            "notices. The classification assumes clients that read what they "
            "were given and send what they always sent, which is the assumption "
            "every API version policy is built on."
        ),
    }


def load_snapshot(path: str | Path) -> dict[str, Any] | None:
    file_path = Path(path)
    if not file_path.is_file():
        return None
    text = file_path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Contract snapshot is not readable JSON: {file_path} ({exc})") from exc


def write_snapshot(path: str | Path, captured: dict[str, Any]) -> dict[str, Any]:
    file_path = Path(path)
    file_path.write_text(json.dumps(captured, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "snapshot": str(file_path),
        "serializers": captured.get("serializer_count", 0),
        "note": (
            "Commit this. It is the shape your API promised at this point, and "
            "the only way a later branch can be told it broke something."
        ),
    }
