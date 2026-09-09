# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Everything known about one model, and the risks only visible when combined.

Each analyser here answers one question well. A developer meeting a model for
the first time has a different question, and it is the one an assistant gets
asked: *tell me about Order.* Answering it properly means running six checks and
reading six reports.

The more interesting half is that some defects exist only in the overlap and no
single analyser can see them:

  - A model is tenant-scoped, has queries that do not scope, **and** is
    serialised with `fields = "__all__"`. Each finding alone is a warning. All
    three together is a complete path from a URL to another customer's row.
  - Deleting one row cascades into a model that belongs to a *different* owner
    path. No cascade check looks at ownership; no ownership check looks at
    cascades.
  - A signal writes a model whose own writes trigger more signals, and one of
    those queues external work. The individual hops each look reasonable.

Correlation is the part that is hard to get anywhere else, because it needs all
the analyses in one process with one model graph underneath them.
"""

from __future__ import annotations

from typing import Any

from . import cache as _cache
from .cascade import delete_impact
from .datetimes import datetime_audit
from .django_env import ensure_django
from .indexes import missing_indexes
from .introspect import list_models
from .serializers import serializer_exposure
from .signals import what_happens_on
from .tenancy import find_unscoped_queries


def _safe(fn, **kwargs) -> dict[str, Any]:
    """Never let one analyser failing take the whole picture down."""
    try:
        return fn(**kwargs)
    except Exception as exc:
        return {"unavailable": f"{type(exc).__name__}: {exc}"}


def _shared(fn, root, **kwargs) -> dict[str, Any]:
    """A project-wide analysis, memoised for the session.

    These four do not depend on which model was asked about, so answering a
    second question costs nothing. Measured before caching: explain on one
    model took 5.1 seconds of a 5.4 second full analysis, which made asking
    about ten models a minute of repeated work.
    """
    key = (fn.__module__, fn.__qualname__, tuple(sorted(kwargs.items())))
    return _cache.memoise(key, root, lambda: _safe(fn, **kwargs))


def _correlate(
    label: str,
    ownership: dict[str, Any] | None,
    unscoped: list[dict[str, Any]],
    cascade: dict[str, Any],
    exposure: list[dict[str, Any]],
    signals: dict[str, Any],
    owned_models: dict[str, Any],
    serialized_by: list[str],
) -> list[dict[str, Any]]:
    """Risks that need two or more analyses to be visible at all."""
    risks: list[dict[str, Any]] = []

    sensitive_fields = [
        entry["field"]
        for finding in exposure
        for entry in finding.get("sensitive", [])
    ]
    wide_open = [f for f in exposure if f.get("mode") in {"__all__"} or str(f.get("mode", "")).startswith("exclude")]

    # 1. The complete leak path. A wildcard serializer makes it worse but is
    #    not required: an explicit field list still returns the row, and the
    #    row is the problem when nothing checked who owns it.
    if ownership and unscoped and serialized_by:
        wildcard = [f["serializer"] for f in wide_open]
        risks.append(
            {
                "id": "unscoped_read_of_exposed_owned_model",
                "severity": "critical",
                "title": "A full path from a URL to another owner's row",
                "detail": (
                    f"{label} belongs to an owner through '{ownership['path']}'. "
                    f"{len(unscoped)} queryset(s) read it without scoping, and "
                    f"{len(serialized_by)} serializer(s) return it over the API. "
                    "Each of those is a warning on its own; together they are a "
                    "route from a request to somebody else's data."
                    + (
                        f" {len(wildcard)} of the serializer(s) expose every field, "
                        "so the next migration widens the leak with no diff on them."
                        if wildcard else ""
                    )
                ),
                "evidence": {
                    "owner_path": ownership["path"],
                    "unscoped_at": [f"{f['file']}:{f['line']}" for f in unscoped[:5]],
                    "serializers": serialized_by,
                    "wildcard_serializers": wildcard,
                },
                "seen_by": ["find_unscoped_queries", "serializer_exposure"],
            }
        )

    # 2. Sensitive field on an owned model, exposed wholesale.
    if ownership and sensitive_fields and wide_open:
        risks.append(
            {
                "id": "sensitive_field_on_owned_model_exposed",
                "severity": "critical",
                "title": "A sensitive field on owned data, exposed by a wildcard serializer",
                "detail": (
                    f"{label} is owned data and the API returns every field on it, "
                    f"including {', '.join(sorted(set(sensitive_fields)))}. The next "
                    "migration adds the next one, with no diff on the serializer."
                ),
                "evidence": {"fields": sorted(set(sensitive_fields))},
                "seen_by": ["find_unscoped_queries", "serializer_exposure"],
            }
        )

    # 3. A cascade that crosses into a different owner's subtree.
    if ownership:
        crossing = []
        for row in cascade.get("cascades", []):
            target = row.get("from_model")
            target_ownership = owned_models.get(target)
            if not target_ownership or not target_ownership.get("path"):
                continue
            # A child that reaches the owner *through* this model has a path
            # of "<field pointing here>__<this model's path>", so it ends with
            # it. Checking startswith was backwards and flagged every ordinary
            # child as if it belonged to somebody else.
            same_route = target_ownership["path"] == ownership["path"] or target_ownership[
                "path"
            ].endswith(f"__{ownership['path']}") or target_ownership["path"].endswith(
                ownership["path"]
            )
            if not same_route:
                crossing.append(
                    {"model": target, "its_owner_path": target_ownership["path"], "via": row["via_field"]}
                )
        if crossing:
            risks.append(
                {
                    "id": "cascade_crosses_ownership",
                    "severity": "high",
                    "title": "Deleting one row removes rows reached by a different ownership path",
                    "detail": (
                        f"Deleting a {label} cascades into "
                        f"{', '.join(c['model'] for c in crossing)}, which reach the "
                        "owner by a different route. Worth confirming that the rows "
                        "actually belong to the same owner in every case, because "
                        "the schema does not enforce it."
                    ),
                    "evidence": {"crossing": crossing},
                    "seen_by": ["delete_impact", "find_unscoped_queries"],
                }
            )

    # 4. A save that reaches external systems.
    external = [
        effect
        for effect in signals.get("side_effects", [])
        if effect.get("kind") in {"celery task", "email", "http request"}
    ]
    if external and signals.get("receiver_count", 0) > 1:
        risks.append(
            {
                "id": "save_reaches_external_systems",
                "severity": "medium",
                "title": "Saving this reaches outside the database, several hops away",
                "detail": (
                    f"A save on {label} runs {signals['receiver_count']} receiver(s) "
                    f"and ends in {len(external)} external effect(s). Nothing at the "
                    "call site says so, which matters in a loop, in a data "
                    "migration, or in a test that saves a hundred rows."
                ),
                "evidence": {
                    "effects": [f"{e['call']}() via {e['receiver']}" for e in external]
                },
                "seen_by": ["what_happens_on"],
            }
        )

    # 5. A cascade that also triggers signals: two multiplications at once.
    cascade_targets = {row["from_model"] for row in cascade.get("cascades", [])}
    if cascade_targets and signals.get("receiver_count"):
        risks.append(
            {
                "id": "cascade_and_signals_together",
                "severity": "medium",
                "title": "Deletion cascades and signals both fire, and neither knows about the other",
                "detail": (
                    f"Deleting a {label} removes rows in "
                    f"{len(cascade_targets)} other model(s), and this model also has "
                    "signal receivers. delete_impact does not run signals and "
                    "what_happens_on does not follow the cascade, so the real blast "
                    "radius is the union, which neither report shows on its own."
                ),
                "evidence": {"cascade_into": sorted(cascade_targets)},
                "seen_by": ["delete_impact", "what_happens_on"],
            }
        )

    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    risks.sort(key=lambda r: order.get(r["severity"], 9))
    return risks


def explain_model(
    model_label: str,
    tenant_root: str = "auth.User",
    include_raw: bool = False,
) -> dict[str, Any]:
    """Everything known about one model, plus the risks only visible combined.

    Args:
        model_label: "app_label.ModelName".
        tenant_root: the model that owns data, for the ownership half.
        include_raw: attach the full report from each analyser as well.
    """
    ensure_django()
    from django.apps import apps

    try:
        model = apps.get_model(model_label)
    except (LookupError, ValueError) as exc:
        known = sorted(m._meta.label for m in apps.get_models())
        raise ValueError(
            f"Unknown model '{model_label}'. Known: {', '.join(known[:40])}"
        ) from exc

    label = model._meta.label

    structure = _safe(list_models, app_label=model._meta.app_label, include_fields=True)
    entry = next(
        (m for m in structure.get("models", []) if m["label"] == label), {}
    )

    cascade = _safe(delete_impact, model_label=label)
    signals = _safe(what_happens_on, model_label=label, event="save")
    delete_signals = _safe(what_happens_on, model_label=label, event="delete")
    root = ensure_django().project_path
    tenancy = _shared(find_unscoped_queries, root, tenant_root=tenant_root)
    exposure_report = _shared(serializer_exposure, root, include_safe=True)
    index_report = _shared(missing_indexes, root)
    dt_report = _shared(datetime_audit, root)

    owned_models = tenancy.get("tenant_scoped_models", {}) or {}
    ownership = owned_models.get(label)
    unscoped = [f for f in tenancy.get("findings", []) if f.get("model") == label]
    exposure = [f for f in exposure_report.get("findings", []) if f.get("model") == label]
    unindexed = [f for f in index_report.get("findings", []) if f.get("model") == label]
    dt_fields = [f for f in dt_report.get("model_findings", []) if f.get("model") == label]

    relations = [
        {
            "field": field["name"],
            "kind": field["relation"]["kind"],
            "direction": field["relation"]["direction"],
            "to": field["relation"]["to"],
            "on_delete": field["relation"].get("on_delete"),
        }
        for field in entry.get("fields", [])
        if field.get("relation")
    ]

    # Every serializer that returns this model, whether or not it produced a
    # finding. A clean explicit serializer is not a defect, but it still means
    # the model reaches the API, which is half of the leak path.
    serialized_by = sorted(
        {f["serializer"] for f in exposure}
        | {
            entry["serializer"]
            for entry in exposure_report.get("explicit_and_clean", [])
            if entry.get("model") == label
        }
    )

    risks = _correlate(
        label, ownership, unscoped, cascade, exposure, signals, owned_models, serialized_by
    )

    summary_lines = [
        f"{label} maps to {entry.get('db_table', '?')} with "
        f"{entry.get('field_count', '?')} field(s) and {len(relations)} relation(s).",
    ]
    if ownership and ownership.get("path"):
        summary_lines.append(
            f"It belongs to {tenant_root} through '{ownership['path']}' "
            f"({ownership['depth']} hop(s))."
        )
    else:
        summary_lines.append(f"It has no ownership path to {tenant_root}.")
    if cascade.get("cascades"):
        summary_lines.append(
            f"Deleting one removes rows in {len({c['from_model'] for c in cascade['cascades']})} "
            f"other model(s); {len(cascade.get('blocked_by', []))} relation(s) can block it."
        )
    if signals.get("receiver_count"):
        summary_lines.append(
            f"Saving one runs {signals['receiver_count']} signal receiver(s) and "
            f"{len(signals.get('side_effects', []))} side effect(s)."
        )
    if exposure:
        summary_lines.append(f"{len(exposure)} serializer finding(s) touch it.")
    if unindexed:
        summary_lines.append(
            f"{len(unindexed)} field(s) are filtered or sorted on without an index."
        )
    if risks:
        summary_lines.append(
            f"{len(risks)} correlated risk(s), "
            f"{sum(1 for r in risks if r['severity'] == 'critical')} critical."
        )

    result: dict[str, Any] = {
        "model": label,
        "summary": " ".join(summary_lines),
        "structure": {
            "db_table": entry.get("db_table"),
            "field_count": entry.get("field_count"),
            "relations": relations,
        },
        "ownership": ownership or {"path": None, "note": f"no path to {tenant_root}"},
        "on_delete": {
            "summary": cascade.get("summary"),
            "cascades": cascade.get("cascades", []),
            "blocked_by": cascade.get("blocked_by", []),
            "signal_receivers": delete_signals.get("receiver_count", 0),
        },
        "on_save": {
            "receiver_count": signals.get("receiver_count", 0),
            "models_written": signals.get("models_written", []),
            "side_effects": signals.get("side_effects", []),
        },
        "api_exposure": exposure,
        "performance": {"unindexed_fields": unindexed},
        "datetime_fields": dt_fields,
        "correlated_risks": risks,
        "note": (
            "Every section carries the limits of the analyser it came from; see "
            "the individual tools for those. The correlated risks are the part "
            "no single check can produce, and they are inferences from several "
            "candidate-producing analyses, so they inherit every one of those "
            "uncertainties at once. Read them as the first questions to ask "
            "about this model, not as findings."
        ),
    }

    if include_raw:
        result["raw"] = {
            "delete_impact": cascade,
            "what_happens_on_save": signals,
            "what_happens_on_delete": delete_signals,
            "tenancy": tenancy,
            "serializers": exposure_report,
            "indexes": index_report,
        }

    return result
