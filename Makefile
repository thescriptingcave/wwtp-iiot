# wwtp-iiot — the commands, in one place.
#
#   make            list the targets
#   make check      everything CI would run
#   make up         a running plant with a week of history
#
# This file exists because the gates were being run by hand and therefore were
# run when somebody remembered. A file of documented targets is worth more than a
# CI workflow nobody has added yet, and it is also the thing a reviewer reads to
# find out what "passing" means in this repository.
#
# A `justfile` version of this existed first and was deleted. `just` is not
# installed here, and a task runner that cannot be invoked is a comment with a
# Makefile-shaped hole in your workflow — which is the same lesson as the
# unthrottled scan loop, arrived at a third time.

SHELL := /bin/bash
.SHELLFLAGS := -euo pipefail -a -c
# `make` alone opens the notebooks. It used to print the target list, which is a
# reasonable default for a repository whose whole point is its gates — and the
# wrong default for the one job most people arrive here to do, which is to read
# them. `make help` still prints the list, and `make check` still runs everything
# CI runs.
#
# The prerequisites below are the whole reason this is one command rather than a
# remembered incantation: dependencies, a database, a stopped gateway, and the
# generated notebooks. Each was a thing that had to be right, and each was
# separately forgettable.
.DEFAULT_GOAL := notebooks-open

# Every recipe gets the values from `.env`.
#
# This was missing, and the failure it produced was a lie about where it came
# from: `make notebooks-open` launched JupyterLab with no `POSTGRES_PORT`, so
# `storage.postgres.schema.dsn()` fell back to its defaults — port 5432, password
# "wwtp" — and the first cell of notebook 01 died with
#
#     connection to server at "127.0.0.1", port 5432 failed:
#     FATAL:  password authentication failed for user "wwtp"
#
# which reads like a wrong password rather than a wrong port. The port is the
# tell: this project's database is on 55433 and nothing here has ever run on 5432.
#
# `docker compose` loads `.env` itself, which is exactly why `make up` and
# `make seed` always worked and hid this. Compose was the only thing loading it.
# Anything reaching Postgres directly — `make psql`, `make query`, `make test`,
# `make notebooks-open` — had to be handed the environment by its caller, and I
# had been doing that by hand in my own shell, which is precisely the condition
# under which a missing line goes unnoticed.
#
# `include` reads `.env` as make variables; `export` (with no arguments) puts all
# of them into every recipe's environment. Safe here because every line in `.env`
# is a plain `KEY=value` — a quoted value would keep its quotes, because make
# does not strip them. `tests/test_readme_claims.py` asserts that shape so a
# future `.env` that breaks this fails a test rather than a notebook.
# Every recipe gets the values from `.env`, with **shell** semantics.
#
# ## Why `BASH_ENV` and not `include .env`
#
# `include` is the obvious tool and it is wrong, in two ways that fail silently.
#
# **One.** `.env` is a *shell* file — compose sources it, and it uses shell
# syntax. This one has:
#
#     SOFTPLC_SEED=${SOFTPLC_SEED:-20260926}
#
# Make reads `${SOFTPLC_SEED:-20260926}` as a substitution reference on an
# undefined variable and expands it to **empty** — not the literal string, not an
# error. Sourcing it in bash gives `20260926`, which is what the seeder expects and
# what compose would hand a container.
#
# **Two.** `include` appends the file to `$(MAKEFILE_LIST)`, which the `help`
# target greps. With `.env` in that list, grep prints a filename prefix on every
# match and `make help` lists a target called `Makefile` twenty times instead of
# the twenty targets. That is exactly what happened, in the commit that added the
# `include`, and it went unnoticed because I had stopped running `make help` the
# moment the default goal changed.
#
# `BASH_ENV` is bash's own mechanism: it sources that file at the start of every
# non-interactive shell, which is every recipe. One line, correct semantics, no
# make in the middle.
#
# The `-a` above is load-bearing and was the missing half. Bash sources `BASH_ENV`
# at startup, but a plain assignment in a sourced file creates a *shell* variable,
# not an environment variable — so `.env` was sourced correctly and its values then
# stopped at the shell. `POSTGRES_PORT` was `55433` in the recipe and absent from
# the `python` it ran, which asked the database for 5432 and failed. Observed, not
# theorised: the recipe printed `PORT=55433` and its child printed
# `POSTGRES_PORT=None` in the same run.
#
# `-a` (allexport) makes every assignment from that point on exported, which is
# what `set -a; . .env; set +a` does by hand and what compose does internally.
export BASH_ENV := $(CURDIR)/tools/env.sh

# ## Why any of this exists
#
# `make notebooks-open` launched JupyterLab with no `POSTGRES_PORT`, so
# `storage.postgres.schema.dsn()` fell back to its defaults — port 5432, password
# "wwtp" — and the first cell of notebook 01 died with:
#
#     connection to server at "127.0.0.1", port 5432 failed:
#     FATAL:  password authentication failed for user "wwtp"
#
# which reads like a wrong password rather than a wrong port. **The port is the
# tell**: this project's database is on 55433 and nothing here has ever run on
# 5432.
#
# `docker compose` loads `.env` itself, which is exactly why `make up` and
# `make seed` always worked and hid the gap. Compose was the only thing loading
# it. Anything reaching Postgres directly — `make psql`, `make query`,
# `make test`, `make notebooks-open` — had to be handed the environment by its
# caller, and I had been doing that by hand in my own shell, which is precisely
# the condition under which a missing line goes unnoticed.
#
# ## Precedence
#
# `.env` is sourced *after* the inherited environment, so **`make` wins**:
# `POSTGRES_PORT=5999 make notebooks-open` still targets 55433. That is intended —
# `.env` is this project's configuration, and a stray variable in a shell profile
# should not decide which database gets read. A recipe-level assignment beats
# both, which is how `make integration` reaches `POSTGRES_TEST_PORT`:
#
#     POSTGRES_TEST_PORT=$(TEST_PORT) $(PY) -m pytest ...

