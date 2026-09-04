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


@dataclass(frozen=True)
class BootConfig:
    project_path: Path
    settings_module: str

    @classmethod
    def from_env(cls) -> "BootConfig":
        raw_path = os.environ.get(PROJECT_PATH_VAR)
        settings_module = os.environ.get(SETTINGS_MODULE_VAR)

        missing = [
            name
            for name, value in ((PROJECT_PATH_VAR, raw_path), (SETTINGS_MODULE_VAR, settings_module))
            if not value
        ]
        if missing:
            raise DjangoBootError(
                "Missing environment variable(s): "
                + ", ".join(missing)
                + ". Set them in the MCP client config, for example "
                f'"{PROJECT_PATH_VAR}": "/path/to/project", '
                f'"{SETTINGS_MODULE_VAR}": "myproject.settings".'
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
