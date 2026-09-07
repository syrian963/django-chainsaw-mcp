# Security

## What this tool does to the code you point it at

Read this first, because it is the shape of the risk.

**It imports the target project.** `django.setup()` imports your settings and
every app in `INSTALLED_APPS`, and the checks additionally import the modules
that declare serializers, views and URLs. Anything those modules do at import
time therefore runs: a module-level HTTP call happens, a connection opened in
`apps.py` is opened, a `print` prints.

That is not a bug to be fixed. The app registry is where the answers are, and
there is no way to read it without loading it. It does mean:

> **Do not point this at code you would not run.**

Analysing an unfamiliar repository is the same act as running it. If that is
what you want to do, do it in a container with no credentials and no network,
the way you would run any untrusted code.

## What it does not do

Each of these was checked against the source rather than assumed, and there is
a test or a documented measurement behind it:

- **It never runs your application.** No view is called, no task dispatched, no
  management command executed.
- **It reads the database once, and only to ask which migrations are applied.**
  `migrations` and `deploy-safety` hand Django's own `MigrationLoader` a
  connection, which reads the `django_migrations` table. Nothing else opens a
  connection, nothing writes, no migration is applied.
- **It writes files only when asked.** `fix --write` edits your source, and
  only the mechanical class of fix. A baseline, an API-contract snapshot,
  `--sarif` and `report --out` write where you tell them to.
- **It makes no network calls.** No telemetry, no update check, no uploads.
  The HTML report is deliberately self-contained for the same reason: a report
  that fetched a font from a CDN would break that promise on the behalf of
  whoever you sent it to.

## Findings are not secrets, but they are a map

A findings report names your models, your endpoints and the fields a
serializer exposes. That is a reasonable thing to attach to an internal merge
request and a poor thing to paste into a public issue. If you are reporting a
bug in this tool, a redacted excerpt is enough — see the issue template.

## Supported versions

| | |
| --- | --- |
| This tool | the latest release |
| Python | 3.12, 3.13, 3.14 |
| Django | 4.2 LTS through 6.1 |

Older Django may work and is not tested. CI installs a target project on
`django~=4.2.0` on every run, so 4.2 is a claim with a job behind it.

## Reporting a vulnerability

Open a **private** report through GitHub's security advisory form on this
repository rather than a public issue. If that is not available to you, mail
the address in `pyproject.toml`.

Please include the version, the Python and Django versions, and the smallest
input that shows the problem. You will get an acknowledgement; if the report is
valid you will get the fix and the credit, and if it is not you will get the
reasoning rather than silence.

The classes of report that matter most here:

1. **Anything that writes** where this document says nothing is written.
2. **Anything that reaches the network.**
3. **A path where the tool executes target code beyond an import** — for
   instance evaluating an expression out of a settings file or a template.
4. **Injection into an output artifact.** The HTML report embeds real source
   code from your project; a finding whose text could break out of the payload
   and execute in the reader's browser is a genuine vulnerability, not a
   cosmetic bug. There is a test for exactly that case, and it is not proof
   that no other route exists.