# The venv's python, not `uv run` — which needs --no-sync or it strips the
# optional extras and then everything that touches a database fails with an
# ImportError that looks like a code problem.
#
# A *wrapper*, not the interpreter, and the reason is make's fast path. When a
# recipe line has no shell metacharacters, make execs it directly without ever
# starting `$(SHELL)` — so `BASH_ENV` is never read, `tools/env.sh` never runs,
# and `.env` never reaches the command. `$(PY) -m pytest tests/ -q` is exactly
# such a line: no quotes, no metacharacters, no environment. It asked for port
# 5432 and failed with what reads like a wrong password.
#
# The tell is the port: this project is on 55433 and nothing here runs on 5432.
# `tools/py.sh` sources `env.sh` and execs the venv interpreter, so `.env`
# arrives whether make took the fast path or not. Full account, including the
# bisection that identified it, is in that file. It is one line here and it fixes
# every `$(PY)` recipe at once, which no Makefile edit could: the fast path is
# decided per line, from that line's own characters.
PY := $(CURDIR)/tools/py.sh

# Where the tests get a database. POSTGRES_TEST_PORT is deliberately *not* 5432:
# that is where the compose stack lives, and a test that truncates the seeded
# week is a bad afternoon. See the guard in tests/integration/conftest.py.
TEST_DB    ?= wwtp_test
TEST_PORT  ?= 55432

# Where `make notebooks-open` serves. Pinned, not left to Jupyter: port 8888 was
# already in use on this machine and Jupyter moved to 8889 *silently*, so the URL
# in the log stopped matching the documented one and the only symptom was a
# connection refused. A fixed port turns that into a visible collision.
#
# The token is minted here rather than in the recipe. JupyterLab 4 prints its own
# URL with the token masked as `token=...`, so the line it logs is not a URL
# anyone can open — the target has to print its own.
NB_PORT  ?= 8899
NB_TOKEN := $(shell uuidgen 2>/dev/null | tr 'A-Z' 'a-z' | cut -c1-12)

# Which notebooks `make notebooks-read` should do, as name fragments. Empty means
# all of them. A variable rather than a positional argument because the recipe
# line is quoted — without one, `make notebooks-read 05 09` reads `09` as a make
# goal and fails with a message about a target.
NB_ONLY ?=

.PHONY: help setup check lint lint-all lint-debt types test integration sql sql-check \
        up seed wait down clean logs \
        scada scada-flows scada-check dashboards dashboards-check grafana \
        coverage coverage-json alarms browse watch psql query roles contract \
        lessons tableplus notebooks notebooks-build notebooks-read notebooks-open \
        notebooks-url notebooks-url-open notebooks-stop

# `head -1` on $(MAKEFILE_LIST) rather than the whole list.
#
# This target greps for `target:  ## description`, and any *other* file in
# MAKEFILE_LIST would be grepped with a `filename:` prefix on every match, so awk's
# first field becomes the filename and every row reads `Makefile`. It happened
# when `.env` was `include`d and this target silently stopped listing targets.
#
# The first makefile is the only one with targets in it, so it is the only one
# worth reading — and `head -1` means a future `include` cannot break this again.
help:
	@grep -E '^[a-z][a-zA-Z-]*:.*?## .*$$' $(firstword $(MAKEFILE_LIST)) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── the gates ────────────────────────────────────────────────────────────────

# The notebooks are a **separate target from `check`**, deliberately, and the
# reason is cost: `make notebooks` seeds its own seven-day database from nothing
# on the first run, which is about two and a half minutes, plus six minutes of
# execution. A person running `make check` to see whether the tree is healthy
# should not pay that to learn that a notebook's prose number is stale, and a
# target that is only sometimes run is a target that is sometimes true.
#
# It is in `gates.yml` as its own job on every push, so "only run manually" is
# only true of the *local* convenience. `make notebooks` is the same command,
# and is safe to re-run: the data is pinned and the gate is the comparison.
check: lint lint-debt types test sql lessons  ## everything CI would run
	@echo "── all gates green ──"

# **Scoped, and the scoping is on the label.**
#
# `ruff check .` reports 156 findings, almost all `E501` and `PLC0415` in
# `softplc/process/units.py`, `softplc/servers/opcua.py` and the Phase 1-2 test
# files. That debt has been tracked as an open thread since Phase 1 rather than
# swept into a commit claiming to be about something else, and
# `make lint-debt` measures it so it cannot grow.
#
# This target previously ran `ruff check .` and **had been failing the whole
# time**, while every ruff invocation in this project was run over a subset of
# the packages. Same as mypy. A gate that is reported as passing because it was
# run over the wrong subset is worse than no gate.
lint:  ## ruff, on the packages that are clean
	@echo "── ruff ──"
	uv run --no-sync ruff check alarms scada ui storage gateway softplc/scanloop.py

lint-all:  ## every finding, including the tracked debt
	uv run --no-sync ruff check .

lint-debt:  ## fail if the finding count has gone up; never fail if it has gone down
	@echo "── lint debt ratchet ──"
	@uv run --no-sync ruff check . --output-format concise > /tmp/ruff.txt || true
	@n=$$(grep -cE ':[0-9]+:[0-9]+:' /tmp/ruff.txt || true); \
	 b=$$(cat lint-debt-baseline.txt); \
	 echo "   $$n findings, baseline $$b"; \
	 if [ "$$n" -gt "$$b" ]; then \
	   echo "   the debt grew — fix the new findings or raise the baseline"; exit 1; \
	 elif [ "$$n" -lt "$$b" ]; then \
	   echo "   the debt fell — lower lint-debt-baseline.txt in the same commit"; \
	 fi

types:  ## mypy, over every Python package that ships
	@echo "── mypy ──"
	# `workshops` is here because it ships code rather than notebooks-to-be. The
	# list is explicit because a bare `mypy .` follows `.venv` and the build tree, and
	# because `notebooks/` is deliberately absent: it is generated Markdown rendered
	# as .ipynb, and the prose in it is checked by `make notebooks` instead. Both of
	# those are the kind of thing a reader cannot infer from the command, which is why
	# it is a comment here rather than a convention.
	$(PY) -m mypy softplc gateway storage alarms scada tools ui workshops

