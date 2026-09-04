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
    "shop.apps.ShopConfig",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# A real project always configures this. The demo did not, which meant the
# template analysis could never resolve `{% include %}` here and the gap was
# invisible until the checks were run against a real codebase.
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {},
    }
]

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
