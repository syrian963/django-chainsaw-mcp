# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Restrict findings to what a branch actually changed.

A baseline needs a committed file and a decision about what was already
acceptable. `--since` needs neither: it asks a narrower question that a code
review asks anyway. *Did this branch make anything worse?*

The two are different ratchets and both are useful. `--since main` is the one
to reach for on a pull request; a baseline is the one for a scheduled run on
the default branch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


class GitError(RuntimeError):
    """Raised when git cannot answer, so the caller can say why."""


def _run(args: list[str], cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not installed or not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError("git did not respond within 30 seconds") from exc

    if result.returncode != 0:
        raise GitError((result.stderr or result.stdout).strip() or "git failed")
    return result.stdout


def repo_root(start: Path) -> Path:
    return Path(_run(["rev-parse", "--show-toplevel"], start).strip())


def changed_files(ref: str, cwd: str | Path) -> dict[str, Any]:
    """Files that differ from `ref`, as paths relative to the repository root.

    Uses the merge base rather than a plain two-dot diff. On a branch that is
    behind main, `git diff main` also reports everything main gained in the
    meantime, which would blame this branch for other people's work.
    """
    start = Path(cwd).expanduser().resolve()
    root = repo_root(start)

    try:
        base = _run(["merge-base", ref, "HEAD"], root).strip()
        comparison = base
        strategy = f"merge-base({ref}, HEAD)"
    except GitError:
        # A ref with no shared history, or a bare commit id. Compare directly
        # and say so, rather than failing.
        comparison = ref
        strategy = f"direct diff against {ref}"

    committed = _run(["diff", "--name-only", comparison, "HEAD"], root).splitlines()
    # Uncommitted work counts: the point is to catch it before it is pushed.
    working = _run(["diff", "--name-only", "HEAD"], root).splitlines()
    staged = _run(["diff", "--name-only", "--cached"], root).splitlines()
    untracked = _run(["ls-files", "--others", "--exclude-standard"], root).splitlines()

    files = {line.strip() for line in committed + working + staged + untracked if line.strip()}

    return {
        "ref": ref,
        "strategy": strategy,
        "repo_root": str(root),
        "changed_files": sorted(files),
        "changed_count": len(files),
    }


def filter_findings(
    findings: list[dict[str, Any]],
    changed: set[str],
    base_dir: Path,
    root: Path,
    key: str = "file",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split findings into (in changed files, unmatchable).

    Finding paths are relative to whatever directory was scanned; git reports
    paths relative to the repository root. Both are resolved to absolute paths
    and compared there, which is unambiguous.

    Anything that cannot be resolved is returned separately rather than
    dropped. A finding that silently disappears because a path did not line up
    is the worst outcome for a tool whose whole job is to say what is wrong.
    """
    changed_absolute = {(root / path).resolve() for path in changed}

    matched: list[dict[str, Any]] = []
    unmatchable: list[dict[str, Any]] = []

    for finding in findings:
        value = finding.get(key)
        if not value:
            unmatchable.append(finding)
            continue
        try:
            absolute = (base_dir / str(value)).resolve()
        except (OSError, ValueError):
            unmatchable.append(finding)
            continue
        if absolute in changed_absolute:
            matched.append(finding)

    return matched, unmatchable
