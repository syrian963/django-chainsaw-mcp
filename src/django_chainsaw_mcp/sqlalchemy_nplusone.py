# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Relationships loaded one row at a time.

    orders = db.query(Order).all()          # one query
    for order in orders:
        print(order.customer.name)          # one more, per order

SQLAlchemy fires a query the first time an unloaded relationship is touched.
On its own that is a feature. Inside a loop it is the N+1, and the loop and the
query are usually far enough apart that neither line looks wrong.

The FastAPI shape is quieter still, because there is no loop to see:

    @app.get("/orders", response_model=list[OrderOut])
    def list_orders(db=Depends(get_db)):
        return db.query(Order).all()

`OrderOut` declares `items: list[ItemOut]`, so **serialisation** walks the
relationship, once per order, after the endpoint has returned. Nothing in the
function body touches `items` at all.

## What exists already

`nplusone` finds this at runtime, by watching lazy loads as they happen, so it
covers whatever the tests exercise. `lazy="raise"` turns it into an exception
at runtime, which is the right fix and still needs the code path to run. The
documentation's own advice is to lock it in with query-count tests.

Nothing reads it out of the source, and both halves are there: the query says
what it eagerly loaded, the model says which attributes are relationships, and
the loop or the response model says which ones get touched.

## Nothing is imported

The same reason as the rest of the FastAPI support: a project wanting a
database URL before it will import is not a project this can boot. Models,
relationships and loader options are all read from the AST.
"""

from __future__ import annotations

import ast
from typing import Any

from .project import get_profile, parse_file, read_source, resolve_root

_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", "dist", "build",
}

# Loader options that make a relationship eager for one query.
_EAGER_OPTIONS = {"selectinload", "joinedload", "subqueryload", "immediateload",
                  "contains_eager", "raiseload", "lazyload"}
_TRULY_EAGER = {"selectinload", "joinedload", "subqueryload", "immediateload",
                "contains_eager"}

# `lazy=` values on the relationship itself that load it up front every time.
_EAGER_LAZY = {"selectin", "joined", "subquery", "immediate", "raise", "raise_on_sql"}

# Calls that hand back rows.
_QUERY_TERMINALS = {"all", "first", "one", "one_or_none", "scalar", "scalars",
                    "get", "unique", "partitions"}


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


def _annotation_target(node: ast.AST | None) -> str | None:
    """`Mapped[list["Item"]]` -> "Item"."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip("\"'")
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _annotation_target(node.slice) or _annotation_target(node.value)
    return None


class _ModelIndex(ast.NodeVisitor):
    """SQLAlchemy models and the relationships declared on them."""

    def __init__(self) -> None:
        # model name -> {attribute: {"target": str|None, "lazy": str|None}}
        self.relationships: dict[str, dict[str, dict[str, Any]]] = {}
        # Pydantic-shaped classes -> declared field names
        self.schemas: dict[str, list[str]] = {}

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = [_dotted(b).split(".")[-1] for b in node.bases]
        rels: dict[str, dict[str, Any]] = {}
        fields: list[str] = []
        has_tablename = False

        for statement in node.body:
            targets: list[str] = []
            value: ast.AST | None = None
            annotation: ast.AST | None = None

            if isinstance(statement, ast.Assign):
                targets = [t.id for t in statement.targets if isinstance(t, ast.Name)]
                value = statement.value
            elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                targets = [statement.target.id]
                value = statement.value
                annotation = statement.annotation
                fields.append(statement.target.id)

            if "__tablename__" in targets:
                has_tablename = True

            called = ""
            if isinstance(value, ast.Call):
                called = _dotted(value.func).split(".")[-1]
            if called != "relationship":
                continue

            lazy = None
            for keyword in value.keywords:  # type: ignore[union-attr]
                if keyword.arg == "lazy" and isinstance(keyword.value, ast.Constant):
                    lazy = keyword.value.value

            target = None
            if value.args and isinstance(value.args[0], ast.Constant):  # type: ignore[union-attr]
                target = str(value.args[0].value)  # type: ignore[union-attr]
            if target is None:
                target = _annotation_target(annotation)

            for name in targets:
                rels[name] = {"target": target, "lazy": lazy}

        if rels or has_tablename:
            self.relationships[node.name] = rels
        looks_schema = any(
            base in {"BaseModel", "SQLModel"} or base.endswith(("Schema", "Out", "Model"))
            for base in bases
        )
        if looks_schema and fields:
            self.schemas[node.name] = fields

        self.generic_visit(node)


