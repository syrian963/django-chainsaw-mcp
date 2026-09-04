# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning is [semantic](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Command line interface** (`django-chainsaw`) with exit codes, so the
  analysis can gate a pipeline instead of only answering questions. `0` clean,
  `1` findings, `2` load or argument error. Gates are opt-in: without
  `--max-high` or `--fail-on-risk` a command only reports.
- **`scan_templates`**: the N+1 analysis over a whole directory, resolving the
  template context from class-based views that declare `template_name` with
  `model` or `queryset`. `find_n_plus_one` needs a context map typed by hand,
  which is fine from an assistant and useless in CI.
- **`what_happens_on`**: follows the signal chain a save or delete sets off,
  through receiver bodies and into the signals those writes fire in turn.
  Receivers come from the live registry; their bodies are read with the AST.
  Write targets resolve by class name and, more usefully, through the sending
  model's relations, so `instance.order.save()` continues the chain instead of
  ending it. Tools that list receivers exist; none follow the second hop, which
  is where a Celery task three models away turns up. It is also the half
  `delete_impact` documents itself as missing.
- **`find_unscoped_queries`**: reports querysets that read tenant-scoped rows
  without an ownership filter, the shape behind most IDOR reports. Generic
  static analysers struggle with this class because the defect is the absence of
  a filter and absence has no syntax; the existing answers are runtime SQL
  inspection or PostgreSQL row-level security. The model graph makes it
  checkable: it knows `Order` reaches the tenant root through `customer`, so
  filtering on `pk` alone is visibly not enough. Models with no path to the
  owner are never reported.
- **`deploy_safety`**: cross-references pending destructive migrations against
  the code that still uses them, and reports `blocking` with file and line
  numbers or `clear`. Python is parsed with `ast`; templates use patterns.
- **`delete_impact`**: follows `on_delete` transitively across the model graph.
- **`find_n_plus_one`**: relation traversals in a template, with the
  `select_related` / `prefetch_related` that would fix them.
- **`migration_risk`**: migration operations rated by production impact.
- **`django://models`** as an MCP resource rather than another tool.
- Documentation under `docs/`: usage, architecture, tool reference, CLI, and the
  reasoning behind `deploy_safety`.
- **`docs/usage.md`**: how to run this against a real project. The decisive
  constraint is that `django.setup()` imports your settings and therefore every
  entry in `INSTALLED_APPS`, so the interpreter needs your project's
  dependencies. Covers installing into the project venv, keeping it separate,
  Docker with `docker compose exec -T`, client configuration for Claude Code and
  JSON-configured clients, and a table mapping each error message to its cause.
- **`install_check.sh`**: builds a throwaway virtualenv, installs Django and this
  package into it, then drives both the CLI and the MCP server with that
  interpreter. The documented install path is verified, not assumed.

### Fixed

Each of these ran without raising anything, which is the failure mode this kind
of tooling actually has.

- **Reverse relations lost their cardinality.** The check tested
  `many_to_many`, `many_to_one` and `one_to_one` but not `one_to_many`, so every
  reverse accessor came back as `Unknown`: exactly the half that matters,
  because reverse relations are where N+1 queries come from.
- **`delete_impact` reported nothing at all.** `on_delete` lives on
  `field.remote_field`, not on the field. `getattr(field, "on_delete", None)`
  returned `None` silently, every relation classified as `UNKNOWN`, and the tool
  cheerfully reported that deleting a customer had no consequences.
- **Nested loops were invisible.** Loop variables were bound to the string they
  iterated, so `{% for line in order.lines.all %}` left `line` unresolvable and
  everything inside went unreported. They now carry the resolved element model,
  which is also what makes `prefetch_related('lines')` appear.
- **`deploy_safety` matched docstrings and every `name` in the project.** The
  first version was a text search: it flagged `contenttypes.0002` removing a
  field called `name`, with hits across unrelated models, and counted a sentence
  in a module docstring as a live reference. Fixed by parsing Python with `ast`
  and by only analysing migrations from apps inside the project.
- **Narrowing the scan emptied the analysis instead of flipping the verdict.**
  `search_path` decided both where to look for references and which apps counted
  as project apps, so pointing it at a subdirectory excluded every app and
  returned nothing rather than reporting the migration as clear. App ownership
  now comes from the project path.
- **The test helper read one spelling of a field that has two.** `_payload` in
  `client_test.py` used `result.structuredContent`, which works on the SDK build
  pinned here and raises `AttributeError` on builds that call it
  `structured_content`. It only surfaced once the package was installed into a
  different environment, which is the entire reason `install_check.sh` exists.
- **The package imported `server` eagerly**, which made
  `python -m django_chainsaw_mcp.server` import the module twice and emit a
  `RuntimeWarning`. `main` is resolved lazily through `__getattr__`.

- **Duplicate findings from chained querysets.** `ast.walk` visits every `Call`
  in a chain, so `Order.objects.filter(...).first()` unwound twice and was
  reported twice. Findings are deduplicated per file, line and model, keeping
  the outermost chain.
- **CRLF line endings in the shell scripts.** Editing them from a Windows host
  through the WSL share wrote `\r\n`, and bash failed with a syntax error on a
  line that looked correct. A `.gitattributes` now pins `eol=lf`.

- **`find_unscoped_queries` flagged `create()`.** Adding signal fixtures
  surfaced it: `Invoice.objects.create(order=instance)` was reported as an
  authorisation problem. Inserting a row cannot leak anybody's data, and the bug
  class is unauthorised reads. Chains ending in a pure write are skipped;
  `get_or_create` and `update_or_create` still count, because they read first.

### Changed

- Ported from `FastMCP` to `MCPServer` for MCP Python SDK v2, where the class
  was renamed. Neither is related to FastAPI, despite the old name.
