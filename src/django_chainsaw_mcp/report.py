# SPDX-FileCopyrightText: 2026 Mohammad Alsakka <mnouralsakka@gmail.com>
# SPDX-License-Identifier: MIT

"""One HTML file you can open, filter and hand to somebody.

A thousand findings in a terminal is a scroll. The same thousand with a
severity filter, a search box and a grouping toggle is a working session, and
the difference is not cosmetic: the first question anybody asks of a report
like this is "which of these are on a page a customer can reach", and answering
it by reading is hopeless.

So `django-chainsaw report` writes a single file. Not a served page:

- **No server.** Opening a local file is one step, and a step nobody has to
  keep running.
- **No network.** The tool promises that nothing leaves the machine; a report
  that pulls a font or a chart library from a CDN breaks that promise on
  someone else's behalf. Every byte is in the file.
- **No build.** The CSS and the script are inline, so the file works from a
  CI artifact, an email attachment, or a directory on a laptop with no
  toolchain at all.

## What it shows that the terminal cannot

Grouping by entry point is the reason this exists. `request_impact` already
answers which endpoints reach which findings, and that answer is a tree - a
shape a terminal renders badly and a page renders naturally. The same data
also groups by check and by file, and switching between the three is a click
rather than another full run.

## What it deliberately keeps

The caveats travel with the findings. A check that could not run is listed as
unverified rather than omitted, the count of findings no entry point reaches is
shown next to the ones that are reached, and every finding keeps the sentence
saying what the tool cannot see about it. A report that looks cleaner than the
analysis was is worse than no report.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SEVERITIES = ("critical", "high", "medium", "low")


def _payload(
    findings_report: dict[str, Any],
    impact_report: dict[str, Any] | None,
) -> dict[str, Any]:
    """Everything the page needs, in one JSON-serialisable mapping."""
    findings = []
    for index, finding in enumerate(findings_report.get("findings", [])):
        findings.append({
            "id": index,
            "check": finding.get("check") or "",
            "severity": finding.get("severity") or "medium",
            "title": finding.get("title") or "",
            "location": finding.get("location") or "",
            "detail": finding.get("detail") or "",
            "fix": finding.get("fix") or "",
        })

    entries = []
    for entry in (impact_report or {}).get("entry_points", []):
        entries.append({
            "label": entry.get("label") or entry.get("entry") or "",
            "kind": entry.get("kind") or "",
            "url": entry.get("url"),
            "worst": entry.get("worst") or "medium",
            "count": entry.get("finding_count") or 0,
            "findings": [
                {
                    "check": f.get("check") or "",
                    "severity": f.get("severity") or "medium",
                    "title": f.get("title") or "",
                    "location": f.get("location") or "",
                    "through": [
                        hop.rsplit(".", 1)[-1] for hop in (f.get("through") or [])
                    ],
                }
                for f in entry.get("findings", [])
            ],
        })

    failed = findings_report.get("checks_failed") or []
    ran = findings_report.get("checks_run") or {}

    return {
        "generated": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "findings": findings,
        "by_severity": findings_report.get("by_severity") or {},
        "checks_ran": sorted(name for name, state in ran.items() if state.get("ok")),
        "checks_failed": [
            {"name": name, "error": str(ran.get(name, {}).get("error", ""))}
            for name in failed
        ],
        "checks_not_applicable": findings_report.get("checks_not_applicable") or {},
        "registry_warning": findings_report.get("registry_warning"),
        "database_warning": findings_report.get("database_warning"),
        # A check that ran and found nothing, with what it looked at. The
        # difference between a clean zero and a blind one belongs in the
        # artifact somebody forwards, not only in the terminal it was run in.
        "checks_clean": [
            {
                "name": name,
                "examined": state.get("examined") or {},
                "cpu_seconds": state.get("cpu_seconds"),
            }
            for name, state in sorted(ran.items())
            if state.get("ok") and not state.get("findings")
        ],
        "timings": sorted(
            (
                {
                    "name": name,
                    "cpu_seconds": state.get("cpu_seconds") or 0.0,
                    "seconds": state.get("seconds") or 0.0,
                }
                for name, state in ran.items()
                if state.get("cpu_seconds") is not None
            ),
            key=lambda row: -row["cpu_seconds"],
        ),
        "frameworks": findings_report.get("frameworks") or {},
        "entries": entries,
        "impact": None if impact_report is None else {
            "entry_points": impact_report.get("entry_points_found", 0),
            "carrying": impact_report.get("entry_points_with_findings", 0),
            "unattributed": impact_report.get("unattributed_count", 0),
            "receiver_unknown": impact_report.get(
                "unattributed_because_the_receiver_is_unknown", 0
            ),
            "without_location": impact_report.get("without_a_location_count", 0),
            "note": impact_report.get("note", ""),
        },
    }


_STYLE = """
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --line: #d1d9e0;
  --panel: #f6f8fa; --accent: #0969da;
  --critical: #cf222e; --high: #bc4c00; --medium: #9a6700; --low: #656d76;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #3d444d;
    --panel: #151b23; --accent: #4493f8;
    --critical: #ff7b72; --high: #ffa657; --medium: #e3b341; --low: #9198a1;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}