# `-m "not slow and not integration"` because the label on this target is a
# promise: **no database, no containers, about two minutes.** Without the marker
# filter it kept the four `slow` tests, which are eighteen minutes on their own
# because every scenario settles for 9h15m before measurement begins, so the
# label was wrong and the target was half an hour.
test: setup  ## unit tests: no database, no containers, about two minutes
	@echo "── unit tests ──"
	$(PY) -m pytest tests/ -q -p no:cacheprovider --ignore=tests/integration \
	    -m "not slow and not integration"

# `POSTGRES_TEST_PASSWORD` is passed explicitly, and it is here rather than
# defaulted in `conftest.py` because **`.env` sets `POSTGRES_PASSWORD` for the
# plant's own database.** `$(PY)` sources `.env`, so the variable is always in the
# environment by the time pytest starts, and a `setdefault` for the scratch
# password could never fire. The result was 48 integration tests skipping on a
# machine where the scratch database was running, reachable, and correct —
# reported as `password authentication failed`, which is a true statement about
# a password that was not the one being used.
integration: setup  ## integration tests, against a throwaway database
	@echo "── integration tests ──"
	-docker exec wwtp-db createdb -U wwtp $(TEST_DB) 2>/dev/null || true
	POSTGRES_TEST_PORT=$(TEST_PORT) POSTGRES_TEST_DB=$(TEST_DB) \
	  POSTGRES_TEST_PASSWORD=$${POSTGRES_TEST_PASSWORD:-itpass} \
	  $(PY) -m pytest tests/integration -q -p no:cacheprovider

# `db-still` first, and the reason is the one the notebooks already have: the
# gateway writes. `sql/01-beginner/01-01_ask_a_question.md` counts rows, so a
# server still ingesting answers a different number every few seconds and the
# gate fails on the clock rather than on the query. `check_sql.py` detects that
# and says so — "non-deterministic: run 3 differs from run 1" — which is the
# checker being right and the target being wrong.
# The course needs data, and a freshly started database has none. Failing 74 queries
# with `relation "reading" does not exist` names a symptom; the cause is that nobody
# seeded, and that is one command away from fixed.
db-has-data:
	@$(PY) -m tools.db_ready --data >/dev/null || { \
	  echo "  the database is not ready for the SQL course. It needs a seeded week,"; \
	  echo "  which is one command:"; \
	  echo ""; \
	  echo "      make up"; \
	  echo ""; \
	  exit 1; \
	}

sql: db-still db-has-data  ## every SQL block in the course, against a real server
	@echo "── the SQL course ──"
	$(PY) tools/check_sql.py sql/

lessons: setup  ## every python snippet in courses/, against a live OPC UA server
	@echo "── the lesson courses ──"
	$(PY) tools/check_lessons.py

sql-check: seed sql  ## the one gate that needs a seeded week

notebooks: setup notebooks-has-data  ## build, execute, and verify the claimed output of every notebook
	@echo "── the analyst notebooks ──"
	$(PY) -m tools.build_notebooks
	$(PY) -m tools.check_notebooks

notebooks-build:  ## regenerate the .ipynb files from notebooks/src/*.md
	$(PY) -m tools.build_notebooks

# **After a session in JupyterLab.** Running a notebook writes `execution_count`
# and `outputs` back into the `.ipynb`, and those files are *generated* from
# `notebooks/src/*.md` — so a session leaves 1,000-odd lines of churn that is
# drift by definition. The gate catches it (`check_notebooks` compares the file to
# a fresh build) but only at gate time, by which point it is a confusing failure
# about a file nobody remembers touching.
#
# So the recovery is one command, and it **says what it discarded** rather than
# silently reverting. Knowing the size is the point: "discarded 1399 insertions"
# is a session, and "discarded nothing" means Jupyter never wrote to disk, which is
# the normal case when you close without saving.
#
# It cannot tell you whether you meant to keep something, so it says plainly what
# it is doing. Edits belong in `notebooks/src/*.md` — the `.ipynb` is an artefact
# and a hand-edit there is lost the next time anyone builds.
# ── the machine-learning workshop ──────────────────────────────────────────────
#
# Three targets, in dependency order, so a clean checkout can reach the dataset
# without reading the README. The defaults are the *measured* ones from plan §4.6:
# three weeks at 1 s is enough for every tier-A result, and 25 weeks is a flag rather
# than the default because it is 2.2 M rows for a lesson that does not need them.
#
# `WORKSHOP_DAYS` and the seeder arguments below MUST stay in step: the labels are
# derived from them rather than read from the data, and a mismatch does not fail --
# it produces a table whose labels are confidently wrong. That is the one failure
# mode in this repository that has no alarm, which is why the builder's README says
# it three times.
WORKSHOP_DB    ?= wwtp_ml
WORKSHOP_WEEKS ?= 3
WORKSHOP_HOURS ?= 36
WORKSHOP_END   ?= 2026-09-29T00:00:00Z
#: **The disk knob.** Was hardcoded to `1`, which made the cost of the long window a
#: fixed ~240 bytes per generated reading with no way to trade resolution for space.
#: At 60 s the same 54 weeks is ~1.2 GB instead of ~55 GB.
#:
#: The caution is specific and worth stating: the per-signal baseline in notebook 03
#: is measured in `n`, and at 60 s a genuinely quiet signal writes *zero* rows in 24 h,
#: so `base_24` collapses to 0 for both healthy and broken signals and the 29x
#: separation disappears. Coarser sampling buys the fault count and costs the
#: mechanism. 1 s is the workshop default for that reason and this exists so the
#: trade is available rather than accidental.
WORKSHOP_SAMPLE_INTERVAL ?= 1
# **Parameterised, not hardcoded.** `--out workshops/ml/dataset.csv` meant that
# building any window other than the default overwrote the panel the six notebooks
# read, so "build the 25-week one" silently replaced the 3-week dataset every
# number in `TRAINER.md` was measured on. The name matches the environment
# variable `workshops/ml/_data.py` reads, so `WORKSHOP_PANEL=... make workshop-dataset`
# writes the CSV that `WORKSHOP_PANEL=... make workshop-notebooks` then reads.
WORKSHOP_PANEL ?= workshops/ml/dataset.csv

