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

# Some views exist nowhere but here. A plain function view carries no
# decorator and belongs to no class, so the URLconf is the only thing that
# knows it serves requests.
ROOT_URLCONF = "demoshop.urls"

# One entry names a task that exists, one names a task that was renamed. Beat
# keeps scheduling both; only one of them ever runs.
CELERY_BEAT_SCHEDULE = {
    "nightly-reconcile": {
        "task": "shop.tasks.reconcile",
        "schedule": 3600,
    },
    "nightly-cleanup": {
        "task": "shop.tasks.cleanup_old_orders",
        "schedule": 3600,
    },
}

USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
