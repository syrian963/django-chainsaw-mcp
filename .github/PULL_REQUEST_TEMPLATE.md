## What this changes

<!-- One or two sentences. The why matters more than the what; the diff
     already says what. -->

## Evidence

<!-- Not a promise that it works - the output that shows it does.

     - A new or changed check: the finding it now produces, and the correct
       code it stays silent on.
     - A fix: the test that fails without it. Revert the fix, watch it go red,
       restore it, watch it go green, and say so here.
     - A performance change: the number before and the number after, from
       `bash benchmark.sh`, with the spread it printed. One run of wall clock
       is not a measurement.
-->

## Checklist

- [ ] `bash run_tests.sh` is green (all 17 suites)
- [ ] `uv run ruff check src/ tests/` is clean
- [ ] A new check documents what it **cannot** see, in its own output and on
      its docs page
- [ ] A suppression carries the reason next to it
- [ ] The docs say what the code does — `docs_check.sh` verifies the tool
      names, the CLI commands and the numbers on the badges, so a stale README
      fails the build rather than shipping

## What you deliberately did not do

<!-- Sibling code paths left alone, a case knowingly out of scope, a number
     you could not measure. Saying so is worth more than leaving it to be
     discovered. -->