def _eager_paths(node: ast.AST) -> set[str]:
    """Attribute names made eager by `.options(selectinload(Order.items))`."""
    loaded: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _dotted(child.func).split(".")[-1]
        if name not in _TRULY_EAGER:
            continue
        for argument in child.args:
            attribute = _dotted(argument)
            if attribute:
                loaded.add(attribute.split(".")[-1])
        # selectinload(Order.items).selectinload(Item.product) chains, and the
        # walk above reaches every link of it.
    return loaded


def _queried_model(node: ast.AST) -> str | None:
    """`db.query(Order)...` or `select(Order)` -> "Order"."""
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _dotted(child.func).split(".")[-1]
        if name not in {"query", "select", "get"}:
            continue
        for argument in child.args:
            if isinstance(argument, ast.Name) and argument.id[:1].isupper():
                return argument.id
    return None


def _is_query(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
            if child.func.attr in _QUERY_TERMINALS:
                return True
        if isinstance(child, ast.Call):
            if _dotted(child.func).split(".")[-1] in {"query", "select"}:
                return True
    return False


class _FunctionScan(ast.NodeVisitor):
    """Where rows come from in one function, and which relationships get touched."""

    def __init__(self, relationships: dict[str, dict[str, dict[str, Any]]]) -> None:
        self.relationships = relationships
        # variable -> {"model": str, "eager": set[str], "line": int}
        self.bound: dict[str, dict[str, Any]] = {}
        self.hits: list[dict[str, Any]] = []
        self.returned: list[dict[str, Any]] = []

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = node.value
            if isinstance(value, ast.Call) and _is_query(value):
                model = _queried_model(value)
                if model and model in self.relationships:
                    self.bound[node.targets[0].id] = {
                        "model": model,
                        "eager": _eager_paths(value),
                        "line": node.lineno,
                    }
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        source = node.iter
        name = source.id if isinstance(source, ast.Name) else None
        info = self.bound.get(name) if name else None

        if info is not None and isinstance(node.target, ast.Name):
            loop_var = node.target.id
            model = info["model"]
            for child in ast.walk(node):
                if not isinstance(child, ast.Attribute):
                    continue
                if not (isinstance(child.value, ast.Name) and child.value.id == loop_var):
                    continue
                attribute = child.attr
                relationship = self.relationships.get(model, {}).get(attribute)
                if relationship is None:
                    continue
                if attribute in info["eager"]:
                    continue
                if (relationship.get("lazy") or "select") in _EAGER_LAZY:
                    continue
                self.hits.append({
                    "line": child.lineno,
                    "model": model,
                    "attribute": attribute,
                    "target": relationship.get("target"),
                    "queried_at": info["line"],
                    "loaded_eagerly": sorted(info["eager"]),
                })
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return) -> None:
        if isinstance(node.value, ast.Name):
            info = self.bound.get(node.value.id)
            if info is not None:
                self.returned.append({**info, "line": node.lineno})
        elif isinstance(node.value, ast.Call) and _is_query(node.value):
            model = _queried_model(node.value)
            if model and model in self.relationships:
                self.returned.append({
                    "model": model,
                    "eager": _eager_paths(node.value),
                    "line": node.lineno,
                })
        self.generic_visit(node)


def _route_response_model(node: ast.AST) -> str | None:
    from .fastapi_exposure import _annotation_name, _route_decorator

    for decorator in node.decorator_list:
        route = _route_decorator(decorator)
        if route is None:
            continue
        return route["response_model"] or _annotation_name(node.returns)
    return None


