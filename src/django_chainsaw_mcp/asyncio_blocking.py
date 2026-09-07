# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""One synchronous call, and the whole server stops.

FastAPI runs an `async def` endpoint **on the event loop itself**. A `def`
endpoint it hands to a threadpool. So this:

    @app.get("/users/{pk}")
    async def get_user(pk: int):
        return session.query(User).get(pk)      # blocking driver

does not serve one slow request. It stops **every** request in the process for
as long as the query takes, because nothing else can run while the loop is
blocked. At one request a second in development it is invisible. At two
hundred a second it is an outage, and the traceback points at whichever
unlucky endpoint timed out rather than at this line.

The maddening part is that removing `async` fixes it. A `def` endpoint runs in
a threadpool where blocking is fine.

## What ruff already covers, and what it does not

`ruff`'s ASYNC rules (from flake8-async) catch a fixed list inside an async
function: `open`, `time.sleep`, `subprocess`, `os.popen`. Those are real and
they are the easy half.

They do not catch the common one, which is a **synchronous database call** -
`session.query(...)`, `Model.objects.get(...)`, `cursor.execute(...)` - and
they do not follow a call. This does both:

    @app.get("/orders")
    async def list_orders():
        return build_report()          # nothing here looks blocking

    def build_report():
        return session.query(Order).all()      # and here it is

Neither function is suspicious alone. The call graph already knows the edge,
so the endpoint is reported with the path that reaches the blocking call.

## Why `def` endpoints are silent

They are not a bug. FastAPI puts them in a threadpool precisely so that
blocking code is safe there, and telling somebody to make a working
synchronous endpoint async would be advice that causes the outage.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from . import callgraph
from .project import TreeCache, get_profile, resolve_root

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
}

# Full dotted paths, matched exactly. A bare `get` or `execute` means far too
# many things; `requests.get` means one.
_BLOCKING_PATHS: dict[str, tuple[str, str]] = {
    "requests.get": ("http", "requests is synchronous; use httpx.AsyncClient"),
    "requests.post": ("http", "requests is synchronous; use httpx.AsyncClient"),
    "requests.put": ("http", "requests is synchronous; use httpx.AsyncClient"),
    "requests.patch": ("http", "requests is synchronous; use httpx.AsyncClient"),
    "requests.delete": ("http", "requests is synchronous; use httpx.AsyncClient"),
    "requests.request": ("http", "requests is synchronous; use httpx.AsyncClient"),
    "urllib.request.urlopen": ("http", "urlopen is synchronous; use httpx.AsyncClient"),
    "time.sleep": ("sleep", "use `await asyncio.sleep(...)`"),
    "subprocess.run": ("subprocess", "use `await asyncio.create_subprocess_exec(...)`"),
    "subprocess.call": ("subprocess", "use `await asyncio.create_subprocess_exec(...)`"),
    "subprocess.check_output": ("subprocess", "use `await asyncio.create_subprocess_exec(...)`"),
    "os.system": ("subprocess", "use `await asyncio.create_subprocess_exec(...)`"),
}

# Attribute names that mean a synchronous database round trip on the objects
# they are usually called on. Guarded by the receiver, below.
_ORM_TERMINALS = {
    "all", "first", "one", "one_or_none", "scalar", "scalars", "get", "count",
    "exists", "delete", "update", "save", "create", "bulk_create", "commit",
    "flush", "refresh", "execute", "fetchall", "fetchone", "fetchmany",
    "get_or_create", "update_or_create", "aggregate", "iterator", "add_all",
}

# Receivers that make an ORM terminal a real database call rather than a list
# method or a dict lookup.
_ORM_RECEIVERS = ("session", "db", "objects", "query", "cursor", "conn",
                  "connection", "engine", "queryset", "qs", "_default_manager")

_ASYNC_SAFE_PREFIXES = ("await ", "async ")


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def _receiver_chain(node: ast.AST) -> str:
    """Every name in a call chain, including through intermediate calls.

    `_dotted` stops at the first Call, so `session.query(User).get(pk)` came
    back empty and the single most common form of this bug was invisible. This
    keeps walking: attribute, call, attribute, until it reaches a name.
    """
    parts: list[str] = []
    current = node
    while True:
        if isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        elif isinstance(current, ast.Call):
            current = current.func
        elif isinstance(current, ast.Subscript):
            current = current.value
        elif isinstance(current, ast.Name):
            parts.append(current.id)
            break
        else:
            break
    return ".".join(reversed(parts))