code, .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
header { border-bottom: 1px solid var(--line); padding: 20px 24px; }
h1 { font-size: 19px; margin: 0 0 4px; }
.sub { color: var(--muted); font-size: 13px; }
main { padding: 20px 24px 64px; max-width: 1180px; }
.tiles { display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 18px; }
.tile {
  border: 1px solid var(--line); border-radius: 6px; padding: 10px 14px;
  background: var(--panel); min-width: 104px;
}
.tile b { display: block; font-size: 21px; line-height: 1.2; }
.tile span { color: var(--muted); font-size: 12px; }
.controls {
  display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
  margin: 0 0 16px; padding: 12px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--panel);
}
input[type=search], select {
  font: inherit; padding: 6px 9px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--bg); color: var(--fg);
}
input[type=search] { flex: 1 1 240px; min-width: 180px; }
button {
  font: inherit; padding: 6px 11px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--bg); color: var(--fg); cursor: pointer;
}
button[aria-pressed=true] { background: var(--accent); border-color: var(--accent); color: #fff; }
.group { border: 1px solid var(--line); border-radius: 6px; margin-bottom: 10px; overflow: hidden; }
.group > summary {
  cursor: pointer; padding: 10px 14px; background: var(--panel);
  display: flex; gap: 10px; align-items: baseline; font-weight: 600;
}
.group > summary::-webkit-details-marker { display: none; }
.group > summary .count { color: var(--muted); font-weight: 400; font-size: 12px; }
.group > summary .url { color: var(--muted); font-weight: 400; }
.item { border-top: 1px solid var(--line); padding: 11px 14px; }
.item h3 { font-size: 14px; margin: 0 0 4px; font-weight: 600; }
.where { font-size: 12px; color: var(--muted); }
.detail { margin: 7px 0 0; color: var(--fg); }
.fix { margin: 6px 0 0; }
.fix b { color: var(--muted); font-weight: 600; }
.path { font-size: 12px; color: var(--muted); margin-top: 5px; }
.sev {
  display: inline-block; min-width: 62px; text-align: center;
  font-size: 11px; font-weight: 700; letter-spacing: .04em;
  text-transform: uppercase; padding: 2px 7px; border-radius: 999px;
  border: 1px solid currentColor;
}
.sev.critical { color: var(--critical); }
.sev.high { color: var(--high); }
.sev.medium { color: var(--medium); }
.sev.low { color: var(--low); }
.caveat {
  border: 1px solid var(--line); border-left: 3px solid var(--medium);
  border-radius: 6px; padding: 12px 14px; margin: 22px 0 0; background: var(--panel);
}
.caveat h2 { font-size: 14px; margin: 0 0 6px; }
.caveat p { margin: 0 0 8px; color: var(--muted); }
/* A project that did not really load, or a database that will make the
   imports wait, invalidates every count below it. Loud on purpose. */
.caveat.alarm { border-left-color: var(--critical); }
.caveat.alarm h2 { color: var(--critical); }
.empty { color: var(--muted); padding: 24px 0; }
"""

_SCRIPT = """
const DATA = JSON.parse(document.getElementById("payload").textContent);
const $ = (id) => document.getElementById(id);
const esc = (s) => { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; };
const ORDER = { critical: 0, high: 1, medium: 2, low: 3 };
let state = { group: "severity", severities: new Set(["critical", "high", "medium", "low"]), q: "", check: "" };

function matches(f) {
  if (!state.severities.has(f.severity)) return false;
  if (state.check && f.check !== state.check) return false;
  if (state.q) {
    const hay = (f.title + " " + f.location + " " + f.check + " " + (f.detail || "")).toLowerCase();
    if (!hay.includes(state.q)) return false;
  }
  return true;
}

function itemHtml(f, path) {
  return `<div class="item">
    <h3><span class="sev ${f.severity}">${esc(f.severity)}</span> ${esc(f.title)}</h3>
    <div class="where mono">${esc(f.location)} &middot; ${esc(f.check)}</div>
    ${f.detail ? `<p class="detail">${esc(f.detail)}</p>` : ""}
    ${f.fix ? `<p class="fix"><b>fix:</b> ${esc(f.fix)}</p>` : ""}
    ${path && path.length > 1 ? `<div class="path mono">via ${esc(path.join(" \\u2192 "))}</div>` : ""}
  </div>`;
}

function groupHtml(title, extra, items, open) {
  return `<details class="group"${open ? " open" : ""}>
    <summary><span>${title}</span><span class="count">${items.length} finding(s)</span>${extra || ""}</summary>
    ${items.join("")}
  </details>`;
}

function render() {
  const shown = DATA.findings.filter(matches);
  $("shown").textContent = shown.length;
  const host = $("results");

  if (state.group === "endpoint") {
    if (!DATA.entries.length) {
      host.innerHTML = `<p class="empty">This report was written with --no-impact, so there is no
        endpoint grouping in it. Run <code>django-chainsaw report</code> without that flag.</p>`;
      return;
    }
    const allowed = new Set(shown.map((f) => f.check + "|" + f.location + "|" + f.title));
    const blocks = [];
    for (const e of DATA.entries) {
      const kept = e.findings.filter((f) => allowed.has(f.check + "|" + f.location + "|" + f.title));
      if (!kept.length) continue;
      kept.sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);
      const items = kept.map((f) => itemHtml(f, f.through));
      const url = e.url ? `<span class="url mono">${esc(e.url)}</span>` : "";
      blocks.push(groupHtml(`<span class="sev ${e.worst}">${esc(e.worst)}</span> ${esc(e.label)}`, url + `<span class="count">${esc(e.kind)}</span>`, items, blocks.length < 3));
    }
    host.innerHTML = blocks.length ? blocks.join("") : `<p class="empty">Nothing matches those filters.</p>`;
    return;
  }

  const buckets = new Map();
  for (const f of shown) {
    const key = state.group === "check" ? f.check
      : state.group === "file" ? (f.location.split(":")[0] || "(no file)")
      : f.severity;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(f);
  }
  const keys = [...buckets.keys()].sort((a, b) => {
    if (state.group === "severity") return (ORDER[a] ?? 9) - (ORDER[b] ?? 9);
    return buckets.get(b).length - buckets.get(a).length || a.localeCompare(b);
  });
  host.innerHTML = keys.length
    ? keys.map((k, i) => {
        const items = buckets.get(k).sort((a, b) => ORDER[a.severity] - ORDER[b.severity]);
        const label = state.group === "severity"
          ? `<span class="sev ${k}">${esc(k)}</span>` : esc(k);
        return groupHtml(label, "", items.map((f) => itemHtml(f, null)), i < 2);
      }).join("")
    : `<p class="empty">Nothing matches those filters.</p>`;
}