workshop-seed: setup  ## seed the workshop window: $(WORKSHOP_WEEKS) weeks, a fault every $(WORKSHOP_HOURS) h
	@echo "── creating $(WORKSHOP_DB) if it is not there ──"
	@$(PY) -c "from workshops.ml._data import ensure_database; \
	print('  created' if ensure_database('$(WORKSHOP_DB)') else '  already exists')"
	@echo "── seeding $(WORKSHOP_DB): $(WORKSHOP_WEEKS) weeks, a fault every $(WORKSHOP_HOURS) h ──"
	@echo "   $$(($(WORKSHOP_WEEKS) * 7)) days at $(WORKSHOP_SAMPLE_INTERVAL) s."
	# `--database`, never `POSTGRES_DB=`: `$(PY)` sources `.env` after the inherited
	# environment, so an environment assignment here names `wwtp` -- the plant's own
	# database -- and this target passes `--reset`. An argument cannot be overridden
	# that way, and the seeder refuses `--reset` without one. See
	# `storage/seed/main.py::_target_database`, which is where the reasoning lives.
	# `POSTGRES_HOST` is set here, and only here, because this is the one seeder
	# invocation that runs **on the host** rather than through `docker compose run`
	# (which is how `make seed` reaches it, and where compose supplies the host).
	# `.env` does not set it, so nothing overrides this, and the seeder's "there is
	# nowhere to seed to" check is a container-deployment check rather than a
	# general one -- the library default of 127.0.0.1 is right from a laptop.
	POSTGRES_HOST=127.0.0.1 $(PY) -m storage.seed.main --database $(WORKSHOP_DB) \
	  --days $$(($(WORKSHOP_WEEKS) * 7)) --sample-interval $(WORKSHOP_SAMPLE_INTERVAL) \
	  --fault-every-hours $(WORKSHOP_HOURS) --storm-after 36 \
	  --end $(WORKSHOP_END) --reset

workshop-dataset: setup  ## emit the tidy modelling panel from the seeded window
	@echo "── building the panel ──"
	# **No WORKSHOP_DSN required.** It used to be, and the refusal printed a
	# connection string to copy -- the same information this Makefile already has,
	# transcribed by hand, and wrong the moment `POSTGRES_PORT` in `.env` is not
	# 55433. A fresh `.env` from `.env.example` ships 5432, so the string in any
	# document is wrong on the machine a reader is on. The builder derives it now, the
	# same way the seeder does; `WORKSHOP_DSN` still overrides it for anyone
	# rebuilding a panel somewhere else.
	$(PY) -m workshops.ml.build_dataset \
	  --days $$(($(WORKSHOP_WEEKS) * 7)) \
	  --every-hours $(WORKSHOP_HOURS) \
	  --end $(WORKSHOP_END) \
	  --out $(WORKSHOP_PANEL)

workshop-has-data:  ## fail unless the panel exists and is not empty
	@if [ ! -s $(WORKSHOP_PANEL) ]; then \
	  echo "  $(WORKSHOP_PANEL) is missing."; \
	  echo "  run: make workshop"; \
	  exit 1; \
	fi
	@echo "  $(WORKSHOP_PANEL): $$(wc -l < $(WORKSHOP_PANEL) | tr -d ' ') lines"

# A JupyterLab for the workshop, rooted at `workshops/ml/` and on its own port.
#
# **Its own port, and that is the whole reason.** `notebooks-open` serves
# `--notebook-dir=notebooks` on 8899, so the workshop is not in that tree and cannot
# be reached from it. Parameterising the notebook *tools* for a second track was the
# first half of making the workshop usable; this is the second half, and without it
# the gate runs a notebook nobody can open.
#
# Different port rather than a second directory under `notebooks/`, because the two
# tracks are different audiences on different data and a shared server makes it easy
# to run the wrong one — and a workshop participant following the README should not
# have to know that `notebooks/` exists.
WS_PORT  ?= 8898
WS_TOKEN := $(shell uuidgen 2>/dev/null | tr 'A-Z' 'a-z' | cut -c1-12)

workshop-open: sync workshop-has-data  ## open the workshop notebook in JupyterLab
	@echo "  kernel : Python 3 (ipykernel) - check the status bar says .venv"
	@mkdir -p workshops/ml
	@printf 'http://127.0.0.1:%s/lab?token=%s\n' '$(WS_PORT)' '$(WS_TOKEN)' \
		> workshops/ml/.jupyter-url
	@echo "  open   : http://127.0.0.1:$(WS_PORT)/lab?token=$(WS_TOKEN)"
	@echo "  again  : make workshop-url"
	@echo "  stop   : make workshop-stop"
	@( $(PY) -m tools.jupyter_url --port $(WS_PORT) --open --wait 30 2>&1 || true ) & \
	MPLBACKEND=$${MPLBACKEND:-Agg} $(PY) -m jupyterlab \
		--notebook-dir=workshops/ml \
		--IdentityProvider.token=$(WS_TOKEN) \
		--ServerApp.port=$(WS_PORT) \
		--no-browser

workshop-url:  ## print the workshop JupyterLab URL
	@$(PY) -m tools.jupyter_url --port $(WS_PORT)

workshop-stop:  ## stop the workshop JupyterLab
	@$(PY) -m tools.jupyter_url --port $(WS_PORT) --stop

workshop-notebooks: setup workshop-has-data  ## build, execute and check the workshop notebooks
	@echo "── the workshop notebooks ──"
	$(PY) -m tools.build_notebooks --track workshop
	$(PY) -m tools.check_notebooks --track workshop

# **Both halves, in that order.** It said "seed and build" and depended only on the
# build, so on a machine with no `wwtp_ml` it failed with
# `FATAL: database "wwtp_ml" does not exist` -- which names the database and not the
# command, from a target whose own description promised the seed.
workshop: workshop-seed workshop-dataset  ## seed the window and build the panel, in one go
	@echo "── done. Next: open workshops/ml/README.md ──"

