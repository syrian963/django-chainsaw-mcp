# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Trace what actually happens when a model is saved or deleted.

Signals are the most invisible thing in a Django codebase. Nothing at the call
site hints that `order.save()` also writes an Invoice, clears a cache and
queues a Celery task, because the receiver lives in another module and was
connected in an AppConfig.ready() nobody reads twice.

Tools that list registered receivers exist. dj-signals-panel and the debug
toolbar both show what is connected to what. None of them follow the chain:
that a post_save receiver on Order calls invoice.save(), which fires post_save
on Invoice, which queues a task. The second hop is where the surprise lives.

So this resolves receivers from the live signal registry, reads each receiver's
body with the AST, works out which models it writes and what else it triggers,
and then follows those writes into their own signals.

It also closes a blind spot delete_impact documents about itself: that graph
walk deliberately ignores signals, and this is the other half.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from pathlib import Path
from typing import Any

from .django_env import ensure_django

_SIGNAL_NAMES = ("pre_save", "post_save", "pre_delete", "post_delete", "m2m_changed")

# Calls worth reporting even though they are not model writes.
_SIDE_EFFECTS = {
    "delay": ("celery task", "queues background work"),
    "apply_async": ("celery task", "queues background work"),
    "send": ("signal or message", "may fan out further"),
    "send_mail": ("email", "outbound network call"),
    "send_messages": ("email", "outbound network call"),
    "post": ("http request", "outbound network call"),
    "put": ("http request", "outbound network call"),
    "patch": ("http request", "outbound network call"),
    "get": ("http request or orm read", "ambiguous, check the receiver"),
    "set": ("cache write", "cache mutation"),
    "delete_pattern": ("cache invalidation", "cache mutation"),
    "invalidate": ("cache invalidation", "cache mutation"),
}

_WRITE_METHODS = {"save", "create", "delete", "update", "bulk_create", "get_or_create",
                  "update_or_create", "bulk_update", "add", "remove", "set"}


