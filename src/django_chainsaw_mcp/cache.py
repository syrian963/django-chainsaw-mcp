# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Reuse project-wide analyses within a session.

Measured on a generated project of 2822 files: the full `check` takes about
5.4 seconds, and `explain_model` for a **single** model takes 5.1. That is
almost the entire cost of analysing the project, spent to answer a question
about one class, because explain runs the ownership, exposure, index and
datetime checks and every one of them walks the whole tree.

Over MCP that is the normal case. An assistant asked about a model will be
asked about the next one, and paying five seconds each time makes the tool
feel broken on exactly the workflow it was built for.

So project-wide results are memoised for the life of the process and
invalidated when any source file changes. Invalidation is by modification time
rather than content hash: hashing 2822 files to decide whether to skip work
that takes five seconds is its own kind of slow, and mtime is what every build
tool relies on for the same reason.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", ".ruff_cache",
}
_WATCHED = {".py", ".html", ".jinja", ".jinja2", ".txt"}

_lock = threading.Lock()
_entries: dict[tuple, tuple[tuple[int, int], Any]] = {}
_stats = {"hits": 0, "misses": 0, "invalidations": 0}


def fingerprint(root: Path) -> tuple[int, int]:
    """(file count, newest mtime) for everything the analyses read.

    Cheap enough to run before each lookup: a stat call per file, no reads. On
    the 2822 file project this is a few milliseconds against seconds of
    analysis.
    """
    count = 0
    newest = 0
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = [d for d in subdirs if d not in _SKIP_DIRS]
        for name in files:
            if not name.endswith(tuple(_WATCHED)):
                continue
            count += 1
            try:
                mtime = os.stat(os.path.join(directory, name)).st_mtime_ns
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    return count, newest


def memoise(key: tuple, root: Path, produce: Callable[[], Any]) -> Any:
    """Return a cached result, or compute and store one.

    A changed tree drops the whole cache rather than one entry. The analyses
    are project-wide, so a single edited file can change any of them, and
    tracking which would cost more than recomputing.
    """
    current = fingerprint(root)

    with _lock:
        cached = _entries.get(key)
        if cached is not None and cached[0] == current:
            _stats["hits"] += 1
            return cached[1]

        if cached is not None:
            _stats["invalidations"] += 1
            _entries.clear()

    # Computed outside the lock: these take seconds, and holding a lock across
    # them would serialise concurrent tool calls for no benefit.
    value = produce()

    with _lock:
        _entries[key] = (current, value)
        _stats["misses"] += 1

    return value


def clear() -> None:
    with _lock:
        _entries.clear()


def stats() -> dict[str, Any]:
    with _lock:
        return {**_stats, "entries": len(_entries)}
