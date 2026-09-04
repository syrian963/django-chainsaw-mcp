# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Compare today's findings against a recorded baseline.

Every analyser here has the same adoption problem. Point `tenancy` at a five
year old codebase and it returns two hundred candidates. Nobody reads two
hundred candidates. The gate gets switched off in the first week, and then the
tool is worse than useless, because it is still running and nobody is looking.

A baseline fixes the arithmetic. Record what the project looks like today, and
from then on fail only on findings that were not there before. The existing two
hundred stay visible in the report and stop blocking anybody, and the number can
only go down, because the file is regenerated when something is fixed.

Findings are matched by a fingerprint that deliberately leaves out the line
number. Adding an import at the top of a file must not resurrect twenty
findings that nobody touched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

DEFAULT_BASELINE = ".django-chainsaw-baseline.json"

# How to reduce each analyser's output to comparable findings.
_EXTRACTORS: dict[str, Callable[[dict[str, Any]], list[dict[str, Any]]]] = {}


def _register(name: str):
    def wrap(fn):
        _EXTRACTORS[name] = fn
        return fn

    return wrap


@_register("tenancy")
def _tenancy(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "file": f["file"],
            "identity": f"{f['model']}|{f['chain']}|{','.join(sorted(f['filter_keys']))}",
            "severity": f["severity"],
            "summary": f"{f['model']} unscoped via {f['chain']}",
            "line": f["line"],
        }
        for f in report.get("findings", [])
    ]


@_register("n+1")
def _nplusone(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "file": result["template"],
            "identity": finding["expression"],
            "severity": finding["severity"],
            "summary": f"{finding['expression']} -> {finding['suggested']}",
            "line": None,
        }
        for result in report.get("results", [])
        for finding in result.get("findings", [])
    ]


@_register("deploy-safety")
def _deploy_safety(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "file": f"{entry['app']}/{entry['migration']}",
            "identity": f"{entry['operation']}|{entry['symbol']}",
            "severity": "high",
            "summary": entry["explanation"][:120],
            "line": None,
        }
        for entry in report.get("blocking", [])
    ]


def _fingerprint(finding: dict[str, Any]) -> str:
    """Stable across edits that move code around.

    File and identity, never the line number. A finding that shifted down
    because somebody added an import is the same finding.
    """
    raw = f"{finding['file']}::{finding['identity']}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def extract(check: str, report: dict[str, Any]) -> list[dict[str, Any]]:
    if check not in _EXTRACTORS:
        raise ValueError(
            f"No baseline support for '{check}'. Available: {', '.join(sorted(_EXTRACTORS))}"
        )
    findings = _EXTRACTORS[check](report)
    for finding in findings:
        finding["fingerprint"] = _fingerprint(finding)
    return findings


def load(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.is_file():
        return {"version": 1, "checks": {}}

    text = file_path.read_text(encoding="utf-8").strip()
    # An empty file means no baseline yet, not a broken one. `touch` and
    # `mktemp` both produce one, and erroring there sends people looking for a
    # corruption that is not present.
    if not text:
        return {"version": 1, "checks": {}}

    try:
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Baseline file is not readable JSON: {file_path} ({exc})") from exc
    if not isinstance(data, dict) or "checks" not in data:
        raise ValueError(f"Baseline file has an unexpected shape: {file_path}")
    return data


def compare(check: str, report: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    """Split today's findings into new, known and fixed."""
    current = extract(check, report)
    recorded = baseline.get("checks", {}).get(check, {}).get("findings", [])
    known = {entry["fingerprint"]: entry for entry in recorded}
    seen = {finding["fingerprint"] for finding in current}

    new = [f for f in current if f["fingerprint"] not in known]
    unchanged = [f for f in current if f["fingerprint"] in known]
    fixed = [entry for fp, entry in known.items() if fp not in seen]

    return {
        "check": check,
        "new": new,
        "unchanged": unchanged,
        "fixed": fixed,
        "new_count": len(new),
        "unchanged_count": len(unchanged),
        "fixed_count": len(fixed),
        "baseline_recorded_at": baseline.get("checks", {}).get(check, {}).get("recorded_at"),
        "has_baseline": check in baseline.get("checks", {}),
    }


def write(path: str | Path, check: str, report: dict[str, Any]) -> dict[str, Any]:
    """Record the current findings for one check, keeping the others intact."""
    file_path = Path(path)
    data = load(file_path)
    findings = extract(check, report)

    data.setdefault("version", 1)
    data.setdefault("checks", {})
    data["checks"][check] = {
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(findings),
        # Sorted so the file does not churn between runs and stays reviewable
        # in a diff.
        "findings": sorted(findings, key=lambda f: (f["file"], f["identity"])),
    }

    file_path.write_text(json.dumps(data, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return {
        "baseline": str(file_path),
        "check": check,
        "recorded": len(findings),
        "note": (
            "Commit this file. It is a record of what was already wrong, so that "
            "the gate can fail on new findings only. Regenerate it when findings "
            "are fixed; the count should only ever go down."
        ),
    }
