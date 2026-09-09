# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Boot an arbitrary Django project inside this process.

This is the load-bearing part of the server. Every tool that inspects models,
migrations or querysets needs a populated Django app registry, so it happens
once, lazily, and any failure is reported as a readable message instead of
taking the server down.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

# Env vars the MCP client sets when it launches the server.
PROJECT_PATH_VAR = "DJANGO_CHAINSAW_PROJECT_PATH"
SETTINGS_MODULE_VAR = "DJANGO_CHAINSAW_SETTINGS_MODULE"


class DjangoBootError(RuntimeError):
    """Raised when the target project cannot be loaded."""


def database_reachable(timeout: float = 2.0) -> tuple[bool, str | None]:
    """Whether the configured default database answers a TCP connect.

    Not a query and not a Django connection: a socket, opened and closed, with
    a short timeout. The point is to fail fast where Django's own driver would
    wait, and to be able to say so before the analysis starts.

    A file-backed engine - SQLite - is always reachable, and an engine this
    cannot read the host from is reported as unknown rather than unreachable,
    because a wrong warning about a working database is worse than none.
    """
    import socket

    try:
        from django.conf import settings
    except Exception:
        return True, None

    try:
        default = settings.DATABASES.get("default") or {}
    except Exception:
        return True, None

    engine = str(default.get("ENGINE", ""))
    if "sqlite" in engine or not engine:
        return True, None

    host = default.get("HOST") or "localhost"
    port = default.get("PORT")
    if not port:
        port = {"postgresql": 5432, "postgis": 5432, "mysql": 3306,
                "oracle": 1521}.get(engine.rsplit(".", 1)[-1])
    if not port:
        return True, None

    # A unix socket path in HOST is not something to connect to by TCP.
    if str(host).startswith("/"):
        return True, None

    try:
        with socket.create_connection((str(host), int(port)), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, (
            f"The database this project is configured to use - {host}:{port} - "
            f"did not answer within {timeout:g}s ({exc.__class__.__name__}). "
            "Nothing here needs it to run, but any module that touches the "
            "database while being imported will wait out a connect timeout, "
            "and this tool imports the modules that declare serializers, "
            "views and URLs. On one real project that turned a 28-second "
            "check into a 567-second one, of which 26 seconds was actual "
            "work. Point the settings at a reachable database, or at a "
            "SQLite file, before reading any timing from this run."
        )


def _configures_settings_in_code(root: Path) -> str | None:
    """The file where this project configures Django without a settings module.

    `settings.configure(...)` in a conftest is how a library boots Django for
    its own tests - django-rest-framework does it, and so there is nothing to
    put in DJANGO_CHAINSAW_SETTINGS_MODULE. Saying "you did not set the
    variable" sends somebody looking for a file that was never written, so
    when the pattern is there the error says so instead.

    Only the few places it is conventionally written are read, and only the
    first hit matters: this runs on a failure path and must not turn a missing
    variable into a scan of the whole tree.
    """
    candidates = [
        root / "conftest.py",
        root / "tests" / "conftest.py",
        root / "setup.py",
        root / "runtests.py",
        root / "tests" / "runtests.py",
    ]
    for path in candidates:
        try:
            if "settings.configure(" in path.read_text(encoding="utf-8", errors="ignore"):
                return str(path.relative_to(root))
        except OSError:
            continue
    return None


@dataclass(frozen=True)
class BootConfig:
    project_path: Path
    settings_module: str

    @classmethod
    def from_env(cls) -> BootConfig:
        raw_path = os.environ.get(PROJECT_PATH_VAR)
        settings_module = os.environ.get(SETTINGS_MODULE_VAR)

        missing = [
            name
            for name, value in ((PROJECT_PATH_VAR, raw_path), (SETTINGS_MODULE_VAR, settings_module))
            if not value
        ]
        if missing:
            hint = ""
            if SETTINGS_MODULE_VAR in missing and raw_path:
                where = _configures_settings_in_code(Path(raw_path).expanduser())
                if where:
                    hint = (
                        f" This project has no settings module to point at: "
                        f"{where} calls settings.configure() instead, which is "
                        "how a library configures Django for its own test run. "
                        "Point this at an application that uses the library, "
                        "or at a settings module of your own that imports the "
                        "same apps."
                    )
            raise DjangoBootError(
                "Missing environment variable(s): "
                + ", ".join(missing)
                + ". Set them in the MCP client config, for example "
                f'"{PROJECT_PATH_VAR}": "/path/to/project", '
                f'"{SETTINGS_MODULE_VAR}": "myproject.settings".'
                + hint
            )

        project_path = Path(raw_path).expanduser().resolve()
        if not project_path.is_dir():
            raise DjangoBootError(f"{PROJECT_PATH_VAR} is not a directory: {project_path}")

        return cls(project_path=project_path, settings_module=settings_module)


_lock = threading.Lock()
_booted: BootConfig | None = None


def ensure_django(config: BootConfig | None = None) -> BootConfig:
    """Populate the Django app registry exactly once.

    Returns the config that was used. Calling this again with a different
    config raises, because a process can only ever host one Django project:
    ``django.setup()`` mutates global state and there is no way back.
    """
    global _booted

    with _lock:
        if _booted is not None:
            if config is not None and config != _booted:
                raise DjangoBootError(
                    "This server is already bound to "
                    f"{_booted.settings_module} at {_booted.project_path}. "
                    "Django cannot be re-initialised in the same process; "
                    "start a second server instance for another project."
                )
            return _booted

        resolved = config or BootConfig.from_env()

        if str(resolved.project_path) not in sys.path:
            sys.path.insert(0, str(resolved.project_path))
        os.environ["DJANGO_SETTINGS_MODULE"] = resolved.settings_module

        try:
            import django
        except ModuleNotFoundError as exc:
            raise DjangoBootError(
                "Django is not importable from this interpreter. Install the "
                "target project's dependencies into the environment that runs "
                "this server."
            ) from exc

        try:
            django.setup()
        except Exception as exc:
            raise DjangoBootError(
                f"django.setup() failed for settings module "
                f"'{resolved.settings_module}' at {resolved.project_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        _booted = resolved
        return _booted


def django_version() -> str:
    ensure_django()
    import django

    return django.get_version()


def reset_for_tests() -> None:
    """Only for the test suite. Django itself is not actually unloaded."""
    global _booted
    with _lock:
        _booted = None
