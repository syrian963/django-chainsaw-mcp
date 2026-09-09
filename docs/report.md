# `report` — one HTML file you can open and hand over

```bash
django-chainsaw report --out findings.html --title "myproject"
```

A thousand findings in a terminal is a scroll. The same thousand with a
severity filter, a search box and a grouping toggle is a working session, and
the difference is not cosmetic: the first question anybody asks of a report
like this is *which of these are on a page a customer can reach*, and reading
your way to that answer is hopeless.

## Four ways to look at the same findings

| Grouped by | Answers |
| --- | --- |
| **endpoint** | Which pages carry this, and through what call path |
| severity | What to fix first if nothing else is known |
| check | Is this one systemic problem or twenty unrelated ones |
| file | Which module has accumulated the most |

Endpoint grouping is why the page exists. `request_impact` already answers
which entry points reach which findings, and that answer is a tree — a shape a
terminal renders badly and a page renders naturally. On a real project it turns
879 findings into 224 endpoints, which is a list somebody can actually work
through.

Search and the severity toggles apply to whichever grouping is showing, so
"critical findings on an endpoint, excluding the management commands" is two
clicks rather than another full run.

## What it deliberately is not

**Not a server.** Opening a local file is one step, and a step nobody has to
keep running. There is no `--serve`, no port and no process to remember.

**Not networked.** The tool promises that nothing leaves the machine. A report
that pulls a font or a chart library from a CDN would break that promise on
somebody else's behalf, and would also stop working on an air-gapped box or
from a CI artifact. Every byte is in the file: the CSS, the script and the
data. A test asserts there is no `<link>`, no `<img>`, no `src="http` and no
`@import` in the output.

**Not a build step.** No bundler, no framework, no `node_modules`. The file
opens from a directory, an email attachment or a CI artifact with nothing
installed.

Roughly 100 KB for 192 findings, and it scales with the findings rather than
the project.

## The caveats travel with the findings

A report that looks cleaner than the analysis was is worse than no report, and
this is the file people forward. So the page also carries:

- **Whether the right project loaded at all.** A settings module can import
  cleanly and define no models, and then eighteen of the twenty-one checks
  report nothing - correctly, and uselessly. That warning is red and sits
  above every count on the page, because it invalidates all of them.
- **Whether the database answers.** Nothing here needs it, but the tool
  imports the modules that declare serializers, views and URLs, and one that
  touches the database while being imported waits out a connect timeout. On
  one real project that turned a 28-second check into a 567-second one.
- **Checks that could not run**, with their error, listed as *unverified*
  rather than omitted.
- **Checks that ran and found nothing, with what they looked at.** "0
  findings" reads as clean and can equally mean the check found nothing to
  examine. `celery: tasks found 12, dispatches checked 41` is a clean zero; a
  check that reports no coverage at all is named as such, because that zero
  cannot be read either way.
- **Where the CPU went**, when a run passes ten seconds, with the share per
  check and the names `--skip` takes. If the machine was busy - wall clock far
  above CPU - the page says the shares cannot be compared.
- **Checks that do not apply**, grouped by reason.
- **What the endpoint grouping cannot say**: how many findings no entry point
  reaches, how many of those are in a method something calls by name on an
  object that cannot be identified — unresolved rather than unreached — and how
  many have no file and line to attribute at all.

## In CI

```yaml
- run: django-chainsaw report --out findings.html --fail-on-findings
- uses: actions/upload-artifact@v4
  with:
    name: findings
    path: findings.html
```

The file is always written, and `--fail-on-findings` decides the exit code
separately. A red pipeline with the report attached beats a red pipeline with a
log to scroll.

## Options

| Flag | Meaning |
| --- | --- |
| `--out FILE` | where to write it (default `django-chainsaw-report.html`) |
| `--title NAME` | what to call the project in the heading |
| `--only` / `--skip` | narrow which checks run, repeatable |
| `--no-impact` | skip the endpoint grouping, which costs a second pass |
| `--fail-on` | severity the gate trips at |
| `--fail-on-findings` | exit 1 as well as writing the file |

## Limits

- **The endpoint grouping is only as good as `request_impact`**, and that page
  documents its own blind spots. With `--no-impact` the endpoint tab says so
  instead of showing an empty tree.
- **No history.** It reports the tree as it is now; comparing two runs is what
  `--since` and baselines are for.
- **No sorting by anything but severity** inside a group. The grouping is the
  axis; a second one would be a table, and a table is where this kind of page
  stops being readable.