def _conditional_lines(tree: ast.AST) -> set[int]:
    """Lines that only run when a branch is taken.

    A write inside `if created:` happens on insert and not on update. Reporting
    it as unconditional overstates the chain, and the chain's whole value is
    that somebody trusts it.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp)):
            body = list(getattr(node, "body", []) or [])
            body += list(getattr(node, "orelse", []) or [])
            for child in body:
                for inner in ast.walk(child) if isinstance(child, ast.AST) else []:
                    if hasattr(inner, "lineno"):
                        out.add(inner.lineno)
    return out


class _ReceiverBody(ast.NodeVisitor):
    """What a receiver function does, as far as it can be read statically."""

    def __init__(self, model_names: dict[str, str], sender: Any | None = None) -> None:
        self.model_names = model_names
        self.sender = sender
        self.writes: list[dict[str, Any]] = []
        self.effects: list[dict[str, Any]] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            attr = func.attr

            if attr in _WRITE_METHODS:
                target, chain = self._resolve_target(func.value)
                resolved = self.model_names.get(target or "") or self._follow_instance(chain)
                # `set` and `get` mean one thing on a related manager and
                # another on a cache. Without a model behind the call there is
                # no reason to call it a write, and the side-effect branch
                # below already reports it correctly.
                ambiguous = attr in _SIDE_EFFECTS and resolved is None
                if not ambiguous:
                    self.writes.append(
                        {
                            "method": attr,
                            "target": ".".join([target, *chain]) if target else None,
                            "line": node.lineno,
                            "resolved_model": resolved,
                            "resolved_via": "relation on the sender" if chain and resolved else "class name",
                        }
                    )

            if attr in _SIDE_EFFECTS:
                kind, why = _SIDE_EFFECTS[attr]
                self.effects.append(
                    {"call": attr, "kind": kind, "why": why, "line": node.lineno}
                )

        self.generic_visit(node)

    def _resolve_target(self, node: ast.AST) -> tuple[str | None, list[str]]:
        """Flatten the receiver of a call into a base name and its attribute path.

        Order.objects.create(...)   -> ("Order",    [])
        instance.order.save()       -> ("instance", ["order"])
        """
        attributes: list[str] = []
        while isinstance(node, ast.Attribute):
            if node.attr not in {"objects", "_default_manager"}:
                attributes.append(node.attr)
            node = node.value
        attributes.reverse()
        base = node.id if isinstance(node, ast.Name) else None
        return base, attributes

    def _follow_instance(self, attributes: list[str]) -> str | None:
        """Resolve `instance.order` through the sender's own relations.

        A receiver's `instance` is an object of the sending model, so the model
        graph can say what `instance.order` is. Without this the chain stops at
        the first write that goes through a relation instead of a manager, and
        `instance.order.save()` is a very ordinary thing to write.
        """
        if self.sender is None or not attributes:
            return None

        current = self.sender
        for step in attributes:
            try:
                field = current._meta.get_field(step)
            except Exception:
                return None
            if not field.is_relation or field.related_model is None:
                return None
            current = field.related_model
        return current._meta.label


def _receivers_for(signal: Any, model: Any) -> list[Any]:
    """Live receivers connected for this sender.

    _live_receivers is private but is the only way to resolve the sender-keyed
    registry back to callables. Falling back to the raw list would report every
    receiver in the project for every model, which is worse than nothing.
    """
    try:
        receivers = signal._live_receivers(sender=model)
    except Exception:
        return []
    # Django 5+ returns (sync_receivers, async_receivers).
    if isinstance(receivers, tuple) and len(receivers) == 2:
        sync, asynchronous = receivers
        return list(sync) + list(asynchronous)
    return list(receivers)


def _overrides_for(model: Any, event: str) -> list[tuple[str, Any]]:
    """A save() or delete() the model defines itself, or inherits from a mixin.

    These are not signals. They are ordinary methods, they run on every write,
    and they were previously invisible here - which made the chain look
    complete when the largest effect in it was a `save()` on a base class that
    writes an audit row. "Nothing else happens" is the one answer this tool
    must never give wrongly.
    """
    from django.db.models import Model

    base = getattr(Model, event, None)
    out: list[tuple[str, Any]] = []
    for klass in model.__mro__:
        if klass is Model or klass is object:
            break
        own = klass.__dict__.get(event)
        if own is None or own is base:
            continue
        out.append((f"{klass.__module__}.{klass.__qualname__}.{event}()", own))
    return out


def _duplicate_receivers(receivers: list[Any]) -> list[dict[str, Any]]:
    """The same function connected more than once to the same signal and sender.

    Usually a `@receiver` decorator in a module that gets imported twice under
    different names, or a connect() call in a `ready()` that runs again. The
    symptom is a side effect happening twice, which reads as a race and is not.
    """
    counts: dict[str, int] = {}
    for receiver in receivers:
        name = "{}.{}".format(
            getattr(receiver, "__module__", "?"),
            getattr(receiver, "__qualname__", getattr(receiver, "__name__", repr(receiver))),
        )
        counts[name] = counts.get(name, 0) + 1
    return [
        {"receiver": name, "connected": count,
         "consequence": f"everything it does happens {count} times per event"}
        for name, count in sorted(counts.items()) if count > 1
    ]


def _describe(receiver: Any, model_names: dict[str, str], sender: Any = None) -> dict[str, Any]:
    name = getattr(receiver, "__qualname__", getattr(receiver, "__name__", repr(receiver)))
    module = getattr(receiver, "__module__", "?")
    entry: dict[str, Any] = {"receiver": f"{module}.{name}", "writes": [], "side_effects": []}

    try:
        source_file = inspect.getsourcefile(receiver)
        source, first_line = inspect.getsourcelines(receiver)
        entry["source"] = f"{Path(source_file).name}:{first_line}" if source_file else None
    except (OSError, TypeError):
        entry["unreadable"] = "source not available, likely a lambda or C callable"
        return entry

    try:
        tree = ast.parse(textwrap.dedent("".join(source)))
    except SyntaxError:
        entry["unreadable"] = "receiver source could not be parsed"
        return entry

    visitor = _ReceiverBody(model_names, sender)
    visitor.visit(tree)

    # `if created:` is the commonest guard in a post_save receiver, and a chain
    # that presents a guarded write as unconditional is overstating itself.
    conditional = _conditional_lines(tree)
    for item in list(visitor.writes) + list(visitor.effects):
        line = item.get("line")
        item["conditional"] = bool(line and line in conditional)

    entry["writes"] = visitor.writes
    entry["side_effects"] = visitor.effects
    entry["conditional_count"] = sum(
        1 for i in visitor.writes + visitor.effects if i.get("conditional")
    )
    return entry


def what_happens_on(
    model_label: str,
    event: str = "save",
    max_depth: int = 4,
) -> dict[str, Any]:
    """Follow the signal chain triggered by saving or deleting a model.

    Args:
        model_label: "app_label.ModelName".
        event: "save" or "delete".
        max_depth: how far to follow writes into further signals.
    """
    ensure_django()
    from django.apps import apps
    from django.db.models import signals as django_signals

    if event not in {"save", "delete"}:
        raise ValueError(f"event must be 'save' or 'delete', got {event!r}")

    try:
        root = apps.get_model(model_label)
    except (LookupError, ValueError) as exc:
        known = sorted(m._meta.label for m in apps.get_models())
        raise ValueError(
            f"Unknown model '{model_label}'. Known: {', '.join(known[:40])}"
        ) from exc

    model_names = {m.__name__: m._meta.label for m in apps.get_models()}
    wanted = [f"pre_{event}", f"post_{event}"]

    steps: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    visited: set[str] = set()
    truncated = False

    def walk(model: Any, depth: int, path: list[str]) -> None:
        nonlocal truncated
        label = model._meta.label
        if depth > max_depth:
            truncated = True
            return
        if label in visited:
            steps.append(
                {
                    "depth": depth,
                    "model": label,
                    "path": " -> ".join(path),
                    "note": "already traced on another branch, not expanded again",
                }
            )
            return
        visited.add(label)

        # An overridden save()/delete() runs on every write, before any
        # post_save receiver does. It belongs in the chain.
        for label_text, method in _overrides_for(model, event):
            described = _describe(method, model_names, sender=model)
            described.update({
                "depth": depth,
                "signal": f"{event}() override",
                "kind": "override",
                "receiver": label_text,
                "on_model": label,
                "path": " -> ".join(path),
            })
            steps.append(described)
            for write in described.get("writes", []):
                target_label = write.get("resolved_model")
                if not target_label or target_label == label:
                    continue
                walk(apps.get_model(target_label), depth + 1,
                     [*path, f"{label_text} -> {target_label}"])

        for signal_name in wanted:
            signal = getattr(django_signals, signal_name, None)
            if signal is None:
                continue
            connected = _receivers_for(signal, model)
            for entry in _duplicate_receivers(connected):
                duplicates.append({**entry, "signal": signal_name, "on_model": label})
            for receiver in connected:
                described = _describe(receiver, model_names, sender=model)
                described.update(
                    {"depth": depth, "signal": signal_name, "on_model": label,
                     "path": " -> ".join(path)}
                )
                steps.append(described)

                for write in described.get("writes", []):
                    target_label = write.get("resolved_model")
                    if not target_label or target_label == label:
                        continue
                    child = apps.get_model(target_label)
                    walk(child, depth + 1, [*path, f"{described['receiver']} -> {target_label}"])

    walk(root, 0, [root._meta.label])

    receivers = [s for s in steps if "receiver" in s]
    all_effects = [
        {**effect, "receiver": step["receiver"], "on_model": step["on_model"]}
        for step in receivers
        for effect in step.get("side_effects", [])
    ]
    unreadable = [s["receiver"] for s in receivers if s.get("unreadable")]

    return {
        "model": root._meta.label,
        "event": event,
        "signals_checked": wanted,
        "receiver_count": len([s for s in receivers if s.get("kind") != "override"]),
        "override_count": len([s for s in receivers if s.get("kind") == "override"]),
        "duplicate_receivers": duplicates,
        "max_depth_reached": truncated,
        "chain": steps,
        "side_effects": all_effects,
        "models_written": sorted(
            {
                write["resolved_model"]
                for step in receivers
                for write in step.get("writes", [])
                if write.get("resolved_model")
            }
        ),
        "unreadable_receivers": unreadable,
        "note": (
            "Receivers are read from the live signal registry, so anything "
            "connected at import time is included. Their bodies are parsed "
            "statically: a write behind a condition is reported as if it always "
            "happens, and a write through a variable this cannot resolve is "
            "reported without a model. Receivers connected at runtime, "
            "dynamically dispatched calls, and overridden save() methods that "
            "write other models are not visible."
        ),
    }