for (const b of document.querySelectorAll("[data-group]")) {
  b.addEventListener("click", () => {
    state.group = b.dataset.group;
    document.querySelectorAll("[data-group]").forEach((o) =>
      o.setAttribute("aria-pressed", String(o === b)));
    render();
  });
}
for (const b of document.querySelectorAll("[data-sev]")) {
  b.addEventListener("click", () => {
    const s = b.dataset.sev;
    if (state.severities.has(s)) state.severities.delete(s); else state.severities.add(s);
    b.setAttribute("aria-pressed", String(state.severities.has(s)));
    render();
  });
}
$("q").addEventListener("input", (e) => { state.q = e.target.value.trim().toLowerCase(); render(); });
$("check").addEventListener("change", (e) => { state.check = e.target.value; render(); });
render();
"""


def html_report(
    findings_report: dict[str, Any],
    impact_report: dict[str, Any] | None = None,
    project: str = "",
) -> str:
    """The whole report as one self-contained HTML document."""
    data = _payload(findings_report, impact_report)

    # `</script>` inside the payload would end the tag early, and a finding
    # quotes real source code. Escaping the angle brackets is enough and keeps
    # the JSON readable if somebody opens the file in an editor.
    encoded = (
        json.dumps(data, ensure_ascii=False)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )

    counts = data["by_severity"]
    total = len(data["findings"])
    checks = sorted({f["check"] for f in data["findings"]})
    options = "".join(
        f'<option value="{html.escape(name)}">{html.escape(name)}</option>'
        for name in checks
    )

    tiles = [
        f'<div class="tile"><b>{total}</b><span>finding(s)</span></div>',
    ]
    for severity in _SEVERITIES:
        if counts.get(severity):
            tiles.append(
                f'<div class="tile"><b style="color:var(--{severity})">'
                f'{counts[severity]}</b><span>{severity}</span></div>'
            )
    tiles.append(
        f'<div class="tile"><b>{len(data["checks_ran"])}</b><span>checks ran</span></div>'
    )
    if data["impact"]:
        tiles.append(
            f'<div class="tile"><b>{data["impact"]["carrying"]}</b>'
            f'<span>endpoints affected</span></div>'
        )

    caveats = []
    # Before any count on this page: was the right project loaded, and could
    # the imports it needed run without waiting on a database.
    for warning in (data.get("registry_warning"), data.get("database_warning")):
        if warning:
            caveats.append(
                "<div class='caveat alarm'><h2>Read this before the numbers</h2>"
                f"<p>{html.escape(str(warning))}</p></div>"
            )
    if data["checks_failed"]:
        rows = "".join(
            f"<li class='mono'>{html.escape(item['name'])}: "
            f"{html.escape(item['error'])}</li>"
            for item in data["checks_failed"]
        )
        caveats.append(
            "<div class='caveat'><h2>These checks could not run, so their area "
            f"is unverified</h2><ul>{rows}</ul></div>"
        )
    if data["checks_not_applicable"]:
        grouped: dict[str, list[str]] = {}
        for name, reason in data["checks_not_applicable"].items():
            grouped.setdefault(reason, []).append(name)
        rows = "".join(
            f"<li><span class='mono'>{html.escape(', '.join(sorted(names)))}</span>"
            f"<p>{html.escape(reason)}</p></li>"
            for reason, names in grouped.items()
        )
        caveats.append(
            f"<div class='caveat'><h2>{len(data['checks_not_applicable'])} check(s) "
            f"do not apply to this project</h2><ul>{rows}</ul></div>"
        )
    if data.get("checks_clean"):
        rows = "".join(
            "<li><span class='mono'>{name}</span><p>{detail}</p></li>".format(
                name=html.escape(entry["name"]),
                detail=html.escape(
                    ", ".join(
                        f"{key.replace('_', ' ')} {value}"
                        for key, value in entry["examined"].items()
                    )
                    or "does not report its coverage, so this zero cannot be read"
                ),
            )
            for entry in data["checks_clean"]
        )
        caveats.append(
            f"<div class='caveat'><h2>{len(data['checks_clean'])} check(s) ran "
            "and found nothing. What each one looked at</h2>"
            f"<ul>{rows}</ul></div>"
        )

    if data.get("timings"):
        total = sum(row["cpu_seconds"] for row in data["timings"])
        wall = sum(row["seconds"] for row in data["timings"])
        if total >= 10:
            rows = "".join(
                f"<li><span class='mono'>{html.escape(row['name'])}</span>"
                f"<p>{row['cpu_seconds']:.1f}s CPU"
                f" &middot; {row['cpu_seconds'] / total * 100:.0f}%</p></li>"
                for row in data["timings"][:5]
            )
            busy = (
                "<p>The machine was busy: most of the wall time was waiting, "
                "not working, so compare the CPU figures and not the total.</p>"
                if total and wall > total * 1.5 else ""
            )
            caveats.append(
                f"<div class='caveat'><h2>Where the {total:.0f}s of CPU went</h2>"
                f"<ul>{rows}</ul>{busy}"
                "<p>These names are what <span class='mono'>--skip</span> "
                "takes.</p></div>"
            )

    if data["impact"]:
        impact = data["impact"]
        caveats.append(
            "<div class='caveat'><h2>What the endpoint grouping cannot say</h2>"
            f"<p>{impact['unattributed']} finding(s) are reached by no entry "
            f"point this can see, and {impact['receiver_unknown']} of those sit "
            "in a method something calls by name on an object that cannot be "
            "identified without type inference. Those are unresolved, not "
            "unreached. A further "
            f"{impact['without_location']} have no file and line to attribute "
            "at all.</p>"
            f"<p>{html.escape(impact['note'])}</p></div>"
        )

    heading = html.escape(project) if project else "this project"

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>django-chainsaw report</title>
<style>{_STYLE}</style>
</head><body>
<header>
  <h1>django-chainsaw &mdash; {heading}</h1>
  <div class="sub">{data["generated"]} &middot; read-only analysis &middot;
    <span id="shown">{total}</span> of {total} finding(s) shown</div>
</header>
<main>
  <div class="tiles">{"".join(tiles)}</div>

  <div class="controls">
    <input type="search" id="q" placeholder="Search title, file or check">
    <select id="check"><option value="">every check</option>{options}</select>
    <span>group by</span>
    <button data-group="severity" aria-pressed="true">severity</button>
    <button data-group="check" aria-pressed="false">check</button>
    <button data-group="file" aria-pressed="false">file</button>
    <button data-group="endpoint" aria-pressed="false">endpoint</button>
    <span>severity</span>
    <button data-sev="critical" aria-pressed="true">critical</button>
    <button data-sev="high" aria-pressed="true">high</button>
    <button data-sev="medium" aria-pressed="true">medium</button>
    <button data-sev="low" aria-pressed="true">low</button>
  </div>

  <div id="results"></div>
  {"".join(caveats)}
</main>
<script type="application/json" id="payload">{encoded}</script>
<script>{_SCRIPT}</script>
</body></html>
"""


def write_report(
    path: Path,
    findings_report: dict[str, Any],
    impact_report: dict[str, Any] | None = None,
    project: str = "",
) -> int:
    """Write the report and return its size in bytes."""
    document = html_report(findings_report, impact_report, project)
    # Written through a temporary file: an interrupted write over an existing
    # report would otherwise leave half a document behind, and this project
    # has truncated a file that way before.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(document, encoding="utf-8")
    tmp.replace(path)
    return len(document.encode("utf-8"))