# The 25-week window, into its own database and its own panel.
#
# **Why a separate target and not just `WORKSHOP_WEEKS=25 make workshop`:** that
# works, and it overwrites `workshops/ml/dataset.csv` -- the 3-week panel every
# number in `TRAINER.md` and all six notebooks were measured on. This target points
# both the database and the CSV somewhere else, so building the long window is a
# measurement you can take without changing what the workshop reads.
#
# **Do not size a disk from an interrupted build.** `reading` carries
# `drop_after: '7 days'`, evaluated against `now()`, so the 1-second table trims
# itself while you build. An interrupted build piles up raw rows faster than a
# once-daily retention job can remove them: 25 weeks reached 23 GB that way, while
# a completed 8-week build leaves a 1,040 MB database. The 23 GB figure was measured
# on a build killed at 92.5% of the window, and it was used here to warn about a
# disk that a finished build does not need.
#
# The durable artefact is the CSV, not the database. `workshop-dataset` reads
# `reading_1h`, which has no retention policy, so the panel stays rebuildable after
# `reading` has been trimmed. Keep the CSV.
#
# **Rows per day, measured: 142,654 (3wk), 604,341 (8wk), 557,522 (25wk at 92.5%).**
# Up 4.2x and then down 8% -- **not a power law.** An earlier version of this comment
# called the growth "superlinear and unexplained", which was a name for a shape two
# points do not have. The third point refutes it. Do not extrapolate a disk from a
# row count here; the 23 GB above is the honest counterexample.

#: 8 weeks, and the reason is measured rather than preferred.
#:
#: Notebook 03 needs about **10 positives in the test fold** before a recall figure
#: means anything. Built and measured on 2026-10-01:
#:
#:   | weeks | positives | in a 20% fold | split noise / effect | power? |
#:   |------:|----------:|---------------:|--------------------:|:-------|
#:   |     3 |        22 |              4 |                  42x | no     |
#:   |     8 |        62 |             12 |                  13x | no     |
#:
#: **Eight weeks is not enough either, and that is the finding.** The ratio fell from
#: 42x to 13x, which is real progress and not the answer. Noise/effect falls roughly
#: as 1/positives -- the product is near-constant, 924 at 22 and 806 at 62 -- so
#: reaching 2x needs about 416 positives, which is **about 54 weeks**.
#:
#: So the honest summary for a trainer is that this is not a dataset-size problem you
#: solve on a laptop. It is ~54 weeks at 1 s. The 8-week default exists so the trend
#: can be shown cheaply, not because it settles the question.
WORKSHOP_LONG_WEEKS ?= 8
LONG_DB    ?= wwtp_ml25
#: Named after the week count so `WORKSHOP_LONG_WEEKS=25` does not write a file
#: called `dataset-8wk.csv`, which is the kind of mismatch that makes a later reader
#: distrust both numbers.
LONG_PANEL ?= workshops/ml/dataset-$(WORKSHOP_LONG_WEEKS)wk.csv

workshop-long:  ## build the long window: 8wk default, wwtp_ml25 + dataset-Nwk.csv
	@echo "── this writes $(LONG_DB) and $(LONG_PANEL); the 3-week panel is untouched ──"
	@echo "── 8 weeks gives 62 positives and cuts split noise from 42x to 13x."
	@echo "   Still NOT enough to measure the effect; that needs ~54 weeks. See the"
	@echo "   comment above before sizing a disk."
	$(MAKE) workshop-seed WORKSHOP_DB=$(LONG_DB) WORKSHOP_WEEKS=$(WORKSHOP_LONG_WEEKS)
	$(MAKE) workshop-dataset WORKSHOP_DB=$(LONG_DB) WORKSHOP_WEEKS=$(WORKSHOP_LONG_WEEKS) \
	  WORKSHOP_PANEL=$(LONG_PANEL)
	@echo "── done. $(LONG_PANEL) ──"

notebooks-reset:  ## discard what a Jupyter session wrote back, and report it
	@dirty=$$(git diff --name-only -- 'notebooks/*.ipynb'); \
	if [ -z "$$dirty" ]; then \
	  echo "  no notebook changed since the last commit — nothing to discard"; \
	  echo "  (JupyterLab only writes a file when you save it)"; \
	else \
	  echo "  discarding what a Jupyter session wrote back:"; \
	  git diff --stat -- 'notebooks/*.ipynb' | sed 's/^/    /'; \
	  $(MAKE) --no-print-directory notebooks-build > /dev/null; \
	  echo "  regenerated from notebooks/src/*.md — put edits there, not in the .ipynb"; \
	fi

# Executed HTML, for reading a notebook without Jupyter in the loop.
#
# The committed `.ipynb` files carry **no outputs** — that is deliberate, and it
# is the whole reason they are generated: an `outputs` array is a record of the
# last run and goes stale silently, whereas an `output` fence in
# `notebooks/src/*.md` is a claim that `tools/check_notebooks.py` verifies. So a
# freshly built notebook has empty cells and a reader has to execute it.
#
# This target executes each one and writes the result to `notebooks/read/`, which
# is git-ignored for the same reason `figures/` is: it is derived from the data.
# Read it, do not cite it — if a number here disagrees with the source markdown,
# the source markdown is right.
#
# It goes through this Makefile on purpose. Invoking `python -m jupyter` by hand
# does not load `.env`, so `dsn()` falls back to port 5432 and fails with a
# message that names a password rather than the port.
# **`tools/notebook_read.py`, not `jupyter nbconvert --execute`.** The nbconvert
# command line resolves a kernel by *name*, and every notebook here declares
# `kernelspec.name = "python3"` — a name several interpreters answer to. On this
# machine it answered with `/opt/homebrew/anaconda3/bin/python`, which has no
# psycopg and no pandas, so every notebook died on cell one with
# `ModuleNotFoundError: No module named 'notebooks'`.
#
# `tools/check_notebooks.py` runs the same eleven notebooks through nbclient
# in-process and gets the venv interpreter, because the kernel it starts is the
# process it is running in. Gate green, read path red, same notebooks, same
# machine, same day: two code paths that execute the same thing and agree
# nowhere.
#
# So both go through the same nbclient call, and this one renders with
# `HTMLExporter` *without* `--execute`, which means the rendering half has no
# kernel to choose and cannot disagree with the gate about which one it is.
#
# `NB_ONLY` restricts it to notebooks whose names contain one of the fragments:
#     make notebooks-read NB_ONLY="05 09"
notebooks-read: notebooks-has-data  ## execute every notebook to notebooks/read/*.html, for reading
	@$(PY) -m tools.notebook_read $(NB_ONLY)

