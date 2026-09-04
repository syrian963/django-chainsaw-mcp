# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Classify pending migrations by what they do to a live database.

Django tells you which migrations are unapplied. It does not tell you which of
them take a lock that stops writes, or which break the currently running code
during a rolling deploy. Both matter more than the migration count.

django-migration-linter covers similar ground as a standalone linter. This is
the same idea reachable from an assistant, together with the deploy-ordering
problem that a linter does not see.
"""

from __future__ import annotations

from typing import Any

from .django_env import ensure_django

BLOCKS_WRITES = "blocks_writes"
BREAKS_OLD_CODE = "breaks_running_code"
REWRITES_TABLE = "rewrites_table"
SAFE = "safe"


def _reversible(operation: Any) -> dict[str, Any] | None:
    """Can this operation be undone, and does the migration say so?

    Every schema operation Django ships knows how to reverse itself. RunPython
    and RunSQL do not: without `reverse_code` / `reverse_sql` they raise
    IrreversibleError on the way back, so `migrate <app> <previous>` fails and
    the rollback that a bad deploy needs is not available.

    This is not the same question as "is this operation risky". A safe data
    migration can still be the reason a rollback is impossible at 3am, and a
    dangerous one that declares its reverse is not.
    """
    name = type(operation).__name__
    if name == "RunPython":
        reverse = getattr(operation, "reverse_code", None)
    elif name == "RunSQL":
        reverse = getattr(operation, "reverse_sql", None)
    else:
        return None

    if reverse is None:
        return {
            "reversible": False,
            "why": (
                f"{name} without a reverse. Migrating backwards past this "
                "raises IrreversibleError, so rolling the deploy back is not "
                "an option once it has run"
            ),
            "fix": (
                "give it a reverse that undoes the change, or "
                "migrations.RunPython.noop if going backwards genuinely needs "
                "to do nothing - noop is a decision, None is an oversight and "
                "they are indistinguishable later"
            ),
        }

    noop = getattr(reverse, "__name__", "") == "noop" or reverse is getattr(
        __import__("django.db.migrations", fromlist=["RunPython"]).RunPython, "noop", None
    )
    return {
        "reversible": True,
        "why": f"{name} declares a reverse" + (" (noop)" if noop else ""),
        "fix": None,
    }


def _classify(operation: Any) -> dict[str, Any]:
    name = type(operation).__name__

    if name == "AddField":
        field = operation.field
        nullable = getattr(field, "null", False)
        has_default = field.has_default()
        if not nullable and not has_default:
            return {
                "risk": REWRITES_TABLE,
                "detail": (
                    "NOT NULL without a default cannot be applied to a table "
                    "that already has rows without a table rewrite or a failure."
                ),
                "safer": "Add it nullable, backfill, then set NOT NULL in a later migration.",
            }
        if not nullable and has_default:
            return {
                "risk": SAFE,
                "detail": (
                    "NOT NULL with a default is metadata-only on PostgreSQL 11 "
                    "and newer. On older versions it rewrites the table."
                ),
                "safer": None,
            }
        return {"risk": SAFE, "detail": "Nullable column, metadata-only.", "safer": None}

    if name in {"AlterField"}:
        return {
            "risk": REWRITES_TABLE,
            "detail": (
                "Changing a column type or constraint usually takes an "
                "ACCESS EXCLUSIVE lock and can rewrite the whole table."
            ),
            "safer": "Check the generated SQL with sqlmigrate before deploying.",
        }

    if name in {"RemoveField", "DeleteModel"}:
        return {
            "risk": BREAKS_OLD_CODE,
            "detail": (
                "During a rolling deploy the old code is still running and "
                "still selects this column or table. It will error until every "
                "pod is replaced."
            ),
            "safer": "Ship the code that stops using it first, remove it in the next release.",
        }

    if name in {"RenameField", "RenameModel"}:
        return {
            "risk": BREAKS_OLD_CODE,
            "detail": (
                "A rename is a remove and an add at the same time. Old and new "
                "code cannot both be right during the deploy window."
            ),
            "safer": "Add the new name, write to both, migrate readers, then drop the old one.",
        }

    if name in {"AddIndex", "AddConstraint"}:
        concurrent = getattr(getattr(operation, "index", None), "concurrently", False)
        if concurrent:
            return {"risk": SAFE, "detail": "Created concurrently, writes keep running.", "safer": None}
        return {
            "risk": BLOCKS_WRITES,
            "detail": (
                "Building an index without CONCURRENTLY holds a lock that "
                "blocks writes to the table for the duration."
            ),
            "safer": "Use AddIndexConcurrently from django.contrib.postgres.operations.",
        }

    if name in {"RunPython", "RunSQL"}:
        return {
            "risk": BLOCKS_WRITES,
            "detail": (
                "Arbitrary data migration. Runtime and locking depend entirely "
                "on what it does; a full-table UPDATE can hold locks for a long time."
            ),
            "safer": "Batch it, or move it out of the deploy into a background job.",
        }

    if name in {"CreateModel", "AddIndexConcurrently", "AlterModelOptions", "AlterUniqueTogether"}:
        return {"risk": SAFE, "detail": f"{name} does not touch existing rows.", "safer": None}

    return {"risk": SAFE, "detail": f"{name}: no specific rule, review manually.", "safer": None}


def migration_risk(include_applied: bool = False) -> dict[str, Any]:
    """List migrations and rate each operation by production impact.

    Args:
        include_applied: also classify migrations that already ran. Off by
            default; only what is about to be deployed usually matters.
    """
    ensure_django()
    from django.db import connections
    from django.db.migrations.loader import MigrationLoader

    connection = connections["default"]
    try:
        loader = MigrationLoader(connection, ignore_no_migrations=True)
        applied = set(loader.applied_migrations or ())
        db_reachable = True
    except Exception:
        # No database available: still analyse, just without applied state.
        loader = MigrationLoader(None, ignore_no_migrations=True)
        applied = set()
        db_reachable = False

    entries: list[dict[str, Any]] = []
    counts = {BLOCKS_WRITES: 0, BREAKS_OLD_CODE: 0, REWRITES_TABLE: 0, SAFE: 0}

    irreversible: list[dict[str, Any]] = []

    for key, migration in sorted(loader.disk_migrations.items()):
        is_applied = key in applied
        if is_applied and not include_applied:
            continue

        operations = []
        for operation in migration.operations:
            verdict = _classify(operation)
            counts[verdict["risk"]] = counts.get(verdict["risk"], 0) + 1
            reversibility = _reversible(operation)
            if reversibility is not None and not reversibility["reversible"]:
                irreversible.append(
                    {
                        "app": key[0],
                        "name": key[1],
                        "operation": type(operation).__name__,
                        "why": reversibility["why"],
                        "fix": reversibility["fix"],
                    }
                )
            operations.append(
                {
                    "operation": type(operation).__name__,
                    "reversible": None if reversibility is None else reversibility["reversible"],
                    "target": getattr(operation, "model_name", None)
                    or getattr(operation, "name", None),
                    "field": getattr(operation, "name", None)
                    if type(operation).__name__ in {"AddField", "RemoveField", "AlterField"}
                    else None,
                    **verdict,
                }
            )

        worst = SAFE
        for level in (BREAKS_OLD_CODE, REWRITES_TABLE, BLOCKS_WRITES):
            if any(op["risk"] == level for op in operations):
                worst = level
                break

        entries.append(
            {
                "app": key[0],
                "name": key[1],
                "applied": is_applied,
                "atomic": migration.atomic,
                "worst_risk": worst,
                "operations": operations,
            }
        )

    risky = [e for e in entries if e["worst_risk"] != SAFE]
    return {
        "irreversible": irreversible,
        "irreversible_count": len(irreversible),
        "database_reachable": db_reachable,
        "migration_count": len(entries),
        "risky_count": len(risky),
        "operation_risk_counts": counts,
        "migrations": entries,
        "legend": {
            BLOCKS_WRITES: "holds a lock that stops writes while it runs",
            BREAKS_OLD_CODE: "the currently deployed code breaks during the rolling deploy window",
            REWRITES_TABLE: "may rewrite the whole table under an exclusive lock",
            SAFE: "no effect on existing rows",
        },
        "note": (
            "Reversibility is tracked separately from row impact, because they "
            "are different questions: a harmless data migration with no reverse "
            "is still the reason a rollback fails, and a dangerous one that "
            "declares its reverse is not. RunPython.noop counts as declared - "
            "it says going backwards should do nothing, where None says nobody "
            "decided.\n\n"
            "Static reading of migration operations. It does not know your row "
            "counts, your PostgreSQL version or your deploy strategy, all of "
            "which change how bad a given operation actually is."
        ),
    }
