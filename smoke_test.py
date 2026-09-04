"""Quick manual check: boot the demo project and print what list_models sees."""

import os
from pathlib import Path

# Anchored on this file, not on the caller's working directory. A bare
# "testprojects" works from the repository root and nowhere else, which is a
# trap: it passes locally and fails the moment anything runs it from elsewhere.
ROOT = Path(__file__).resolve().parent

os.environ.setdefault("DJANGO_CHAINSAW_PROJECT_PATH", str(ROOT / "testprojects"))
os.environ.setdefault("DJANGO_CHAINSAW_SETTINGS_MODULE", "demoshop.settings")

from django_chainsaw_mcp.django_env import ensure_django  # noqa: E402
from django_chainsaw_mcp.introspect import list_models  # noqa: E402

config = ensure_django()
print("BOOT OK ->", config.settings_module, "@", config.project_path)

out = list_models(app_label="shop")
print("Modelle:", out["model_count"])
print()

unknown = 0
for model in out["models"]:
    print("{:<18} table={:<18} felder={}".format(
        model["label"], model["db_table"], len(model["fields"])
    ))
    for field in model["fields"]:
        rel = field.get("relation")
        if not rel:
            continue
        if rel["kind"] == "Unknown":
            unknown += 1
        print("     {:<14} {:<11} {:<8} -> {:<18} {}".format(
            field["name"],
            rel["kind"],
            rel["direction"],
            rel["to"],
            rel.get("on_delete") or "",
        ))

print()
print("Relationen ohne erkannte Kardinalitaet:", unknown)