# `make` with no arguments. The prerequisites are the whole point: install
# everything, bring up the database, stop the gateway, build the notebooks, check
# there is actually a week of data to read, then open JupyterLab.
#
# Ordering matters and make does not promise it across sibling prerequisites, so
# the stack is ordered through one chain rather than four parallel edges.
notebooks-open: sync db-still notebooks-build notebooks-has-data
	@echo "  kernel : Python 3 (ipykernel) - check the status bar says .venv"
	@echo "  read   : notebooks/read/  (make notebooks-read, for HTML)"
	@mkdir -p notebooks
	@printf 'http://127.0.0.1:%s/lab?token=%s\n' '$(NB_PORT)' '$(NB_TOKEN)' \
		> notebooks/.jupyter-url
	@echo "  open   : http://127.0.0.1:$(NB_PORT)/lab?token=$(NB_TOKEN)"
	@echo "  again  : make notebooks-url          (this URL, again)"
	@echo "  browser: make notebooks-url-open"
	@echo "  stop   : Ctrl-C, or: make notebooks-stop"
	@# **The browser, opened by us and not by Jupyter.** `--no-browser` is still
	@# here and still deliberate: JupyterLab masks the token in the URL it prints
	@# and opens, so its own attempt would land on `token=...` and fail to
	@# authenticate. This Makefile's URL is the real one, so this is what sends it.
	@#
	@# Backgrounded, because JupyterLab runs in the foreground and nothing after
	@# it in this recipe would run until Ctrl-C. `--wait 30` closes the race the
	@# other way: it polls until the port answers, so a browser is never sent to a
	@# server that is not up yet.
	@#
	@# **Its output is not discarded**, which was the first version and was wrong.
	@# Redirecting to /dev/null made this quiet, and quiet is the original
	@# complaint: "the browser does not start, what do I do" gets *more* confusing
	@# when the attempt says nothing. So if there is no display, or no browser
	@# installed, the reason and the URL are printed — which is the answer.
	@# `|| true` only stops the failure from being a *gate* failure; a headless box
	@# has no browser and that is not a broken build.
	@( $(PY) -m tools.jupyter_url --open --wait 30 2>&1 || true ) & \
	MPLBACKEND=$${MPLBACKEND:-Agg} $(PY) -m jupyterlab \
		--notebook-dir=notebooks \
		--IdentityProvider.token=$(NB_TOKEN) \
		--ServerApp.port=$(NB_PORT) \
		--no-browser

# **The URL is also written to `notebooks/.jupyter-url`,** and the reason is the
# one thing this target got wrong for a long time. It printed the address and
# *then* started a server that logs twenty more lines, so on any terminal of
# ordinary height the only line with a usable token was pushed off the top within
# a second. The recovery was a `ps | grep -oE '--ServerApp.token=…'` incantation,
# which is not something to ask a reader to type.
#
# The file is gitignored, and that is load-bearing rather than tidiness: it holds
# a **token**, and a public repository's history is forever. Checked, not assumed
# — with the rule absent `git check-ignore` says the file is committable.
notebooks-url:  ## print the URL of the JupyterLab `make` started
	@$(PY) -m tools.jupyter_url

notebooks-url-open:  ## print it and open it in a browser
	@$(PY) -m tools.jupyter_url --open

notebooks-stop:  ## stop that JupyterLab, without hunting for its terminal
	@$(PY) -m tools.jupyter_url --stop

# Seed the notebooks' own database if it is missing or empty; otherwise leave it.
#
# This used to *refuse* on an empty database and say how to fix it, because the
# database it checked was the plant's main one and seeding rewrites the week —
# destroying something someone may be reading numbers from. That reasoning no
# longer applies: the notebooks read `wwtp_notebooks` (see `notebooks/_data.py`),
# which holds only synthetic data, nothing else reads, and which is seeded to a
# pinned instant so the result is identical every time. Seeding it cannot destroy
# anything that is not exactly reproducible, so `make` just does it — once, about
# two and a half minutes, the first time.
notebooks-has-data:
	@s="$$($(PY) -m tools.notebook_data --status)"; echo "$$s"; \
	case "$$s" in \
	  *readings*) ;; \
	  *) echo "   seeding it now (about two and a half minutes, once)"; \
	     $(PY) -m tools.notebook_data ;; \
	esac

notebooks-data:  ## (re)create and seed wwtp_notebooks, the notebooks' own database
	$(PY) -m tools.notebook_data

# Every optional extra, in one place.
#
# `uv sync` extras are **additive per invocation, not cumulative**. `uv sync
# --extra analysis` on its own installs pandas and matplotlib and *removes*
# asyncua, pymodbus and fastapi, because it resolves the environment against that
# one extra and treats the rest as unwanted. Nothing warns about it.
#
# So every extra is always listed together, and `make sync` is the only supported
# way to install. That is the loose end this closes.
EXTRA := --extra protocols --extra storage --extra analysis --extra serve

# ── the stack ────────────────────────────────────────────────────────────────

# Skipped when the virtualenv already satisfies every import.
#
# `uv` is not on PATH everywhere — a Homebrew or pyenv install can put it outside
# what a non-login shell sees — and the first version of this target made `make`
# fail on a missing `uv` even when the environment was already complete. A command
# ── the first thing a clean checkout needs ───────────────────────────────────