def _classify_call(node: ast.Call) -> tuple[str, str] | None:
    """Is this call blocking, and why?"""
    plain = _dotted(node.func)
    if plain in _BLOCKING_PATHS:
        return _BLOCKING_PATHS[plain]

    if not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in _ORM_TERMINALS:
        return None

    # `session.query(User).all()` -> the chain contains "session", even though
    # there is a call in the middle of it. `[1, 2].count(1)` does not.
    chain = _receiver_chain(node.func).lower()
    if not chain:
        return None
    if not any(word in chain for word in _ORM_RECEIVERS):
        return None
    return ("database", "a synchronous database call blocks the loop for every "
                        "request in this process; use an async driver, or make "
                        "the endpoint `def` so FastAPI runs it in a threadpool")


class _AsyncBodyScan(ast.NodeVisitor):
    """Blocking calls written directly in one function body.

    A nested `def` or `async def` is skipped: its body runs when that function
    is called, not here, and attributing it to the enclosing function would
    report the wrong line.
    """

    def __init__(self) -> None:
        self.hits: list[dict[str, Any]] = []
        self.calls: list[tuple[str, int]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_Await(self, node: ast.Await) -> None:
        # Anything awaited is by definition not blocking the loop.
        return

    def visit_Call(self, node: ast.Call) -> None:
        verdict = _classify_call(node)
        if verdict is not None:
            kind, fix = verdict
            self.hits.append({"line": node.lineno, "kind": kind, "fix": fix,
                              "call": _receiver_chain(node.func) or _dotted(node.func)})
        dotted = _dotted(node.func)
        if dotted:
            self.calls.append((dotted, node.lineno))
        self.generic_visit(node)


def _async_functions(tree: ast.AST) -> list[ast.AsyncFunctionDef]:
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]


