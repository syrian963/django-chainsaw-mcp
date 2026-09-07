# Performance

## Measured, not assumed

Everything was built against a demo project with eight models and eleven files.
That says nothing about a real codebase, so `benchmark.sh` generates one and
times the commands against it.

**2822 Python files, 1500 models, 60 apps:**

| Command | Time |
| --- | --- |
| `models --short` | 1.8 s |
| `deploy-safety` | 2.0 s |
| `datetimes` | 3.1 s |
| `tenancy` | 4.0 s |
| `indexes` | 4.1 s |
| `explain` one model | 8.7 s |
| `check` (everything) | **35.6 s** |

```bash
bash benchmark.sh 60 25 45      # apps, models per app, view modules per app
```

**That last number was 5.4 seconds when there were ten checks.** There are
twenty-one now, and a full run costs what twenty-one analyses of 2822 files
cost. Half a minute is still fine in CI and borderline at a terminal, which is
what `--only` and `--skip` are for.

### The generated project is kinder than a real one

The numbers above come from `benchmark.sh`, which writes 2822 uniform files.
Run against a real codebase of 2120 files and 424 models, a full `check` took
**338 seconds** rather than 35.

The generated project has shallow call graphs, few relations per model and no
inheritance to speak of. Real code has all three, and the checks that walk a
call graph pay for it. So read the 35 seconds as the floor for a project of
that size and the 338 as what a mature one costs, and use `--only` or `--skip`
if that matters at a terminal. In CI it is a coffee, not a problem.

The per-check numbers on that real project, in the same process so Django boots
once: `dangling` 26 s, `async` 24 s, `aggregates` 22 s, `n+1-serializer` 21 s,
`choices` 20 s. One full read and parse of that tree is 3.2 s, so parsing is
about a quarter of the total and the rest is the analysis itself.

### Where the half minute goes

Each check on its own, on the same project. Every one of these includes about
2.2 s of Django boot, so the marginal cost of adding a check to a run is
roughly the number minus that:

| Check | Time | | Check | Time |
| --- | --- | --- | --- | --- |
| `races` | 9.4 s | | `open` | 4.5 s |
| `loops` | 7.6 s | | `dangling` | 4.3 s |
| `on-commit` | 6.7 s | | `deploy-safety` | 4.0 s |
| `tenancy` | 5.6 s | | `n+1-serializer` | 3.9 s |
| `celery` | 5.5 s | | `datetimes` | 3.8 s |
| `indexes` | 5.2 s | | `routes` | 3.7 s |
| `aggregates` | 4.5 s | | the rest | 2.2–3.5 s |

The expensive ones are the ones that walk the call graph. Their costs do not
add up to 35.6 s — the individual runs total about 43 s of marginal work — and
the difference is the call graph being built once per process instead of once
per check.

## The number that was not fine

`explain` on a **single** model took 5.1 seconds, which is almost the entire
cost of analysing the whole project.

The reason is structural: `explain_model` runs the ownership, exposure, index
and datetime checks so it can correlate them, and every one of those walks the
whole tree. Asking about ten models cost fifty seconds of repeating the same
four analyses.

Over MCP that is the normal case, not an edge case. An assistant asked about one
model will be asked about the next one, and the tool would feel broken on
exactly the workflow it was built for.

## The cache

Project-wide analyses are memoised for the life of the process:

```
shop.Order           360 ms      <- cold
shop.Invoice           3 ms
shop.OrderLine         4 ms
shop.Product           3 ms
shop.Customer          2 ms

cache: {'hits': 16, 'misses': 4, 'invalidations': 0}
```

**About a hundred times faster from the second model onwards.**

Model-specific work is not cached, because it is cheap and because caching it
would need a second invalidation key for no gain.

### Invalidation

By **file modification time**, not content hash. Hashing 2822 files to decide
whether to skip five seconds of work is its own kind of slow, and mtime is what
build tools rely on for the same reason.

A changed tree drops the **whole** cache rather than one entry. These analyses
are project-wide, so one edited file can change any of them, and tracking which
would cost more than recomputing.

The fingerprint is a `stat` per watched file and no reads: a few milliseconds
against seconds of analysis, cheap enough to run before every lookup.

### Verified, not asserted

`cache_check.sh` runs against a copy of the demo project:

```
ok  a second model is much faster (369ms then 2ms)
ok  the cached run answers about the model asked for
ok  cached ownership is correct
ok  editing a file invalidates the cache
ok  the finding from the new file is present after invalidation
ok  the recomputed answer is not the stale one
```

The last three matter more than the first. **A cache that does not invalidate is
a bug that reports yesterday's findings with today's confidence**, which is
worse than being slow, and it is the failure that hides for months because
everything looks fast and nothing looks wrong.

## Where the time goes

Almost all of it is `ast.parse` on every Python file. There is no way around
reading the source, and the AST is what makes the analysis precise rather than
a text search, which is a trade already made deliberately in `deploy_safety`.

### Two caches, and only one of them is for the server

The **model cache** above is for the MCP server: a long-lived process asked
about one model after another, where the repetition is across requests. The CLI
runs one command and exits, so that cache does nothing for it.

The **call graph** is different. Seven checks build one, and `check` runs all of
them, so a single CLI invocation used to parse the same unchanged tree seven
times. It is now built once per process and reused while a `stat`-only
fingerprint — file count, newest mtime, total size — matches. That is worth
about 7 seconds of the 35.6, inside one command, with no server involved.

An earlier version of this page said the cache does nothing for the CLI. That
was true when there was one cache.