# A fresh `git clone` has no `.env`. It holds this checkout's passwords, so it is
# gitignored and cannot be committed — which means **`make` on a new machine used
# to fail**, and it failed at the least useful place:
#
#     warning: no /home/someone/developer/wwtp-iiot/.env
#     ── starting the database
#     error while interpolating services.db.environment.POSTGRES_PASSWORD:
#     required variable POSTGRES_PASSWORD is missing a value
#     make: *** [Makefile:414: db-up] Error 1
#
# Reported from a real clean clone on a real machine, by somebody doing the
# obvious thing. The `warning` line is this repository's own — `tools/env.sh`
# knows there is no `.env` and says so — and then hands the problem to
# `docker compose`, which reports it as a *password* problem two steps from its
# cause. `README.md` documents `cp .env.example .env` in Quick start, which is a
# section a reader reaches *after* trying `make`, because `make` is what
# everybody tries first.
#
# So `make` makes it, from the tracked template, and never overwrites an existing
# one. This is idempotent, prints what it did, and is a no-op on every run after
# the first.
#
# ## Why this is a target and not a line in `tools/env.sh`
#
# **Because `env.sh` is sourced by far more than `make`.** `tools/py.sh` sources
# it, so a bare `python -m pytest` does too — and so does every test in this
# repository that shells out to `make` or to `$(PY)`. The first version of this
# fix created the file from `env.sh`, and the immediate consequence was that
# running the test suite created a `.env` in the repository root part-way
# through a run. That made the suite **order-dependent**: one test skipped because
# there was no `.env`, another failed because a test before it had created one.
#
# A file that reads the environment should not write the filesystem, and the
# blast radius of "every recipe and every `$(PY)` call" is the wrong place for a
# side effect. A prerequisite is the right shape — and the risk that it is
# *forgotten* on the next target is covered by a test rather than by care, which
# is `tests/test_clean_checkout.py`.
setup:
	@if [ -f .env ]; then :; \
	elif [ -f .env.example ]; then \
	  cp .env.example .env; \
	  echo "── created .env from .env.example (this checkout's passwords)"; \
	else \
	  echo "no .env and no .env.example to copy it from." >&2; \
	  echo "cp .env.example .env, or export POSTGRES_* yourself." >&2; \
	  exit 1; \
	fi

# whose whole job is "open the notebooks" should not refuse to open them because a
# package manager is not installed and nothing needs installing.
# Installed **before** anything that needs Python, and never *through* $(PY) to
# decide whether it is needed: on a checkout that has never been synced there is no
# `.venv` at all, so probing it with `$(PY)` reports every module missing and the
# install is the right answer by accident rather than by decision. `uv.lock` is the
# authority on what "already installed" means, and it is a file rather than an
# interpreter.
sync: setup
	@if [ ! -x .venv/bin/python ]; then \
	  echo "── installing the project (first run on this checkout)"; \
	  uv sync --all-extras; \
	  exit 0; \
	fi
	@if uv sync --all-extras --locked --dry-run >/dev/null 2>&1; then \
	  echo "── dependencies already present"; \
	else \
	  echo "── dependencies changed since last sync"; \
	  uv sync --all-extras; \
	fi

# Is the database reachable, from here, right now?
#
# A subprocess rather than a shell probe so it goes through `dsn()` and therefore
# through `.env` — the same path the notebooks take, so "reachable" means reachable
# *the way the notebooks will find it*. A TCP probe would say yes while a notebook
# still failed on the port.
#: The poll itself stays silent -- sixty copies of a paragraph of error text is
#: unreadable -- and `db-up` runs the same check again, unsilenced, when the wait
#: fails. That is the third time a setup failure has presented as a bare timeout.
#: The first two were real bugs (a seeder writing to the wrong database, and a
#: compose service missing an argument) and neither was diagnosable from a boolean,
#: because the error that would have said so was available on the first attempt and
#: thrown away.
db-live:
	@$(PY) -m tools.db_ready >/dev/null 2>&1 && echo yes || echo no

# Bring the database up only if it is not already answering.
#
# `db-live` first, on purpose. This repository runs its database in compose on port
# 55433, but nothing here *requires* that: a Postgres reachable on the configured
# port is equally good, and `make` should not demand docker to open a notebook when
# the data is already there. The previous version called `docker compose up` first
# unconditionally, which meant `make` failed on a machine with no docker even
# though the database it wanted was right there.
# **`db` *and* `init-db`, not just `db`.** `init-db` is what applies the schema and
# creates the login roles, and nothing makes `docker compose up -d db` run it: `db`
# is the one service in this file that everything else depends on, so it cannot
# depend on anything. Starting the bare database therefore gives you a Postgres with
# no `reading` table, and the first thing that says so is 74 SQL queries failing with
# `relation "reading" does not exist`.
#
# **No `#` comment inside the recipe below.** Bash runs a comment to the newline and a
# trailing backslash *continues the comment*, so a commented-out line ending in `\`
# silently swallows the line after it. That produced
# `/bin/bash: -c: line 1: syntax error: unexpected end of file` from a recipe whose
# text looked correct, and the error named neither the comment nor the line it ate.
db-up: setup sync
	@if [ "$$($(MAKE) --no-print-directory db-live)" = "yes" ]; then \
	  echo "── database already reachable"; \
	else \
	  command -v docker >/dev/null 2>&1 || { \
	    echo "no database, and no docker to start one."; \
	    echo "Set POSTGRES_* in .env for a database you already run, or install docker."; \
	    exit 1; }; \
	  echo "── starting the database"; \
	  docker compose up -d db init-db; \
	  for i in $$(seq 1 60); do \
	    if [ "$$($(MAKE) --no-print-directory db-live)" = "yes" ]; then \
	      echo "   database ready"; exit 0; \
	    fi; sleep 1; \
	  done; \
	  echo "the database did not become reachable in 60s."; \
	  echo ""; \
	  $(PY) -m tools.db_ready || true; \
	  exit 1; \
	fi

# The gateway writes; the notebooks read. Left running it moves the data out from
# under a notebook mid-run, so `make` stops it — and says so, because silently
# stopping a service the user started is its own surprise.
db-still: db-up
	@if [ "$$(docker compose ps --status running --services 2>/dev/null | grep -cx gateway)" = "1" ]; then \
	  echo "   stopping gateway: it writes, and the gates read"; \
	  docker compose stop gateway; \
	fi

