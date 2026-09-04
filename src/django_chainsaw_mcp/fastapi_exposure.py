# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""What a FastAPI endpoint actually returns, which is often everything.

    @app.get("/users/{pk}")
    async def get_user(pk: int):
        return session.get(User, pk)

There is no `response_model` and no return annotation, so FastAPI serialises
whatever it is handed. That is the ORM object, with every column on it -
`password_hash`, `reset_token`, `is_superuser`, the internal notes. The
endpoint reads as three lines of nothing.

This is the FastAPI shape of `fields = "__all__"`, and it is worse in one way:
a Django serializer at least lists what it exposes somewhere. Here the absence
of one line is the whole bug, so there is nothing to read and nothing to
review. It also gets worse on its own - the next column added to the model
joins the response without anybody touching this file.

Every FastAPI guide says to set `response_model`. Several say a CI rule should
enforce it. No linter ships one, so this does.

## Read from the source, never imported

A FastAPI project is often not importable in the environment doing the
analysis: it wants a database URL, a settings object, a secret. All of this is
in the decorators and the annotations, so nothing here imports the project.
That also means a `response_model` computed at runtime is unreadable, and it
is reported as unknown rather than guessed at.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from .project import get_profile, resolve_root
from .serializers import _SENSITIVE  # noqa: PLC2701 - one list of names, shared

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
}

_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Names a router or app is conventionally bound to. The decorator position
# already rules out `requests.get`, but a decorator called `cache.get` is not
# a route either.
_ROUTER_HINTS = ("app", "router", "api", "v1", "v2", "endpoint", "blueprint")

# Anything whose call looks like it establishes who is asking.
_AUTH_HINTS = re.compile(
    r"auth|current_user|get_user|login_required|require|permission|scope|"
    r"security|token|jwt|oauth|api_?key|admin|staff",
    re.I,
)

_RESPONSE_HELPERS = {"Response", "JSONResponse", "HTMLResponse", "FileResponse",
                     "StreamingResponse", "RedirectResponse", "PlainTextResponse"}


def _classify(field_name: str) -> str | None:
    for pattern, label in _SENSITIVE:
        if pattern.search(field_name):
            return label
    return None


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


