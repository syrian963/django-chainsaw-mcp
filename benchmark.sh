#!/usr/bin/env bash
# Does this survive a codebase that is not a toy?
#
# Everything so far was measured against a demo project with eight models and
# eleven files. A tool that takes four minutes on a real repository is a tool
# nobody runs twice, and guessing which side of that line we are on is not
# measuring.
cd "$(dirname "$0")" || exit 2
export PATH="$HOME/.local/bin:$PATH"

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

APPS=${1:-20}
MODELS_PER_APP=${2:-10}
VIEWS_PER_APP=${3:-15}

echo "Generating $APPS app(s), $((APPS * MODELS_PER_APP)) model(s), $((APPS * VIEWS_PER_APP)) view module(s)"

python3 - "$WORK" "$APPS" "$MODELS_PER_APP" "$VIEWS_PER_APP" <<'PY'
import sys, pathlib

root = pathlib.Path(sys.argv[1]) / "bigproject"
apps, models_per_app, views_per_app = (int(x) for x in sys.argv[2:5])
root.mkdir(parents=True)

(root / "config").mkdir()
(root / "config" / "__init__.py").write_text("")

app_labels = [f"app{i:03d}" for i in range(apps)]
(root / "config" / "settings.py").write_text(
    "SECRET_KEY = 'benchmark-only'\n"
    "DEBUG = False\n"
    "USE_TZ = True\n"
    "INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth'] + "
    f"{app_labels!r}\n"
    "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}\n"
    "DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'\n"
)

for label in app_labels:
    app = root / label
    app.mkdir()
    (app / "__init__.py").write_text("")

    models = ["import datetime\n\nfrom django.db import models\n\n"]
    models.append(
        "class Owner(models.Model):\n"
        "    email = models.EmailField(unique=True)\n\n\n"
    )
    for n in range(models_per_app):
        models.append(
            f"class Thing{n}(models.Model):\n"
            f"    name = models.CharField(max_length=100)\n"
            f"    code = models.CharField(max_length=40, db_index=True)\n"
            f"    created = models.DateTimeField(default=datetime.datetime.now)\n"
            f"    owner = models.ForeignKey(Owner, on_delete=models.CASCADE,"
            f" related_name='things{n}')\n\n\n"
        )
    (app / "models.py").write_text("".join(models))

    for v in range(views_per_app):
        lines = [f"from .models import Thing{v % models_per_app}\n\n"]
        for q in range(6):
            lines.append(
                f"def view_{v}_{q}(request, pk):\n"
                f"    return Thing{v % models_per_app}.objects.filter(name=pk)\n\n"
            )
        (app / f"views_{v}.py").write_text("".join(lines))

print(f"wrote {sum(1 for _ in root.rglob('*.py'))} python files")
PY

export DJANGO_CHAINSAW_PROJECT_PATH="$WORK/bigproject"
export DJANGO_CHAINSAW_SETTINGS_MODULE=config.settings
BIN=$PWD/.venv/bin/django-chainsaw

files=$(find "$WORK/bigproject" -name '*.py' | wc -l)
echo "Files: $files"
echo

time_it() {
  local label="$1"; shift
  local start end
  start=$(date +%s%N)
  "$@" >/dev/null 2>&1
  end=$(date +%s%N)
  printf '  %-26s %6s ms\n' "$label" "$(( (end - start) / 1000000 ))"
}

time_it "models --short"      $BIN models --short
time_it "tenancy"             $BIN tenancy --tenant-root app000.Owner
time_it "indexes"             $BIN indexes
time_it "datetimes"           $BIN datetimes
time_it "deploy-safety"       $BIN deploy-safety
time_it "explain one model"   $BIN explain app000.Thing0 --tenant-root app000.Owner
time_it "check (everything)"  $BIN check --tenant-root app000.Owner
