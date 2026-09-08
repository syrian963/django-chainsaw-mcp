# Performance

## Measured, not assumed

Everything was built against a demo project with eight models and eleven files.
That says nothing about a real codebase, so `benchmark.sh` generates one and
times the commands against it.

**2822 Python files, 1500 models, 60 apps:**

`benchmark.sh` runs each command three times and prints the best next to the
worst, because one wall-clock run is not a measurement:

| Command | Best of 3 | Worst of 3 |
| --- | --- | --- |
| `models --short` | 2.8 s | 3.2 s |
| `deploy-safety` | 2.5 s | 2.9 s |
| `datetimes` | 2.8 s | 3.0 s |
| `indexes` | 4.5 s | 5.7 s |
| `tenancy` | 7.0 s | 9.8 s |
| `explain` one model | 8.8 s | 11.9 s |
| `check` (everything) | **33.3 s** | 44.8 s |

**Read the spread before the number.** A `check` that ranges from 33 s to 45 s
across three runs of the same code was measured on a machine that was not
quiet, and 33 is an upper bound rather than the cost. An earlier reading on an
idle machine gave 26.6 s. Run it yourself; `RUNS=5` if you want a firmer
floor.

```bash
bash benchmark.sh 60 25 45      # apps, models per app, view modules per app
```

**That last number was 5.4 seconds when there were ten checks, and 35.6 when
there were twenty-one.** It is 26.6 now because the twenty-one no longer parse
the same files twenty-one times over - see the cache section below.

### Why the spread matters more than the number

Two things happened while working on this that are worth passing on.

The first: a cache that retained every parsed tree made `check` **twice as
slow** and every single-pass command two to three times slower, and the first
measurements did not show it, because they came from a busy machine where the
noise was larger than the regression.

The second: the same code read 172 s and 326 s on that machine across runs, and
CPU time there varied by 40% too - a shared box does not just deschedule your
process, it competes for the cache your instructions need.

So `benchmark.sh` takes the best of three and prints the worst beside it. If
those two numbers are within a third of each other the floor is meaningful. If
they are not, the machine is telling you to stop drawing conclusions.

### The generated project is kinder than a real one

The numbers above come from `benchmark.sh`, which writes 2822 uniform files
with shallow call graphs, few relations per model and no inheritance worth the
name. Real code has all three, and the checks that walk a call graph pay for
it.

Measured against a real codebase of 2120 files and 424 models, a full `check`
cost about **270 seconds of CPU time**, and individual checks 10 to 17 seconds
each once the tree was parsed.

That number carries a caveat worth stating rather than hiding: the only real
project available for this was a shared machine with other work on it, and
wall-clock times there ranged from 172 to 326 seconds across runs of the same
code. Wall clock on a contended box measures the box. The benchmark above is
the repeatable figure and the one to compare releases against; treat the 270
seconds as an order of magnitude, not a measurement.

### A public project anyone can repeat

The figure above comes from a codebase that cannot be shared, which makes it
an assertion rather than evidence. So the same run was done against
[Wagtail](https://github.com/wagtail/wagtail), which anyone can clone:

```bash
git clone --depth 1 https://github.com/wagtail/wagtail.git
cd wagtail && uv venv && uv pip install -e ".[testing]" django-chainsaw-mcp
DJANGO_CHAINSAW_PROJECT_PATH=. DJANGO_CHAINSAW_SETTINGS_MODULE=wagtail.test.settings \
  django-chainsaw check
```

1386 Python files, 264 models, 21 checks: **142 s of user CPU** (171 s wall),
0 checks failed, 427 findings. Two checks reported that they do not apply -
no FastAPI, no SQLAlchemy - which is the intended answer rather than a gap.

The run is also the honest test of the false-positive rate, because nothing in
Wagtail was written with this tool in mind. It found one defect in the tool
immediately: 11 of the 41 apps it called the project's own were Django
contrib, DRF, taggit and django-filters, installed into a `.venv` **inside**
the project directory. Everything under the root is not everything that is
yours, and the fix is in the changelog.

### And a second one, because one stranger is not a sample

Wagtail found one defect. Saleor - 4332 files, 122 models, GraphQL instead of
DRF, a custom user model - found three more on its first run, none of which
Wagtail could have shown:

```bash
git clone --depth 1 https://github.com/saleor/saleor.git
cd saleor && uv venv --python 3.12 && uv sync --frozen
uv pip install django-chainsaw-mcp
SECRET_KEY=x DATABASE_URL=sqlite:///db.sqlite3 \
DJANGO_CHAINSAW_PROJECT_PATH=. DJANGO_CHAINSAW_SETTINGS_MODULE=saleor.settings \
  django-chainsaw check
```

**968 s of user CPU** (1069 s wall), 21 checks, none failed after the fixes,
two correctly not applicable.

That number carries the caveat. Wagtail's 1386 files cost 142 s and Saleor's
4332 cost 968: 3.1x the files for 6.2x the time. The cost is **not linear in
file count**, and this page should not be read as though it were. Two
plausible causes - deeper call graphs and more relations per model, both of
which the checks that walk them pay for - and neither has been measured, so
neither is stated as the reason. What is measured is the ratio.

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

### The parse cache, and why it is off by default

Twenty checks walk the project's Python files, and every one of them used to
read and parse the tree itself. One full read and parse of 2144 files is 5.4
seconds, so about a fifth of a `check` run was the same files being parsed
twenty times over. One full `ast.walk` over all of them, by contrast, is 1.5
seconds - the parsing was the expensive half, not the analysis.

So `read_source` and `parse_file` in `project.py` cache by path, mtime and
size, and `check` turns that on for the duration of its run: 35.6 s to 26.6 s
on the benchmark.

**It is off by default, and the benchmark is why.** The first version cached
unconditionally. A single subcommand reads each file exactly once, so
retaining every tree bought it nothing and cost 315 MB, and the measurement
was unambiguous: `check` went from 35.6 s to **78 s** and every single-pass
command got two to three times slower. Allocation and garbage collection are
not free, and a cache that is never read is pure cost.

That regression only showed up because the benchmark is repeatable. The
first numbers came from the contended machine above, where it would have been
invisible.

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
