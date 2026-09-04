"""Minimal Django settings for a throwaway project used to exercise the server.

Deliberately relation-heavy: foreign keys, a one-to-one and a many-to-many, so
the introspection output has something real to show.
"""

SECRET_KEY = "not-a-secret-this-project-only-exists-for-introspection-tests"
DEBUG = True
ALLOWED_HOSTS: list[str] = []

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "shop",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
