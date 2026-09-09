# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Serializers that expose more of a model than anybody decided to.

`fields = "__all__"` is a decision made once and then re-made silently by every
migration that follows. Add `password_reset_token` to the model and the API
starts returning it, with no diff on the serializer and nothing in review to
notice.

`exclude` has the same shape from the other direction: it lists what must not go
out, so anything added later goes out by default.

This walks the ModelSerializer subclasses, resolves what each one actually
exposes, and flags fields whose names suggest they were not meant to be public.
"""

from __future__ import annotations

import re
from typing import Any

from .discovery import binding_site, load_serializer_modules, serializer_label
from .django_env import ensure_django

# Names that are almost never meant to leave the server.
_SENSITIVE = [
    (re.compile(r"password", re.I), "credential"),
    (re.compile(r"secret", re.I), "credential"),
    (re.compile(r"token", re.I), "credential"),
    (re.compile(r"api_?key", re.I), "credential"),
    (re.compile(r"salt", re.I), "credential"),
    (re.compile(r"hash", re.I), "credential"),
    (re.compile(r"otp|two_?factor|mfa", re.I), "second factor"),
    (re.compile(r"ssn|social_security|tax_?id|vat_?id", re.I), "government identifier"),
    (re.compile(r"iban|bic|account_?number|card_?number", re.I), "financial identifier"),
    (re.compile(r"date_?of_?birth|birth_?date|dob\b", re.I), "personal data"),
    (re.compile(r"internal_?note|admin_?note|staff_?note", re.I), "internal only"),
    (re.compile(r"is_?staff|is_?superuser|permission", re.I), "privilege flag"),
    (re.compile(r"deleted|archived", re.I), "soft-delete flag, usually internal"),
]


def _classify(field_name: str) -> str | None:
    for pattern, label in _SENSITIVE:
        if pattern.search(field_name):
            return label
    return None


def _serializer_classes() -> list[Any]:
    try:
        from rest_framework import serializers as drf
    except ModuleNotFoundError:
        return []

    seen: set[int] = set()
    found: list[Any] = []

    def walk(cls: Any) -> None:
        for subclass in cls.__subclasses__():
            if id(subclass) in seen:
                continue
            seen.add(id(subclass))
            # A class the project binds nowhere is a subset built and used
            # inline: no file to send anybody to, and the finding against the
            # class it was built from already says the same thing.
            if binding_site(subclass) is not None:
                found.append(subclass)
            walk(subclass)

    walk(drf.ModelSerializer)
    return found


def serializer_exposure(include_safe: bool = False) -> dict[str, Any]:
    """Report what each ModelSerializer exposes, and what looks unintended.

    Args:
        include_safe: also list serializers with an explicit, clean field list.
    """
    config = ensure_django()
    # Django imports models.py, admin.py and whatever the URLconf
    # reaches. A serializer outside that is real, served, and was
    # previously invisible to a __subclasses__() walk.
    discovered = load_serializer_modules(config.project_path)

    classes = _serializer_classes()
    if not classes:
        return {
        "slow_imports": discovered.get("slow_imports", []),
            "rest_framework_installed": False,
            "serializer_count": 0,
            "findings": [],
            "note": (
                "djangorestframework is not importable from this interpreter, so "
                "there are no ModelSerializer subclasses to inspect. If the "
                "project does use DRF, the server is running in the wrong "
                "environment; see docs/usage.md."
            ),
        }

    findings: list[dict[str, Any]] = []
    safe: list[dict[str, Any]] = []

    for cls in classes:
        meta = getattr(cls, "Meta", None)
        if meta is None:
            continue
        model = getattr(meta, "model", None)
        if model is None:
            continue

        declared = getattr(meta, "fields", None)
        excluded = getattr(meta, "exclude", None)
        label = serializer_label(cls)
        model_label = model._meta.label

        concrete = [
            f.name for f in model._meta.get_fields()
            if getattr(f, "concrete", False)
        ]

        if declared == "__all__" or (declared is None and excluded is not None):
            exposed = [name for name in concrete if name not in (excluded or ())]
            mode = "__all__" if declared == "__all__" else f"exclude={list(excluded or ())}"
            sensitive = [
                {"field": name, "category": _classify(name)}
                for name in exposed
                if _classify(name)
            ]
            findings.append(
                {
                    "serializer": label,
                    "model": model_label,
                    "mode": mode,
                    "exposed_count": len(exposed),
                    "sensitive": sensitive,
                    "severity": "high" if sensitive else "medium",
                    "why": (
                        f"{mode} means every field on {model_label} is exposed, "
                        "including any added later. "
                        + (
                            f"{len(sensitive)} current field(s) look sensitive."
                            if sensitive
                            else "Nothing currently looks sensitive, but the next "
                            "migration decides that, not this file."
                        )
                    ),
                    "suggested": (
                        "list the fields explicitly: fields = ["
                        + ", ".join(repr(n) for n in exposed[:6])
                        + (", ...]" if len(exposed) > 6 else "]")
                    ),
                }
            )
            continue

        if isinstance(declared, (list, tuple)):
            sensitive = [
                {"field": name, "category": _classify(name)}
                for name in declared
                if isinstance(name, str) and _classify(name)
            ]
            if sensitive:
                findings.append(
                    {
                        "serializer": label,
                        "model": model_label,
                        "mode": "explicit",
                        "exposed_count": len(declared),
                        "sensitive": sensitive,
                        "severity": "high",
                        "why": (
                            "the field list is explicit, which is right, but it "
                            "names field(s) that look sensitive"
                        ),
                        "suggested": "confirm each of these is meant to be public",
                    }
                )
            elif include_safe:
                safe.append(
                    {"serializer": label, "model": model_label, "exposed_count": len(declared)}
                )

    findings.sort(key=lambda f: (f["severity"] != "high", f["serializer"]))
    return {
        "discovery": discovered,
        "rest_framework_installed": True,
        "serializer_count": len(classes),
        "finding_count": len(findings),
        "high_severity_count": sum(1 for f in findings if f["severity"] == "high"),
        "findings": findings,
        "explicit_and_clean": safe,
        "note": (
            "Name matching, not classification: a field called `token` might be "
            "a public share link and a field called `notes` might hold medical "
            "history. It reads Meta.fields and Meta.exclude on ModelSerializer "
            "subclasses that Python has imported, so a serializer in a module "
            "nothing imports at startup will not appear. Fields added by "
            "SerializerMethodField or declared on the class rather than in Meta "
            "are not resolved."
        ),
    }
