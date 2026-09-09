# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""Two seconds spent finding out, instead of nine minutes waiting.

Measured on DefectDojo. With the database pointed at a SQLite file the
serializer check costs 28 s. With the same code pointed at the host its own
settings default to - `postgres:3306`, a Docker service name that does not
resolve outside Docker - it costs 567 s, of which 26 s is CPU:

    dojo.location.api.endpoint_compat   440.5 s wall / 0.14 s cpu
    dojo.api_v2.serializers             100.9 s wall / 0.85 s cpu

Both touch the database while being imported. The tool has to import them to
read them, and cannot interrupt an import safely, so the only thing left is to
find out in two seconds that the host is not there and say so first.
"""

from __future__ import annotations

import socket
import threading

import pytest

from django_chainsaw_mcp.django_env import database_reachable


@pytest.fixture
def listening_port():
    """A real socket, so "reachable" is tested against something reachable."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    stop = threading.Event()

    def accept():
        server.settimeout(0.2)
        while not stop.is_set():
            try:
                connection, _ = server.accept()
            except OSError:
                continue
            connection.close()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield port
    stop.set()
    thread.join(timeout=2)
    server.close()


def _with_database(monkeypatch, **fields):
    from django.conf import settings

    monkeypatch.setattr(
        settings, "DATABASES", {"default": fields}, raising=False
    )


def test_a_database_that_answers_produces_no_warning(monkeypatch, django_project, listening_port):
    _with_database(
        monkeypatch,
        ENGINE="django.db.backends.postgresql",
        HOST="127.0.0.1",
        PORT=listening_port,
    )
    reachable, warning = database_reachable()
    assert reachable is True
    assert warning is None


def test_a_database_that_does_not_answer_is_named_with_the_cost(monkeypatch, django_project):
    # Port 1 is reserved and nothing listens on it.
    _with_database(
        monkeypatch,
        ENGINE="django.db.backends.postgresql",
        HOST="127.0.0.1",
        PORT=1,
    )
    reachable, warning = database_reachable(timeout=0.3)
    assert reachable is False
    assert "127.0.0.1:1" in warning
    assert "imported" in warning, "the warning has to say why this costs anything"
    assert "567" in warning, "the number is what makes somebody act on it"


def test_sqlite_is_always_reachable(monkeypatch, django_project):
    _with_database(monkeypatch, ENGINE="django.db.backends.sqlite3", NAME=":memory:")
    assert database_reachable() == (True, None)


def test_a_unix_socket_host_is_not_probed_by_tcp(monkeypatch, django_project):
    # `HOST=/var/run/postgresql` is a directory, not something to connect to,
    # and reporting it as unreachable would be a wrong warning about a working
    # database.
    _with_database(
        monkeypatch,
        ENGINE="django.db.backends.postgresql",
        HOST="/var/run/postgresql",
        PORT=5432,
    )
    assert database_reachable() == (True, None)


def test_an_engine_with_no_known_port_is_left_alone(monkeypatch, django_project):
    _with_database(monkeypatch, ENGINE="some.exotic.backend", HOST="db")
    assert database_reachable() == (True, None)


def test_the_port_the_project_configured_is_the_one_reported(monkeypatch, django_project):
    # DefectDojo really does ship `postgresql` on 3306. Substituting the
    # engine's usual port would have printed a number the project does not
    # use, and sent somebody to check the wrong thing.
    _with_database(
        monkeypatch,
        ENGINE="django.db.backends.postgresql",
        HOST="127.0.0.1",
        PORT=3306,
    )
    _, warning = database_reachable(timeout=0.3)
    assert warning is not None
    assert "3306" in warning
    assert "5432" not in warning


def test_the_probe_is_bounded(monkeypatch, django_project):
    # This runs before the analysis. A probe that can hang has replaced one
    # wait with another.
    import time

    _with_database(
        monkeypatch,
        ENGINE="django.db.backends.postgresql",
        HOST="10.255.255.1",  # non-routable: connect() hangs rather than refusing
        PORT=5432,
    )
    started = time.perf_counter()
    reachable, _ = database_reachable(timeout=0.5)
    elapsed = time.perf_counter() - started
    assert reachable is False
    assert elapsed < 3.0, f"the probe took {elapsed:.1f}s and is meant to be bounded"


def test_the_warning_reaches_a_full_run(django_project):
    from django_chainsaw_mcp.check import run_all

    report = run_all(tenant_root="shop.Customer", only=["money"])
    assert "database_warning" in report
    # The demo project is on SQLite, so there is nothing to warn about.
    assert report["database_warning"] is None
