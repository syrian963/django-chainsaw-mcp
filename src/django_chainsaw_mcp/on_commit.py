# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Side effects that escape the transaction they were written inside.

A database transaction can be rolled back. An email cannot. Neither can a
webhook, a payment capture, or a task another process has already picked up.

So a call like this, inside `transaction.atomic()`, has two distinct defects
and only one of them is the famous one:

    with transaction.atomic():
        order = Order.objects.create(...)
        send_order_confirmation.delay(order.pk)

The famous one is the race. `.delay()` hands the task to the broker
immediately, a worker can pick it up in milliseconds, and the row is not
committed yet. The worker queries for `order.pk` and finds nothing. It passes
every test, because the test runs in a transaction that never commits and a
worker that runs eagerly. It fails under load, intermittently, in production.

The quieter one is the rollback. If anything after that line raises, the order
is gone and the customer has the confirmation email.

The ecosystem's answer to this is runtime: wrapper libraries like
`django-celery-oncommit` and `django-post-request-task`, or Celery 5.4's
`delay_on_commit()`. All of them fix the code you write next. None of them find
the calls already in the codebase, and `flake8-django` and ruff's `DJ` rules do
not look at this at all.

Finding them statically needs three things, all of which are in the source: the
transaction boundaries, the calls that reach outside the process, and whether
those calls are deferred to `on_commit`.

### The case nobody remembers

`ATOMIC_REQUESTS = True` wraps **every view** in a transaction. There is no
`with` block to see, no decorator, nothing at the call site to suggest the
problem exists. A project with that setting has this defect in every view that
sends anything, and the code looks completely innocent.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from . import callgraph
from .django_env import ensure_django
from .project import parse_file, read_source

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build", "migrations",
}

# What each kind of escaping call costs when the transaction it sits in has not
# committed yet, or never does. The wording is the finding: a category name on
# its own tells nobody why they should care.
_TASK = (
    "task",
    "high",
    "the broker has the task now, and a worker can start before this "
    "transaction commits. It will query for a row that is not there yet",
    "transaction.on_commit(lambda: {call}), or .delay_on_commit() on Celery 5.4+",
)
_HTTP = (
    "outbound_http",
    "high",
    "the request leaves the process immediately. If this transaction rolls "
    "back, the other system has been told about something that never happened",
    "transaction.on_commit(lambda: {call})",
)
_EMAIL = (
    "email",
    "high",
    "mail cannot be unsent. If this transaction rolls back, the recipient has "
    "been told about something that never happened",
    "transaction.on_commit(lambda: {call})",
)
_CACHE = (
    "cache_write",
    "medium",
    "the cache is written before the row is visible, so a reader can be handed "
    "state the database does not have yet, and a rollback leaves it stale",
    "transaction.on_commit(lambda: {call})",
)

# Method names. These are matched on the attribute, because the object they
# hang off is almost never resolvable: `send_welcome.delay(...)`,
# `self.notify.apply_async(...)`, `some_factory().delay(...)`.
_ESCAPING_METHODS: dict[str, tuple[str, str, str, str]] = {
    "delay": _TASK,
    "apply_async": _TASK,
    "send_task": _TASK,
    "enqueue": _TASK,          # RQ
    "enqueue_at": _TASK,
    "enqueue_in": _TASK,
    "schedule": _TASK,         # django-q, huey
}

# Dotted calls, matched on the full path so `requests.post` is a finding and
# `self.session.post` is not guessed at.
_ESCAPING_PATHS: dict[str, tuple[str, str, str, str]] = {
    "requests.post": _HTTP,
    "requests.get": _HTTP,
    "requests.put": _HTTP,
    "requests.patch": _HTTP,
    "requests.delete": _HTTP,
    "requests.request": _HTTP,
    "httpx.post": _HTTP,
    "httpx.get": _HTTP,
    "httpx.put": _HTTP,
    "httpx.patch": _HTTP,
    "httpx.delete": _HTTP,
    "urllib.request.urlopen": _HTTP,
    "urlopen": _HTTP,
    # Matched on the full dotted path on purpose. `.set()` is also a related
    # manager method, and treating a bare `set` as a cache write has already
    # produced one wrong finding elsewhere in this project.
    "cache.set": _CACHE,
    "cache.delete": _CACHE,
    "cache.set_many": _CACHE,
    "cache.delete_many": _CACHE,
}

