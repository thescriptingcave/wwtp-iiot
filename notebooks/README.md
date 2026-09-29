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
uv sync --extra protocols --extra storage --extra analysis
docker compose up -d db
docker compose run --rm init-db
docker compose --profile demo run --rm seed
docker compose stop gateway          # the gateway writes; stop it or the data moves

make notebooks                        # build, execute, and verify every claim
```

`make notebooks` is the whole contract: it regenerates the `.ipynb` files, runs
every one against the live database, and fails if a number in the prose no longer
matches what the database says.

To **read** one rather than run the gate, open JupyterLab in the project's own
environment:

```bash
uv sync --extra protocols --extra storage --extra analysis --extra serve
make notebooks-open
```

It prints a URL with a token in it — use that one. JupyterLab serves on `8899`
and mints the token itself.

**Use the `Python 3` kernel from this venv.** A `jupyter` earlier on your `PATH` —
from a system Python, conda, pyenv — will open these notebooks under the *wrong
interpreter*, which has no `psycopg` and no pandas, and the failure is a
`ModuleNotFoundError` on cell one that reads like the project is broken. That is
why JupyterLab lives in the venv: `uv run` and the dependencies are the same
environment by construction. If JupyterLab shows a kernel picker, pick
`Python 3 (ipykernel)` and check the status bar says
`/Users/dev/Developer/wwtp-iiot/.venv`.

**The database port comes from `.env`,** which the `Makefile` reads and exports to
every recipe. If you start Jupyter some other way — `uv run jupyter lab`, a
notebook opened in an editor, a bare `jupyter` — load `.env` yourself first, or
`dsn()` falls back to port 5432 and you get:

```
connection to server at "127.0.0.1", port 5432 failed:
FATAL:  password authentication failed for user "wwtp"
```

which names a *password* when the real fault is the *port*. This project's database
is on **55433**; nothing in the repository has ever run on 5432, so the port in
that message is the tell.

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

## The series

| # | notebook | the question | |
|---|---|---|---|
| 1 | [Meet the plant](01-meet-the-plant.ipynb) | what is here, what it does, and which tags are secretly the same measurement | **done** |
| 2 | [Three kinds of nothing](02-three-kinds-of-nothing.ipynb) | no data, bad data, no change — `dropna` cannot tell them apart | **done** |
| 3 | Choosing your tier | raw, 1-minute, or 1-hour — and what each costs | |
| 4 | [Resampling buys you nothing](04-resampling-buys-you-nothing.ipynb) | the rollups are a different estimator, not a faster one | **done** |
| 5 | Autocorrelation | how many independent observations are in that hour | |
| 6 | Detrending and differencing | stationarity before modelling | |
| 7 | What are you estimating | time- versus flow-weighted | |
| 8 | Rolling-origin validation | why a random split lies | |
| 9 | Anomaly detection | per-signal expectations, not global thresholds | |
| 10 | Change points | when did the plant change, not just what | |
| 11 | Dose or flow | attributing a change to an intervention | |

Notebooks 01, 02 and 04 exist so far, and the table does not pretend otherwise — a course
index that links to unwritten lessons is the same failure as a count in prose, and
this repository has already been bitten by both.

02 is second in the list and first in the *argument*: it establishes the
vocabulary the other nine use. Read it after 01, not instead of it — 01 gives you
the plant and 02 gives you the three ways a signal can fail to give you a number.

04 is where the series turns: it shows that `AVG(value)` and an hourly rollup do not
disagree, they **answer different questions**, and that the two can be 21 % apart
on a control signal.

## Related

- [`docs/DESIGN.md`](../docs/DESIGN.md) — why the schema looks like this
- [`sql/04-expert/`](../sql/04-expert/) — the findings the notebooks build on
- [`docs/LEARNING-LOG.md`](../docs/LEARNING-LOG.md) — every wrong assumption,
  including the ones in these notebooks
