# Contributing

## Setup

```bash
uv sync
```

Python 3.12 or newer. Django is a dev dependency: the server needs it importable
from the interpreter it runs in, but it is the *target project's* Django that
matters in real use, so it is not a runtime dependency of the package.

## Tests

```bash
uv run python smoke_test.py      # introspection functions, called directly
uv run python analysis_test.py   # the analysis tools, with assertions
uv run python client_test.py     # the server over the real MCP transport
bash exitcheck.sh                # CLI exit codes
bash portability_check.sh        # no host paths in tracked files
bash baseline_check.sh           # the baseline lifecycle end to end
bash since_check.sh              # --since against a real throwaway git repo
bash fix_check.sh                # the --write safety boundary
uv run pytest                    # the analysis layer
bash install_check.sh            # the documented install path, in a fresh venv
```

`client_test.py` is the one that matters most. The others call Python functions,
which proves nothing about the protocol: it starts the server as a separate
process, speaks stdio to it, and checks what comes back over the wire.

Run all four before committing. They are fast.

## Adding a tool

1. **Put the logic in its own module** under `src/django_chainsaw_mcp/`. Front
   ends stay thin: `server.py` and `cli.py` should contain argument handling and
   printing, nothing else. That separation is why the CLI took an afternoon.

2. **Call `ensure_django()` only if the tool needs the app registry.** Reading
   models, migrations or signals does; reading Python source does not, and
   making it boot anyway is how a tool ends up refusing to analyse a FastAPI
   project for reasons that have nothing to do with the question asked. Use
   `project.resolve_root()` for the second kind, and add the tool to
   `check._REQUIRES` so the aggregate knows what it needs.

3. **Raise `ValueError` for caller mistakes.** `_guard` in `server.py` turns
   those into `{"ok": false, "error": ...}` so a typo does not kill the server.
   Do not catch broad exceptions inside the module.

4. **Say what the tool cannot see.** Every return value carries a `note`. This
   is not politeness: the failure mode of static analysis is a confident wrong
   answer, and the note is what stops someone acting on one.

5. **Write the failing test first.** Every bug in the README was found by a test
   that asserted a *specific* number or shape, not by one that checked the call
   did not raise.

6. **Add its row to the README and `docs/tools.md`.** `docs_check.sh` fails
   otherwise, and it will tell you exactly what is missing.

## Documentation is checked the way the code is

`docs_check.sh` fails if a tool has no line in the README, a subcommand has no
section in the CLI reference, a check name cannot be looked up anywhere, or a
page under `docs/` is not linked from the index.

This exists because the repository shipped for several iterations describing
itself as a Django tool while a third of its checks no longer needed Django. A
README that describes a scope the code has outgrown is worse than one that says
nothing: somebody with a FastAPI project reads the first paragraph and leaves.
The check found four separate pieces of drift the first time it ran.

## Test style

Assertions are specific on purpose:

```python
check(len(refs) == 4, f"expected exactly the 4 real references, got {len(refs)}")
check(3 not in lines, "line 3 is inside the module docstring and must not count")
```

`assert result` would have passed for every bug listed in the README. Each of
them produced output, and the output was wrong.

When fixing a bug, add the assertion that catches it before the fix, watch it
fail, then fix it.

## The demo project

`testprojects/` is a throwaway Django project, deliberately shaped:

- self-referencing FK (`Category.parent`) so cycle handling is exercised
- `PROTECT` on `OrderLine.product` so a blocked delete has a case
- a `OneToOne`, a `ManyToMany` and several reverse accessors
- a template written the way a slow page is written, including a nested loop
- a migration pair that removes `Product.legacy_code`, plus a `services.py` that
  still uses it in four different shapes

Add to it rather than mocking. Every shape in there exists because a bug hid in
its absence.

## Commit messages

Explain the decision, not the diff. `git log --stat` already shows what changed.

If a bug is fixed, say what the wrong behaviour looked like and why it was not
obvious. Several entries in the README came straight out of commit messages.

## Style

- **No absolute paths anywhere in the repository.** Shell scripts start with
  `cd "$(dirname "$0")"`, Python anchors on `Path(__file__)`, and the server
  takes its target from environment variables. `portability_check.sh` enforces
  this; it is a tool other people install, and a path to somebody's home
  directory is the kind of defect that survives review because it still works
  for the author.
- **Do not assert on a project-wide count.** `unscoped_count == 4` breaks the
  moment somebody adds a fixture, and the reflex is to edit the number rather
  than read the failure. Assert on the file the check was written for.
- No em dashes.
- Comments explain why, not what.
- German is fine in test output; documentation and code are English.