def blocking_in_async(
    search_path: str | None = None,
    follow_calls: bool = True,
    max_depth: int = 3,
) -> dict[str, Any]:
    """Synchronous calls that run on the event loop.

    Args:
        search_path: directory to scan. Defaults to the configured project.
        follow_calls: also report a blocking call reached through a project
            function, with the path that reaches it.
        max_depth: how many calls deep to follow.
    """
    root = resolve_root(search_path)
    found = get_profile(root)

    direct: list[dict[str, Any]] = []
    indirect: list[dict[str, Any]] = []
    async_seen = 0
    files_scanned = 0

    # A project with no async functions has nothing for this check to say, and
    # building a call graph over it is pure cost: on a 2000-file Django project
    # with one async function that was 60 seconds to report nothing.
    if found.async_functions == 0:
        return {
            "search_path": str(root),
            "files_scanned": found.files_scanned,
            "frameworks": dict(found.frameworks),
            "async_functions_seen": 0,
            "direct_count": 0,
            "indirect_count": 0,
            "finding_count": 0,
            "direct": [],
            "reached_through_a_call": [],
            "note": (
                "This project defines no async functions, so nothing can be "
                "running on an event loop and there is nothing to report. The "
                "call graph was not built, because it would have cost a great "
                "deal to confirm an answer that was already known."
            ),
        }

    graph = callgraph.build(root) if follow_calls else None

    # Which project functions block, directly, so an async caller can be told.
    blocking_functions: dict[str, dict[str, Any]] = {}
    if graph is not None:
        trees = TreeCache(root)
        for fn in graph.functions.values():
            node = trees.function(fn)
            if node is None:
                continue
            scan = _AsyncBodyScan()
            for child in node.body:
                scan.visit(child)
            if scan.hits:
                blocking_functions[fn.qualname] = {
                    "file": fn.file,
                    "line": scan.hits[0]["line"],
                    "kind": scan.hits[0]["kind"],
                    "call": scan.hits[0]["call"],
                    "fix": scan.hits[0]["fix"],
                }

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        files_scanned += 1
        lines = source.splitlines()
        relative = str(path.relative_to(root))

        for node in _async_functions(tree):
            async_seen += 1
            scan = _AsyncBodyScan()
            for child in node.body:
                scan.visit(child)

            for hit in scan.hits:
                direct.append({
                    "file": relative,
                    "line": hit["line"],
                    "code": lines[hit["line"] - 1].strip()[:140] if hit["line"] <= len(lines) else "",
                    "function": node.name,
                    "kind": hit["kind"],
                    "call": hit["call"],
                    "severity": "high",
                    "why": (
                        f"`async def {node.name}` runs on the event loop, and this "
                        "call does not yield. Every other request in the process "
                        "waits for it"
                    ),
                    "fix": hit["fix"],
                })

            if graph is None:
                continue

            qualname = _qualname_for(graph, relative, node)
            if qualname is None:
                continue
            reached = _reaches_blocking(graph, qualname, blocking_functions, max_depth)
            for target, path_to in reached:
                info = blocking_functions[target]
                indirect.append({
                    "file": info["file"],
                    "line": info["line"],
                    "function": node.name,
                    "endpoint": qualname,
                    "blocking_in": target,
                    "call": info["call"],
                    "kind": info["kind"],
                    "reached_through": path_to,
                    "severity": "high",
                    "why": (
                        f"`async def {node.name}` reaches a synchronous call "
                        f"{len(path_to) - 1} step(s) away. Nothing at the call "
                        "site looks blocking, and the loop stops all the same"
                    ),
                    "fix": info["fix"],
                })

    seen_keys: set[tuple[str, int, str]] = set()
    unique_indirect = []
    for entry in indirect:
        key = (entry["file"], entry["line"], entry["endpoint"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        unique_indirect.append(entry)

    direct.sort(key=lambda f: (f["file"], f["line"]))
    unique_indirect.sort(key=lambda f: (f["endpoint"], f["file"], f["line"]))

    return {
        "search_path": str(root),
        "files_scanned": files_scanned,
        "frameworks": dict(found.frameworks),
        "async_functions_seen": async_seen,
        "direct_count": len(direct),
        "indirect_count": len(unique_indirect),
        "finding_count": len(direct) + len(unique_indirect),
        "direct": direct,
        "reached_through_a_call": unique_indirect,
        "note": (
            "An `async def` endpoint runs on the event loop; a `def` endpoint "
            "runs in a threadpool where blocking is fine. So a synchronous call "
            "in an async function does not slow one request, it stops every "
            "request in the process - invisible at one request a second, an "
            "outage at two hundred.\n\n"
            "ruff's ASYNC rules cover open, time.sleep and subprocess inside an "
            "async function. The common case is a synchronous database call, "
            "and the expensive case is one reached through another function "
            "where nothing at the call site looks blocking; both are here, the "
            "second with the path that reaches it.\n\n"
            "A database call is recognised by a terminal method on a receiver "
            "named like a session, a manager or a cursor, so `[1, 2].count(1)` "
            "is left alone and a session bound to an unusual name is missed. "
            "Anything awaited is not reported: awaiting is what yields."
        ),
    }


def _function_node(root: Path, fn: Any) -> ast.AST | None:
    """Kept for callers outside the hot loop; the cache is what the loop uses."""
    return TreeCache(root).function(fn)


def _qualname_for(graph: Any, relative: str, node: ast.AST) -> str | None:
    for fn in graph.functions.values():
        if fn.file == relative and fn.line == node.lineno and fn.name == node.name:
            return fn.qualname
    return None


def _reaches_blocking(
    graph: Any,
    start: str,
    blocking: dict[str, dict[str, Any]],
    max_depth: int,
) -> list[tuple[str, list[str]]]:
    """Blocking project functions this one reaches, with the shortest path."""
    out: list[tuple[str, list[str]]] = []
    seen = {start}
    queue: list[tuple[str, list[str]]] = [(start, [start])]

    while queue:
        current, path = queue.pop(0)
        if len(path) > max_depth:
            continue
        fn = graph.functions.get(current)
        if fn is None:
            continue
        for callee in sorted(fn.calls | fn.calls_in_atomic | fn.calls_deferred):
            if callee in seen:
                continue
            seen.add(callee)
            next_path = [*path, callee]
            if callee in blocking:
                out.append((callee, next_path))
            queue.append((callee, next_path))
    return out
