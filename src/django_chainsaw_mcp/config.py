# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Project settings, so the same flags are not typed on every run.

`--tenant-root myapp.Organisation` is not a preference, it is a fact about the
project that never changes. Requiring it on every invocation means it ends up
in a shell alias on one machine, absent from CI, and wrong in the README.

Read from `pyproject.toml` under `[tool.django-chainsaw]`, because that is
where a Python project already keeps this kind of thing and nobody needs to
learn a new file. A command line flag always wins over the file.

Two things beyond defaults, both of which every real linter needs:

**ignore** patterns, for paths where a finding is expected rather than wrong.

**Inline suppression with a mandatory reason.** `# chainsaw: ignore[tenancy]`
alone is refused. A suppression without a reason is how a codebase quietly
fills with unexplained exceptions, and the reason is the only part that is
still useful in a year.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

CONFIG_FILE = "pyproject.toml"
CONFIG_SECTION = ("tool", "django-chainsaw")

# `# chainsaw: ignore[tenancy] - filtered in get_queryset`
_SUPPRESSION = re.compile(
    r"#\s*chainsaw:\s*ignore(?:\[(?P<checks>[a-z0-9+,\s-]*)\])?\s*(?:[-:]\s*(?P<reason>.*))?$",
    re.I,
)


@dataclass
class Suppression:
    line: int
    checks: set[str]
    reason: str
    valid: bool
    problem: str | None = None

    def covers(self, check: str) -> bool:
        return self.valid and (not self.checks or check in self.checks)


@dataclass
class Config:
    tenant_root: str = "auth.User"
    ignore: list[str] = field(default_factory=list)
    fail_on: str = "high"
    skip: list[str] = field(default_factory=list)
    baseline: str | None = None
    source: str | None = None

    def is_ignored(self, relative_path: str) -> bool:
        return any(fnmatch(relative_path, pattern) for pattern in self.ignore)


def _read_section(path: Path) -> dict[str, Any]:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    node: Any = data
    for key in CONFIG_SECTION:
        if not isinstance(node, dict) or key not in node:
            return {}
        node = node[key]
    return node if isinstance(node, dict) else {}


def load(start: str | Path) -> Config:
    """Find the nearest pyproject.toml with a section, walking upwards."""
    current = Path(start).expanduser().resolve()
    if current.is_file():
        current = current.parent

    for directory in [current, *current.parents]:
        candidate = directory / CONFIG_FILE
        if not candidate.is_file():
            continue
        section = _read_section(candidate)
        if not section:
            continue
        return Config(
            tenant_root=str(section.get("tenant-root", "auth.User")),
            ignore=[str(x) for x in section.get("ignore", [])],
            fail_on=str(section.get("fail-on", "high")),
            skip=[str(x) for x in section.get("skip", [])],
            baseline=str(section["baseline"]) if "baseline" in section else None,
            source=str(candidate),
        )

    return Config()


def parse_suppressions(source: str) -> dict[int, Suppression]:
    """Read `# chainsaw: ignore[...]` comments out of a file.

    A suppression with no reason is parsed and marked invalid rather than
    ignored, so the tool can say why it did not take effect. Silently doing
    nothing would be indistinguishable from a bug in the matcher.
    """
    found: dict[int, Suppression] = {}

    for number, line in enumerate(source.splitlines(), start=1):
        match = _SUPPRESSION.search(line)
        if not match:
            continue

        raw_checks = (match.group("checks") or "").strip()
        checks = {c.strip() for c in raw_checks.split(",") if c.strip()}
        reason = (match.group("reason") or "").strip()

        if not reason:
            found[number] = Suppression(
                line=number,
                checks=checks,
                reason="",
                valid=False,
                problem=(
                    "a suppression needs a reason: "
                    "# chainsaw: ignore[check] - why this one is fine"
                ),
            )
            continue

        found[number] = Suppression(line=number, checks=checks, reason=reason, valid=True)

    return found


def apply_suppressions(
    findings: list[dict[str, Any]],
    root: Path,
    file_key: str = "file",
    line_key: str = "line",
    check_key: str = "check",
) -> dict[str, Any]:
    """Split findings into kept, suppressed, and suppressions that were refused.

    A suppression is honoured on the finding's own line or the line directly
    above it, which is the convention every linter uses and the only one people
    guess correctly.
    """
    cache: dict[str, dict[int, Suppression]] = {}
    kept: list[dict[str, Any]] = []
    suppressed: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []

    for finding in findings:
        relative = finding.get(file_key)
        line = finding.get(line_key)
        if not relative or not line:
            kept.append(finding)
            continue

        if relative not in cache:
            try:
                cache[relative] = parse_suppressions(
                    (root / relative).read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                cache[relative] = {}

        check = finding.get(check_key, "")
        marker = cache[relative].get(line) or cache[relative].get(line - 1)

        if marker is None:
            kept.append(finding)
        elif marker.covers(check):
            suppressed.append({**finding, "suppressed_because": marker.reason})
        elif not marker.valid:
            refused.append({**finding, "suppression_problem": marker.problem})
            kept.append(finding)
        else:
            kept.append(finding)

    return {
        "findings": kept,
        "suppressed": suppressed,
        "refused_suppressions": refused,
    }