up: setup  ## a running plant with a week of history
	@# **Build before `up`.** `docker compose up` reuses an image that exists and
	@# only builds one that does not, so a checkout that has run before runs the
	@# *previous commit's* service code. Nothing says so: the containers start, the
	@# ports answer, and the only symptom is a behaviour that no longer matches the
	@# source you are reading.
	@#
	@# This was found the hard way, twice, and both times the error named something
	@# else. `seed: error: unrecognized arguments: --database=wwtp` was a stale
	@# seeder image. `docker compose down -v` does not catch it: a volume is not an
	@# image, and dropping the volumes is exactly what you do to simulate a clean
	@# machine.
	@#
	@# The cost when nothing changed is about a second per service, because Docker
	@# caches every layer and a warm rebuild is a no-op. `make up` is already two
	@# and a half minutes; this does not change that.
	docker compose build
	docker compose up -d
	$(MAKE) seed
	$(MAKE) wait

seed: setup  ## a week of plant history, about two minutes
	@echo "── a week of plant history ──"
	# **Rebuild the seeder image first.** `docker compose run` builds only when the
	# image is *absent*, so a checkout that has run before reuses an image built from
	# an older commit. That is invisible until the code and the image disagree, and
	# then the error names a command line rather than the staleness:
	#
	#     seed: error: unrecognized arguments: --database=wwtp
	#
	# Found by running `make up` after `docker compose down -v` on a machine whose
	# image predated the `--database` flag. A volume is not an image, so dropping the
	# volumes -- the thing you do to get "a clean machine" -- does not catch it.
	#
	# The build is a no-op when nothing changed: Docker caches the layers, and a warm
	# rebuild of this image takes about a second.
	docker compose --profile demo build seed
	docker compose --profile demo run --rm seed

# Block until the gateway has actually written something, rather than until the
# containers exist. "Up and healthy" was true for an hour while the gateway
# spooled and nothing landed, which is the bug the spool-rotation fix addressed.
wait:  ## block until the first readings land
	@echo "── waiting for the first readings ──"
	@for i in $$(seq 1 90); do \
	  n=$$(docker compose exec -T db psql -U wwtp -d wwtp -tAc \
	       "SELECT count(*) FROM reading" 2>/dev/null | tr -d ' \n'); \
	  if [ "$$n" -gt 0 ] 2>/dev/null; then echo "   $$n readings"; exit 0; fi; \
	  sleep 2; \
	done; \
	echo "no readings after 3 minutes — try: docker compose logs gateway"; exit 1

down:  ## stop, keeping the volumes
	docker compose down --remove-orphans

clean:  ## stop and delete the volumes, including the seeded week
	docker compose down -v --remove-orphans

logs:  ## follow the plant and the gateway
	docker compose logs -f softplc gateway

# ── the alarm engine ─────────────────────────────────────────────────────────

scada:  ## Node-RED, the operator flows
	@echo "── Node-RED on http://127.0.0.1:$${SCADA_PORT:-18880}/scada ──"
	docker compose --profile scada build scada
	docker compose --profile scada up -d scada
	@echo "   the editor is bound to loopback; the flows are generated from the contract"
	@echo "   (this rebuilds the image: entrypoint.sh and settings.js are baked in, and"
	@echo "    'up -d' alone reuses a stale one and looks like your change did nothing)"

scada-flows:  ## regenerate the tag list and the flows from the contract
	$(PY) -m scada.generate_tags
	$(PY) -m scada.build_flows

scada-check:  ## report drift between the contract and the generated files
	$(PY) -m scada.generate_tags --check
	$(PY) -m scada.build_flows --check

dashboards:  ## regenerate the Grafana dashboards from the contract
	$(PY) -m ui.grafana.generate_dashboards

dashboards-check:  ## report drift between the contract and the dashboards
	$(PY) -m ui.grafana.generate_dashboards --check

tableplus:  ## extract the course's queries into sql/TablePlus/*.sql
	$(PY) -m tools.extract_sql

tableplus-check:  ## report drift between the lessons and sql/TablePlus
	$(PY) -m tools.extract_sql --check

page:  ## regenerate the web dashboard's read model from the contract
	$(PY) -m ui.web.generate_page

page-check:  ## report drift between the contract and the web page
	$(PY) -m ui.web.generate_page --check

web:  ## the custom dashboard, on http://127.0.0.1:$${WEB_PORT:-3001}
	@echo "── web on http://127.0.0.1:$${WEB_PORT:-3001} ──"
	docker compose --profile ui up -d web
	@echo "   read-only role wwtp_ui; no credential reaches the browser"

# `npm ci` rather than `npm install`: it installs from the lockfile and *fails*
# if the lock and the manifest disagree, so a hand-edited package.json is an
# error instead of a silently different build. `--ignore-scripts` because none of
# these dependencies needs a postinstall and one of them (sharp) would otherwise
# fetch a platform binary at build time.
web-build:  ## typecheck and build the dashboard image contents
	cd ui/web && npm ci --ignore-scripts --no-audit --no-fund
	cd ui/web && npx tsc --noEmit
	cd ui/web && npm run build

grafana:  ## Grafana, provisioned from files in git
	@echo "── Grafana on http://127.0.0.1:$${GRAFANA_PORT:-3000} ──"
	docker compose --profile observability up -d grafana

coverage:  ## the fault x rule matrix, about eight minutes
	@echo "── alarm coverage: twelve faults against sixteen rules ──"
	$(PY) -m alarms.main coverage --hours 8

coverage-json:  ## the same, machine-readable
	$(PY) -m alarms.main coverage --hours 8 --json > coverage.json
	@echo "wrote coverage.json"

alarms:  ## the engine, against the live historian
	$(PY) -m alarms.main serve

# ── one-offs ─────────────────────────────────────────────────────────────────

browse:  ## browse the OPC UA address space
	$(PY) tools/opcua_browser.py browse

watch:  ## watch one signal (make watch SIGNAL=AERATION:AHU-1:DO)
	$(PY) tools/opcua_browser.py watch $(SIGNAL)

psql:  ## a shell on the database
	docker compose exec db psql -U wwtp -d wwtp

query:  ## run one query (make query SQL="SELECT count(*) FROM reading")
	$(PY) tools/sqlrun.py "$(SQL)"

roles:  ## what the database roles actually hold
	$(PY) -m storage.postgres.roles --check

contract:  ## the contract in one line per signal
	$(PY) -c "from softplc.contract import contract; print(contract().summary())"