def sqlalchemy_nplusone(search_path: str | None = None) -> dict[str, Any]:
    """Relationships SQLAlchemy will load one row at a time.

    Args:
        search_path: directory to scan. Defaults to the configured project.
    """
    root = resolve_root(search_path)
    found = get_profile(root)

    index = _ModelIndex()
    trees: dict[str, tuple[ast.AST, list[str]]] = {}

    for path in root.rglob("*.py"):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        try:
            source = read_source(path)
            tree = parse_file(path)
        except (OSError, SyntaxError):
            continue
        trees[str(path.relative_to(root))] = (tree, source.splitlines())
        index.visit(tree)

    in_loops: list[dict[str, Any]] = []
    in_responses: list[dict[str, Any]] = []

    for relative, (tree, lines) in sorted(trees.items()):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            scan = _FunctionScan(index.relationships)
            for child in node.body:
                scan.visit(child)

            for hit in scan.hits:
                in_loops.append({
                    "file": relative,
                    "line": hit["line"],
                    "code": lines[hit["line"] - 1].strip()[:140] if hit["line"] <= len(lines) else "",
                    "function": node.name,
                    "model": hit["model"],
                    "attribute": hit["attribute"],
                    "queried_at_line": hit["queried_at"],
                    "loaded_eagerly": hit["loaded_eagerly"],
                    "severity": "high",
                    "why": (
                        f"{hit['model']}.{hit['attribute']} is a relationship and the "
                        f"query on line {hit['queried_at']} did not load it, so touching "
                        "it inside the loop fires one query per row"
                    ),
                    "fix": f'.options(selectinload({hit["model"]}.{hit["attribute"]}))'
                           " on that query",
                })

            response_model = _route_response_model(node)
            if response_model is None:
                continue
            declared = index.schemas.get(response_model)
            if not declared:
                continue
            for returned in scan.returned:
                model = returned["model"]
                for attribute in declared:
                    relationship = index.relationships.get(model, {}).get(attribute)
                    if relationship is None:
                        continue
                    if attribute in returned["eager"]:
                        continue
                    if (relationship.get("lazy") or "select") in _EAGER_LAZY:
                        continue
                    in_responses.append({
                        "file": relative,
                        "line": returned["line"],
                        "function": node.name,
                        "response_model": response_model,
                        "model": model,
                        "attribute": attribute,
                        "loaded_eagerly": sorted(returned["eager"]),
                        "severity": "high",
                        "why": (
                            f"{response_model} declares '{attribute}', so serialising the "
                            f"response walks {model}.{attribute} once per row - after this "
                            "function has returned, which is why nothing in the body "
                            "touches it"
                        ),
                        "fix": f'.options(selectinload({model}.{attribute}))'
                               " on the query this returns",
                    })

    in_loops.sort(key=lambda f: (f["file"], f["line"]))
    in_responses.sort(key=lambda f: (f["file"], f["line"]))

    return {
        "search_path": str(root),
        "frameworks": dict(found.frameworks),
        "models_with_relationships": sum(1 for r in index.relationships.values() if r),
        "relationship_count": sum(len(r) for r in index.relationships.values()),
        "in_loop_count": len(in_loops),
        "in_response_model_count": len(in_responses),
        "finding_count": len(in_loops) + len(in_responses),
        "in_loops": in_loops,
        "in_response_models": in_responses,
        "note": (
            "SQLAlchemy fires a query the first time an unloaded relationship is "
            "touched. In a loop that is the N+1; in a response model it is the "
            "same thing with no loop to see, because serialisation walks the "
            "relationship after the endpoint has returned and nothing in the "
            "function body mentions it.\n\n"
            "nplusone finds this at runtime by watching lazy loads happen, and "
            "lazy='raise' turns it into an exception - both need the code path "
            "to run. Both halves are in the source: the query says what it "
            "eagerly loaded, the model says which attributes are relationships.\n\n"
            "A relationship declared lazy='selectin', 'joined', 'subquery' or "
            "'raise' is never reported: the first three are already eager and "
            "the fourth turns the mistake into an exception, which is the "
            "recommended fix. A query whose model cannot be read from the call, "
            "and a variable that crosses a function boundary, are both invisible."
        ),
    }
