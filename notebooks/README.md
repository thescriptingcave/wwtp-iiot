# The analyst notebooks

Eleven notebooks for people who already know pandas and SQL, about a wastewater
treatment plant that is instrumented, faulted and seeded with a week of data.

The two courses next door — [OPC UA](../courses/) and [SQL](../sql/) — teach the
protocols. This series does not teach pandas, DuckDB, matplotlib, seaborn or
plotly. It assumes you can write them and is about **what the plant is telling
you, and what it is not**.

SQL is used freely and explained inline. Everything else is used as a tool.

## Running them

```bash
make notebooks                        # build, execute, and verify every claim
```

That is the whole thing, and it is one command because the first version of this
section was five and every one of them was a thing you could get wrong in the
order. `make notebooks` brings up the database, seeds **its own** database,
executes all eleven, and fails if a number in the prose no longer matches what
that database says. **About 90 seconds once `wwtp_notebooks` exists**; the first
run adds the seed, which is most of the time either way.

**They read a separate database, `wwtp_notebooks`, and not `wwtp`.** This is the
one design decision in the series that needs justifying, so here is the whole of
it. The notebooks' numbers are checked claims, and a claim checked against data
that moves is not a claim. `wwtp` is seeded relative to *now*, so re-seeding it
moves every timestamp and every number derived from one — and the SQL course
beside this one uses `now()` in 28 places for the same reason. So the notebooks
get a database seeded to a **pinned** window:

```
4,239,284 readings across 57 signals and 22 pieces of equipment
2026-09-22 00:00 UTC -> 2026-09-28 23:59 UTC
```

An integer fingerprint of that table lives in `notebooks/_data.py`, and the gate
refuses to run if the database does not match it. Two independent seeds produce
the identical fingerprint over all 4,239,284 rows, which is the evidence that the
pin holds — see `docs/LEARNING-LOG.md`, where the first version of it did not.

The cost is honest and worth stating: the fault events inside that window are
*simulated*, deliberately, and the storm is a real one in the data rather than
weather. Nine and ten date change points against ground truth the seeder injects,
and the fact that a detector is scored against an answer rather than against an
opinion is worth the two and a half minutes the seed costs.

```bash
make notebooks-data      # just (re)create and seed wwtp_notebooks
```

## Reading them in order

| # | notebook | the question |
|---|---|---|
| 1 | [Meet the plant](01-meet-the-plant.ipynb) | what is here, and why a row is not a sample |
| 2 | [Three kinds of nothing](02-three-kinds-of-nothing.ipynb) | no data, bad data, no change — and tags that are copies of each other |
| 3 | [Choosing your tier](03-choosing-a-tier.ipynb) | raw, 1-minute or 1-hour, and what each one costs |
| 4 | [Resampling buys you nothing](04-resampling-buys-you-nothing.ipynb) | the rollups are a different estimator, not a faster one |
| 5 | [Autocorrelation](05-autocorrelation.ipynb) | how many independent hours — and how many independent signals |
| 6 | [Detrending and differencing](06-detrending-and-differencing.ipynb) | stationarity before modelling |
| 7 | [What are you estimating](07-what-are-you-estimating.ipynb) | time-weighted, flow-weighted, and which one you meant |
| 8 | [Rolling-origin validation](08-rolling-origin-validation.ipynb) | why a random split lies |
| 9 | [Anomaly detection](09-anomaly-detection.ipynb) | per-signal expectations, not global thresholds |
| 10 | [Change points](10-change-points.ipynb) | when did the plant change, not just what |
| 11 | [Dose or flow](11-dose-or-flow.ipynb) | attributing a change to an intervention |

02 is second in the list and first in the *argument*: it establishes the
vocabulary the other nine use, and 01 gives you the plant it applies to. 04 is
where the series turns — it shows that `AVG(value)` and an hourly rollup do not
disagree, they **answer different questions**, and that the two can be 27 % apart
on one signal on one day.

09 through 11 need the injected faults, and 11 needs 09's ground truth. If you
read one, read 09 before 11.

## Instantiating them

The committed `.ipynb` files carry **no outputs** — that is deliberate. They are
generated from `notebooks/src/*.md`, and the claims live in the markdown, so an
`outputs` array would be a record of one run rather than something checked. You
have to execute a notebook before there is anything to look at.

```bash
make notebooks-build   # regenerate notebooks/*.ipynb from notebooks/src/*.md
make notebooks-read    # execute all eleven to notebooks/read/*.html, to read
make notebooks-open    # JupyterLab, to work in them
```

`make notebooks-read` is the one to use for a manual check. It writes self-contained
HTML — text output and figures inline — into `notebooks/read/`, and you can open it
with `open notebooks/read/`:

```bash
open notebooks/read/
```

It also takes a subset, which is the difference between a minute and a minute
forty:

```bash
make notebooks-read NB_ONLY="05 09"        # just those two
python -m tools.notebook_read --no-execute 03   # re-render, do not run
```