def _annotation_name(node: ast.AST | None) -> str | None:
    """`UserOut`, `list[UserOut]`, `Optional[UserOut]` -> "UserOut"."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # A forward reference, written as a string.
        return node.value.strip("\"'").split("[")[-1].strip("]")
    if isinstance(node, ast.Subscript):
        inner = _annotation_name(node.slice)
        return inner or _annotation_name(node.value)
    if isinstance(node, ast.Tuple) and node.elts:
        return _annotation_name(node.elts[0])
    if isinstance(node, ast.BinOp):
        # `UserOut | None`
        return _annotation_name(node.left) or _annotation_name(node.right)
    return None


def _route_decorator(node: ast.AST) -> dict[str, Any] | None:
    """Is this decorator a route, and what does it declare?"""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    method = node.func.attr
    if method not in _HTTP_METHODS:
        return None
    receiver = _dotted(node.func.value).lower()
    if receiver and not any(hint in receiver for hint in _ROUTER_HINTS):
        return None

    path = None
    if node.args and isinstance(node.args[0], ast.Constant):
        path = node.args[0].value

    declared: str | None = None
    dynamic = False
    dependencies = False
    for keyword in node.keywords:
        if keyword.arg == "response_model":
            name = _annotation_name(keyword.value)
            if name is None:
                dynamic = True
            elif name == "None":
                declared = None
            else:
                declared = name
        elif keyword.arg == "dependencies":
            dependencies = True

    return {
        "method": method.upper(),
        "path": path,
        "response_model": declared,
        "response_model_dynamic": dynamic,
        "decorator_dependencies": dependencies,
    }


def _has_auth(node: ast.AST, decorator_dependencies: bool) -> tuple[bool, list[str]]:
    """Does anything about this endpoint establish who is calling?

    `Depends(get_current_user)` in the signature, a `Security(...)`, or a
    `dependencies=[...]` on the decorator. The name is matched loosely,
    because a dependency called `get_db` is not authentication and one called
    `require_admin` is.
    """
    seen: list[str] = []
    args = node.args
    for default in list(args.defaults) + [d for d in args.kw_defaults if d]:
        if not isinstance(default, ast.Call):
            continue
        called = _dotted(default.func)
        if called.split(".")[-1] not in {"Depends", "Security"}:
            continue
        inner = default.args[0] if default.args else None
        name = _dotted(inner) if inner is not None else ""
        if _AUTH_HINTS.search(name):
            seen.append(name)
    if decorator_dependencies:
        seen.append("dependencies=[...] on the route")
    return bool(seen), seen


class _ModelIndex(ast.NodeVisitor):
    """Pydantic-shaped classes and the field names they declare."""

    def __init__(self) -> None:
        self.models: dict[str, dict[str, Any]] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = [_dotted(b).split(".")[-1] for b in node.bases]
        looks_pydantic = any(
            base in {"BaseModel", "SQLModel"} or base.endswith(("Model", "Schema", "Base"))
            for base in bases
        )
        fields = [
            target.target.id
            for target in node.body
            if isinstance(target, ast.AnnAssign) and isinstance(target.target, ast.Name)
        ]
        if looks_pydantic or fields:
            self.models[node.name] = {
                "bases": bases,
                "fields": fields,
                "pydantic": looks_pydantic,
                "orm_like": any(b in {"Base", "DeclarativeBase"} or b.endswith("Base") for b in bases),
            }
        self.generic_visit(node)


def _returns_orm(node: ast.AST, models: dict[str, dict[str, Any]]) -> bool:
    """Does the body return something that is not a Pydantic model?

    A `return {...}` is a dict the author controls. A `return user` where the
    name came from a session lookup is the dangerous one, and that is what
    this looks for.
    """
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or child.value is None:
            continue
        value = child.value
        if isinstance(value, (ast.Dict, ast.List, ast.Constant)):
            continue
        if isinstance(value, ast.Call):
            called = _dotted(value.func).split(".")[-1]
            if called in _RESPONSE_HELPERS or called in models:
                continue
        return True
    return False


def fastapi_exposure(search_path: str | None = None) -> dict[str, Any]:
    """FastAPI endpoints that serialise more than they declare.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    root = resolve_root(search_path)
    found = get_profile(root)

    models: dict[str, dict[str, Any]] = {}
    trees: dict[str, tuple[ast.AST, list[str]]] = {}

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        trees[str(path.relative_to(root))] = (tree, source.splitlines())
        index = _ModelIndex()
        index.visit(tree)
        models.update(index.models)

    findings: list[dict[str, Any]] = []
    # Every route, with what it declares and whether anything authenticates it.
    # The findings alone are not the inventory: a bounded route with no
    # authentication produces no finding here and is still the interesting half
    # of an amplification question.
    inventory: list[dict[str, Any]] = []
    routes = 0

    for relative, (tree, lines) in sorted(trees.items()):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = None
            for decorator in node.decorator_list:
                route = _route_decorator(decorator)
                if route is not None:
                    break
            if route is None:
                continue
            routes += 1

            declared = route["response_model"] or _annotation_name(node.returns)
            authed, how = _has_auth(node, route["decorator_dependencies"])
            inventory.append({
                "file": relative,
                "line": node.lineno,
                "endpoint": node.name,
                "method": route["method"],
                "path": route["path"],
                "response_model": declared,
                "authenticated": authed,
                "auth_via": how,
            })

            if route["response_model_dynamic"]:
                findings.append(_finding(
                    relative, node, route, "unreadable_response_model", "medium", authed, how,
                    "the response_model is computed, so what this returns cannot be read here",
                    "nothing to change if it is deliberate; this is reported so a clean "
                    "result is not mistaken for a complete one",
                ))
                continue

            if declared is None:
                if not _returns_orm(node, models):
                    continue
                severity = "critical" if not authed else "high"
                findings.append(_finding(
                    relative, node, route, "no_response_model", severity, authed, how,
                    "no response_model and no return annotation, so FastAPI serialises "
                    "whatever the function returns, in full. When that is an ORM object "
                    "it is every column including the ones added next month; when it is "
                    "an upstream response it is whatever that service sent"
                    + ("" if authed else ", and nothing here establishes who is asking"),
                    "declare a response_model, or annotate the return type; FastAPI "
                    "uses either to decide what leaves the process",
                ))
                continue

            model = models.get(declared)
            if model is None:
                continue
            sensitive = [
                {"field": name, "why": _classify(name)}
                for name in model["fields"] if _classify(name)
            ]
            if not sensitive:
                continue
            severity = "critical" if not authed else "high"
            findings.append(_finding(
                relative, node, route, "sensitive_in_response_model", severity, authed, how,
                f"{declared} declares "
                + ", ".join(f"{s['field']} ({s['why']})" for s in sensitive)
                + ("" if authed else ", and nothing here establishes who is asking"),
                f"remove those fields from {declared}, or give this route a narrower model",
                extra={"response_model": declared, "sensitive_fields": [s["field"] for s in sensitive]},
            ))

    order = {"critical": 0, "high": 1, "medium": 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), f["file"], f["line"]))

    by_kind: dict[str, int] = {}
    for finding in findings:
        by_kind[finding["kind"]] = by_kind.get(finding["kind"], 0) + 1

    return {
        "search_path": str(root),
        "frameworks": dict(found.frameworks),
        "routes_found": routes,
        "routes": inventory,
        "unauthenticated_route_count": sum(1 for r in inventory if not r["authenticated"]),
        "models_found": len(models),
        "finding_count": len(findings),
        "critical_count": sum(1 for f in findings if f["severity"] == "critical"),
        "by_kind": by_kind,
        "findings": findings,
        "note": (
            "Without a response_model or a return annotation, FastAPI serialises "
            "whatever the endpoint returns. If that is an ORM object it is every "
            "column, including the ones added later, and the absence of one line "
            "is the whole bug - there is nothing in the file to read or review. "
            "An endpoint returning a dict or a literal is not reported: the "
            "author decided what goes in it.\n\n"
            "Whether a caller is authenticated is judged from Depends(...) and "
            "Security(...) in the signature, and from dependencies=[...] on the "
            "route, matched loosely by name - `get_db` is not authentication and "
            "`require_admin` is. An unauthenticated route with an unbounded "
            "response is critical; the same route behind a dependency is high, "
            "because it still leaks to everyone who can log in.\n\n"
            "Nothing here imports the project. A FastAPI app usually wants a "
            "database URL and a secret before it will import at all, and none of "
            "that is needed to read a decorator. The cost is that a "
            "response_model computed at runtime is unreadable, and it is "
            "reported as unknown rather than guessed at."
        ),
    }


def _finding(
    relative: str,
    node: ast.AST,
    route: dict[str, Any],
    kind: str,
    severity: str,
    authed: bool,
    how: list[str],
    why: str,
    fix: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "file": relative,
        "line": node.lineno,
        "endpoint": node.name,
        "method": route["method"],
        "path": route["path"],
        "kind": kind,
        "severity": severity,
        "authenticated": authed,
        "auth_via": how,
        "why": why,
        "fix": fix,
    }
    if extra:
        entry.update(extra)
    return entry
