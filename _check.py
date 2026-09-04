import os
os.environ.setdefault("DJANGO_CHAINSAW_PROJECT_PATH", "testprojects")
os.environ.setdefault("DJANGO_CHAINSAW_SETTINGS_MODULE", "demoshop.settings")
from django_chainsaw_mcp.overfetch import unused_eager_loading

for low in (False, True):
    r = unused_eager_loading(include_low_confidence=low)
    print(f"--- include_low_confidence={low}: {r['finding_count']} finding(s), "
          f"views_checked={r['views_checked']} dynamic={r['views_with_runtime_paths']}")
    for f in r["findings"]:
        print(f"    {f['confidence']:<5} {f['view'].split('.')[-1]:<26} {f['kind']}(\"{f['path']}\")")
        if f["unreadable_because"]:
            print(f"          opaque: {f['unreadable_because']}")