`--no-execute` re-renders the **committed** outputs without touching the
database. It is much faster and it is worth knowing which one you are looking at:
an executed render is evidence the notebook runs, a re-render is only a
formatting change.

Read it, do not cite it. If a number in that HTML disagrees with the source
markdown, the source markdown is right, because the markdown is what
`make notebooks` checks.

**All of these are `make` targets on purpose**, and the reason is worth one
sentence because it is the same bug this repository has now hit twice. Running
`jupyter` by hand skips the `.env` the Makefile loads, and `dsn()` falls back to
port 5432:

```
connection to server at "127.0.0.1", port 5432 failed:
FATAL:  password authentication failed for user "wwtp"
```

which names a *password* when the real fault is the *port*. This database is on
**55433**. Use `make`, or `set -a && . ./.env && set +a` if you must go around it.

For the same reason `make notebooks-open` stops the gateway before it starts
Jupyter: the gateway writes, and a notebook that reads a moving target cannot
have a checked number in it.

### Opening one in JupyterLab

```bash
uv sync --extra protocols --extra storage --extra analysis --extra serve
make notebooks-open
```

It prints a URL with a token in it — use that one. JupyterLab serves on `8899`
and mints the token itself.

**Your browser opens by itself, and if it cannot, this says so.** JupyterLab masks
the token in the URL *it* prints (`token=...`), so Jupyter is told not to open
anything and this Makefile opens the real URL instead, once the port is
answering. On a machine with no desktop — a server, a container, an SSH session
with no forwarded browser — it prints the address and the `ssh -L` line to use
instead, and does **not** fail:

```
no display on this machine, so no browser to open.
open it yourself, or forward the port: http://127.0.0.1:8899/lab?token=…
(ssh -L 8899:127.0.0.1:8899 <this host>, then use 127.0.0.1:8899)
```

If a browser *should* have opened and did not, that is a failure and it is
reported, rather than passing silently. Setting `BROWSER` overrides the
no-display check, which is the usual answer on a headless box that has a browser
reachable some other way.

If you have lost the address, ask again. These work whether the server came from
`make` or by hand:

```bash
make notebooks-url          # print the URL of the running server
make notebooks-url-open     # print it and hand it to your browser
make notebooks-stop         # stop it, if you lost the terminal it was in
```

The URL is also written to `notebooks/.jupyter-url` so it survives a restart of
the terminal. **That file holds a token, so it is gitignored** — and it is a live
file server address, not a decoration.

> Why Jupyter is told `--no-browser` when the Makefile then opens one: Jupyter's
> own URL has the token masked, so letting it open that lands you on a page
> asking for a password. This Makefile has the real token, so this Makefile
> opens it. The flag is not a decision to have no browser; it is a decision about
> *whose* URL gets opened.

**Use the `Python 3` kernel from this venv.** A `jupyter` earlier on your `PATH` —
from a system Python, conda, pyenv — will open these notebooks under the *wrong
interpreter*, which has no `psycopg` and no pandas, and the failure is a
`ModuleNotFoundError` on cell one that reads like the project is broken. That is
why JupyterLab lives in the venv: `uv run` and the dependencies are the same
environment by construction. If JupyterLab shows a kernel picker, pick
`Python 3 (ipykernel)` and check the status bar names this repository's
`.venv` — if it names anything under `anaconda3`, `miniconda3`, `pyenv` or
`/usr/`, you are in the wrong interpreter and cell one will tell you so.

**`make notebooks-read` had exactly that bug, and it is the reason
`tools/notebook_read.py` exists.** It used to shell out to

    jupyter nbconvert --to html --execute

and `nbconvert`'s command line resolves a kernel by *name*. Every notebook here
declares `kernelspec.name = "python3"`, and on a machine with more than one
Python that is not a name that identifies anything. Here it resolved to
`/opt/homebrew/anaconda3/bin/python`, so all eleven died on cell one — while
`make notebooks`, running the same eleven through nbclient in-process, was
green on the same machine on the same day. The two paths agreed about nothing.

So `make notebooks-read` now goes through the same `NotebookClient` call the gate
uses and renders with `HTMLExporter` *without* `--execute`, which means the
rendering half has no kernel to choose and cannot drift from the gate later.
`tests/test_notebook_kernels.py` fails if anything in this repository starts a
notebook through nbconvert's command line again.

All three of these read `notebooks/_data.py` for the port, so the port in a
connection error tells you which database you reached and nothing else.

## How they are authored

`notebooks/src/*.md` is the source of truth. `notebooks/*.ipynb` is generated.

A `.ipynb` is a JSON file with four hundred lines of escaped markdown in it, and
a git diff on one shows a single enormous changed line because a comma moved in an
output cell. The prose is the thing that gets reviewed, so the prose lives in a
file a reviewer can read.

The payoff is the third fence type. A block tagged `python` runs. A block tagged
`output` is **a claim**, and the gate checks it against what the notebook actually
printed:

````markdown
```python
print(f"{len(readings):,} readings")
```

```output
4,287,657 readings
```
````