# Bare function names.
_ESCAPING_NAMES: dict[str, tuple[str, str, str, str]] = {
    "send_mail": _EMAIL,
    "send_mass_mail": _EMAIL,
    "mail_admins": _EMAIL,
    "mail_managers": _EMAIL,
}

# Calls that are already deferred, so seeing one means the author knew.
_DEFERRING = {"on_commit", "delay_on_commit", "apply_async_on_commit"}

# `.send()` is hopelessly overloaded: EmailMessage.send, Signal.send,
# socket.send, a mock. It is only treated as mail when the line also mentions
# something mail-shaped, and it is reported at low confidence either way.
_MAIL_HINTS = ("mail", "email", "message", "msg")


def _dotted(node: ast.AST) -> str:
    """`requests.post` from the attribute chain, or "" if it is not a plain one."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def _is_atomic(node: ast.AST) -> bool:
    """`transaction.atomic`, `atomic`, either called or bare."""
    target = node.func if isinstance(node, ast.Call) else node
    dotted = _dotted(target)
    return dotted == "atomic" or dotted.endswith(".atomic")


class _Visitor(ast.NodeVisitor):
    """Walks the file carrying the current transaction depth.

    Depth rather than a flag, because a `with transaction.atomic()` inside a
    function already decorated `@transaction.atomic` is one transaction from
    the database's point of view, and leaving the inner block does not end it.
    """

    def __init__(self, base_depth: int = 0) -> None:
        self.findings: list[dict[str, Any]] = []
        self._depth = base_depth
        self._reason: list[str] = []
        # Set while walking the arguments of an on_commit() call, or the body
        # of a lambda passed to one. Anything in there is already deferred.
        self._deferred = 0

    # -- transaction boundaries ------------------------------------------

    def visit_With(self, node: ast.With) -> None:
        opened = any(_is_atomic(item.context_expr) for item in node.items)
        if opened:
            self._depth += 1
            self._reason.append(f"with transaction.atomic() at line {node.lineno}")
        self.generic_visit(node)
        if opened:
            self._depth -= 1
            self._reason.pop()

    visit_AsyncWith = visit_With  # type: ignore[assignment]

    def _visit_function(self, node: Any) -> None:
        decorated = any(_is_atomic(d) for d in node.decorator_list)
        if decorated:
            self._depth += 1
            self._reason.append(f"@transaction.atomic on {node.name}()")
        self.generic_visit(node)
        if decorated:
            self._depth -= 1
            self._reason.pop()

    visit_FunctionDef = _visit_function  # type: ignore[assignment]
    visit_AsyncFunctionDef = _visit_function  # type: ignore[assignment]

    # -- calls -------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else ""
        )

        if name in _DEFERRING:
            # Everything inside the arguments is deferred by definition, and
            # that includes the body of the lambda that is usually passed.
            self._deferred += 1
            self.generic_visit(node)
            self._deferred -= 1
            return

        if self._depth > 0 and self._deferred == 0:
            entry = self._classify(node, name)
            if entry is not None:
                self.findings.append(entry)

        self.generic_visit(node)

    def _classify(self, node: ast.Call, name: str) -> dict[str, Any] | None:
        dotted = _dotted(node.func)
        confidence = "high"

        spec = _ESCAPING_PATHS.get(dotted)
        if spec is None and isinstance(node.func, ast.Name):
            spec = _ESCAPING_NAMES.get(name)
        if spec is None:
            spec = _ESCAPING_METHODS.get(name)
        if spec is None and name == "send":
            haystack = dotted.lower()
            if any(hint in haystack for hint in _MAIL_HINTS):
                spec = _EMAIL
                confidence = "low"
            else:
                return None
        if spec is None:
            return None

        kind, severity, consequence, fix = spec

        # `delay(...)` with nothing in front of it is a plain function call that
        # happens to share a name with Celery's, so it is a guess, not a finding.
        if kind == "task" and not dotted:
            confidence = "low"

        return {
            "kind": kind,
            "severity": severity,
            "confidence": confidence,
            "line": node.lineno,
            "call": dotted or name,
            "inside": self._reason[-1] if self._reason else "",
            "nesting": self._depth,
            "consequence": consequence,
            "fix": fix,
        }


def _views_with_side_effects(root: Path, include_low_confidence: bool) -> list[dict[str, Any]]:
    """Sends inside a view, when ATOMIC_REQUESTS makes every view a transaction.

    Only functions that are actually views: a `request` first argument, or a
    method on a class the graph says descends from something View-shaped.
    Scanning every function would report the whole project.
    """
    from . import callgraph

    graph = callgraph.build(root)
    out: list[dict[str, Any]] = []

    for fn in graph.functions.values():
        if fn.class_name and not _is_view_method(graph, fn):
            continue
        full = root / fn.file
        try:
            source = read_source(full)
            tree = parse_file(full)
        except (OSError, SyntaxError):
            continue
        node = _function_node(tree, fn.name, fn.line)
        if node is None:
            continue
        # A plain function is a view when Django calls it as one, and the only
        # thing in the source that says so is the first argument.
        if not fn.class_name and not _takes_a_request(node):
            continue

        lines = source.splitlines()
        visitor = _Visitor(base_depth=1)
        for child in node.body:
            visitor.visit(child)

        for finding in visitor.findings:
            if finding["confidence"] == "low" and not include_low_confidence:
                continue
            finding["file"] = fn.file
            finding["code"] = (
                lines[finding["line"] - 1].strip()[:140]
                if finding["line"] <= len(lines) else ""
            )
            finding["fix"] = finding["fix"].replace("{call}", finding["code"].rstrip(","))
            finding["inside"] = f"ATOMIC_REQUESTS, via the view {fn.qualname}"
            finding["view"] = fn.qualname
            out.append(finding)

    out.sort(key=lambda f: (f["file"], f["line"]))
    return out


_VIEW_BASE_HINTS = ("View", "ViewSet", "APIView")
_VIEW_METHODS = {
    "get", "post", "put", "patch", "delete", "head", "options",
    "list", "create", "retrieve", "update", "partial_update", "destroy",
}


def _is_view_method(graph: Any, fn: Any) -> bool:
    if fn.name not in _VIEW_METHODS:
        return False
    return any(
        any(hint in ancestor.rsplit(".", 1)[-1] for hint in _VIEW_BASE_HINTS)
        for ancestor in graph.ancestors(fn.class_name)
    )


def _takes_a_request(node: Any) -> bool:
    args = node.args.args
    return bool(args) and args[0].arg == "request"


def _function_node(tree: ast.AST, name: str, line: int) -> ast.AST | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name and node.lineno == line:
                return node
    return None


def _atomic_requests_databases() -> list[str]:
    from django.conf import settings

    return [
        alias
        for alias, conf in getattr(settings, "DATABASES", {}).items()
        if conf.get("ATOMIC_REQUESTS")
    ]


def escaping_side_effects(
    search_path: str | None = None,
    include_low_confidence: bool = False,
) -> dict[str, Any]:
    """Calls inside a transaction whose effect cannot be rolled back."""
    config = ensure_django()

    root = Path(search_path).expanduser().resolve() if search_path else config.project_path
    if not root.is_dir():
        raise ValueError(f"search_path is not a directory: {root}")

    atomic_request_dbs = _atomic_requests_databases()

    findings: list[dict[str, Any]] = []
    files_scanned = 0

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue

        files_scanned += 1
        lines = source.splitlines()

        visitor = _Visitor()
        visitor.visit(tree)

        for finding in visitor.findings:
            if finding["confidence"] == "low" and not include_low_confidence:
                continue
            finding["file"] = str(path.relative_to(root))
            finding["code"] = (
                lines[finding["line"] - 1].strip()[:140]
                if finding["line"] <= len(lines) else ""
            )
            finding["fix"] = finding["fix"].replace("{call}", finding["code"].rstrip(","))
            findings.append(finding)

    # ATOMIC_REQUESTS wraps every view in a transaction with nothing at the
    # call site to say so. Previously only the setting was reported, on the
    # grounds that enumerating the views would drown the visible cases. That
    # was the wrong trade: it left the largest class of this defect uncounted
    # in exactly the projects that have it. They are counted now and kept in
    # their own tier, so they inform without burying anything.
    atomic_request_views: list[dict[str, Any]] = []
    if atomic_request_dbs:
        atomic_request_views = _views_with_side_effects(
            root, include_low_confidence=include_low_confidence
        )

    # Everything above came from a boundary visible in the same file. The
    # rest of them are in functions a caller dragged into a transaction, which
    # is the shape a layered codebase actually has: the service module does
    # not know, and cannot know, who called it.
    graph = callgraph.build(root)
    reached = callgraph.inside_transaction(graph)
    seen = {(f["file"], f["line"]) for f in findings}

    for qualname, path in sorted(reached.items()):
        fn = graph.functions.get(qualname)
        if fn is None:
            continue
        full = root / fn.file
        try:
            source = read_source(full)
            tree = parse_file(full)
        except (OSError, SyntaxError):
            continue
        lines = source.splitlines()

        subtree = _function_node(tree, fn.name, fn.line)
        if subtree is None:
            continue

        # base_depth=1: the caller's transaction is already open around every
        # line of this function.
        visitor = _Visitor(base_depth=1)
        for child in subtree.body:
            visitor.visit(child)

        for finding in visitor.findings:
            if finding["confidence"] == "low" and not include_low_confidence:
                continue
            key = (fn.file, finding["line"])
            if key in seen:
                continue
            seen.add(key)
            finding["file"] = fn.file
            finding["code"] = (
                lines[finding["line"] - 1].strip()[:140]
                if finding["line"] <= len(lines) else ""
            )
            finding["fix"] = finding["fix"].replace("{call}", finding["code"].rstrip(","))
            finding["inside"] = f"a transaction opened by {path[0]}"
            finding["reached_through"] = path
            findings.append(finding)

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["file"], f["line"]))

    by_kind: dict[str, int] = {}
    for finding in findings:
        by_kind[finding["kind"]] = by_kind.get(finding["kind"], 0) + 1

    return {
        "search_path": str(root),
        "files_scanned": files_scanned,
        "atomic_requests_databases": atomic_request_dbs,
        "finding_count": len(findings),
        "high_severity_count": sum(1 for f in findings if f["severity"] == "high"),
        "by_kind": by_kind,
        "cross_module_count": sum(1 for f in findings if f.get("reached_through")),
        "atomic_request_views": atomic_request_views,
        "atomic_request_view_count": len(atomic_request_views),
        "call_graph": callgraph.summary(graph),
        "findings": findings,
        "note": _note(atomic_request_dbs),
    }


def _note(atomic_request_dbs: list[str]) -> str:
    base = (
        "Two different defects are collected here. A task handed to a broker "
        "inside a transaction can be picked up before the commit, so the worker "
        "queries for a row that does not exist yet; that one is a race, it "
        "passes every test, and it fails under load. An email or an outbound "
        "request cannot be rolled back at all, so if the transaction fails "
        "afterwards the outside world has been told about something that never "
        "happened. Both are fixed the same way, by deferring the call to "
        "transaction.on_commit.\n\n"
        "This reads the source, so it sees the transaction boundaries written "
        "in the file it is looking at. A function that opens a transaction and "
        "then calls a helper in another module which sends the mail is not "
        "visible here: the boundary and the call are in different files and "
        "nothing in the second file says it is inside a transaction."
    )
    if atomic_request_dbs:
        return (
            f"ATOMIC_REQUESTS is on for: {', '.join(atomic_request_dbs)}. Every "
            "view is therefore wrapped in a transaction with no atomic() block "
            "anywhere in sight, so the findings below are the visible cases and "
            "every send in every view has the same problem without looking like "
            "it. That setting is the single highest-value thing to know here.\n\n"
            + base
        )
    return base