If the number is stale the gate says so and names the line. That is why the
notebooks are generated: an `outputs` array in a `.ipynb` is a *record* of the
last run and goes stale silently, while an `output` fence is an assertion.

## The two rules every figure obeys

**Every axis is labelled from the database.** `notebooks/_style.py` reads `unit`
and `field` from the `signal` table, so `{pH}`, `{MPN}/100mL` and `m/(L.h)` are
never typed by hand and a renamed signal breaks the label loudly instead of
mislabelling it quietly.

**Every figure carries its qualifier.** A footer reading
`n=721 · window 12 h · coverage 99.9% · source reading_1m · UTC` is the
repository's thesis made mechanical. `sql/04-expert/04-01` measured `AVG` over
irregularly-sampled data to be wrong by 2.9 % on a regulatory signal; a plot that
does not say what it averaged over is the same mistake in a different medium.

## Never `sns.lineplot`

`seaborn.lineplot` aggregates repeated x-values into a mean and a 95 % band. On a
change-triggered historian that erases the irregular sampling these notebooks
exist to examine, and the band implies inference over observations that are almost
entirely redundant — measured lag-1 autocorrelation is +0.92 on
`PRIMARY:PRI-SCR-1:TORQUE` and +0.999 on `UTILITY:SITE:PLANT_POWER`, so there is
about **one** independent observation in that hour, not sixty.

Use `ax.plot()`, and do the aggregation visibly. seaborn is here for `histplot`,
`ecdfplot`, `boxplot` and `heatmap`.

## The four gates, and what each one catches

`make notebooks` runs all four. They are separate because they catch different
classes of wrongness, and the first three were all written after being bitten by
the thing they now catch.

**1. The output fence.** Every `output` block is compared against what the cell
printed. This is the original claim and it is line-by-line, so it catches a
number that moved.

**2. The prose number.** Every **bold** number in the prose must match something
the notebook printed, at the precision the prose states — so `64 %` matches a
printed `63.9` and `1.78x` matches `1.7791`. This exists because the output
fences did not cover the paragraphs, and the paragraphs are where a re-seed did
its damage: 86 claims moved in three notebooks while the prose around them
stayed confidently wrong. `<!-- num-ok -->` is the escape hatch, for a number that
is a rule rather than a measurement.

**3. The SQL fence.** Every ` ```sql ` block is executed, in a rolled-back
read-only transaction, and must return rows. Zero rows fails: a query that returns
nothing is indistinguishable from a query that is wrong, and "nothing" reads as
success. This is the SQL course's own rule, applied to the SQL embedded here.

**4. The seed fingerprint.** `notebooks/_data.py` holds an integer fingerprint of
`reading` and the gate refuses to run when the database does not match it. Without
it, gates 1 to 3 are all perfectly happy against *the wrong week*, and a
re-seed would turn eleven green notebooks into eleven quietly different ones.

## The one that is not per-notebook

Gates 1 to 4 all compare a number against a run **on the machine doing the
running**, so none of them can catch a number that is true here and false
elsewhere. Two were, both on CI:

* `hypertable_size()` was notebook 03's cost column, beside its row counts. The
  rows matched on a runner to the digit and the megabytes did not, because a
  hypertable is measured in compressed chunks and the compression depends on the
  order the rows were written.
* `abs(weighted - raw)` was notebook 04's proof that a count-weighted mean of
  hourly means *is* `AVG(value)`. It claimed `0.00e+00` — exactly zero. Both sides
  sum the same 168 terms in a different order, and floating-point addition is not
  associative, so the last bit depends on the platform's vector width. The runner
  printed `1.33e-15` from the same rows.

The gate's message for the first one said *re-run it and update the block*, and
following that would have committed a number that is wrong on the laptop it was
written on. The second is now an assertion (`abs(weighted - raw) < 1e-9`) rather
than a printed difference: a yes/no about the arithmetic that can be true on two
machines, instead of a rendering of the noise.

So a fifth rule scans the **python fences of every source** for calls that cannot
produce a portable number — `hypertable_size`, `pg_total_relation_size`,
`time.time`, `pd.Timestamp.now`, `os.getpid`, `platform.uname` and the rest of
`NOT_PORTABLE` in `tools/check_notebooks.py`. Comments and prose are not scanned:
notebook 03 has to *name* `hypertable_size()` to explain why the column is gone,
and a rule that cannot be explained at the site it applies to gets disabled where
it hurts.

It catches the call, not the effect. Notebook 04's floating-point difference was
caught by a human reading the output and recognising an exact zero as too good, and
a notebook that hardcodes `1043` with no call in it passes the rule and fails gate
1 on a runner. That is the honest limit of a source-level rule, and the reason the
list exists rather than the reason it is sufficient.

## Related

- [`docs/DESIGN.md`](../docs/DESIGN.md) — why the schema looks like this
- [`sql/04-expert/`](../sql/04-expert/) — the findings the notebooks build on
- [`docs/LEARNING-LOG.md`](../docs/LEARNING-LOG.md) — every wrong assumption,
  including the ones in these notebooks
