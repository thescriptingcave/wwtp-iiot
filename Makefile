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

.PHONY: help check lint lint-all lint-debt types test integration sql sql-check \
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
# `ruff check .` reports 157 findings, almost all `E501` and `PLC0415` in
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

types:  ## mypy, over every Python file in the project
	@echo "── mypy ──"
	$(PY) -m mypy softplc gateway storage alarms scada tools ui

# `-m "not slow and not integration"` because the label on this target is a
# promise: **no database, no containers, about two minutes.** Without the marker
# filter it kept the four `slow` tests, which are eighteen minutes on their own
# because every scenario settles for 9h15m before measurement begins, so the
# label was wrong and the target was half an hour.
test:  ## unit tests: no database, no containers, about two minutes
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
integration:  ## integration tests, against a throwaway database
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
sql: db-still  ## every SQL block in the course, against a real server
	@echo "── the SQL course ──"
	$(PY) tools/check_sql.py sql/

lessons:  ## every python snippet in courses/, against a live OPC UA server
	@echo "── the lesson courses ──"
	$(PY) tools/check_lessons.py

sql-check: seed sql  ## the one gate that needs a seeded week

notebooks: notebooks-has-data  ## build, execute, and verify the claimed output of every notebook
	@echo "── the analyst notebooks ──"
	$(PY) -m tools.build_notebooks
	$(PY) -m tools.check_notebooks

notebooks-build:  ## regenerate the .ipynb files from notebooks/src/*.md
	$(PY) -m tools.build_notebooks

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
	@echo "  browser: make notebooks-url-open     (or just copy the line above)"
	@echo "  stop   : Ctrl-C, or: make notebooks-stop"
	@MPLBACKEND=$${MPLBACKEND:-Agg} $(PY) -m jupyterlab \
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
# whose whole job is "open the notebooks" should not refuse to open them because a
# package manager is not installed and nothing needs installing.
sync:
	@missing=""; \
	for m in asyncua pymodbus psycopg fastapi pandas matplotlib seaborn \
	         jupyterlab nbformat nbclient; do \
	  $(PY) -c "import $$m" >/dev/null 2>&1 || missing="$$missing $$m"; \
	done; \
	if [ -n "$$missing" ]; then \
	  echo "── installing:$$missing"; \
	  uv sync $(EXTRA); \
	else \
	  echo "── dependencies already present"; \
	fi

# Is the database reachable, from here, right now?
#
# A subprocess rather than a shell probe so it goes through `dsn()` and therefore
# through `.env` — the same path the notebooks take, so "reachable" means reachable
# *the way the notebooks will find it*. A TCP probe would say yes while a notebook
# still failed on the port.
db-live:
	@$(PY) -c "from storage.postgres.schema import connect; \
	c = connect(); c.execute('SELECT 1'); c.close()" >/dev/null 2>&1 \
	&& echo yes || echo no

# Bring the database up only if it is not already answering.
#
# `db-live` first, on purpose. This repository runs its database in compose on port
# 55433, but nothing here *requires* that: a Postgres reachable on the configured
# port is equally good, and `make` should not demand docker to open a notebook when
# the data is already there. The previous version called `docker compose up` first
# unconditionally, which meant `make` failed on a machine with no docker even
# though the database it wanted was right there.
db-up:
	@if [ "$$($(MAKE) --no-print-directory db-live)" = "yes" ]; then \
	  echo "── database already reachable"; \
	else \
	  command -v docker >/dev/null 2>&1 || { \
	    echo "no database, and no docker to start one."; \
	    echo "Set POSTGRES_* in .env for a database you already run, or install docker."; \
	    exit 1; }; \
	  echo "── starting the database"; \
	  docker compose up -d db; \
	  for i in $$(seq 1 60); do \
	    if [ "$$($(MAKE) --no-print-directory db-live)" = "yes" ]; then \
	      echo "   database ready"; exit 0; \
	    fi; sleep 1; \
	  done; \
	  echo "the database did not become reachable in 60s"; exit 1; \
	fi

# The gateway writes; the notebooks read. Left running it moves the data out from
# under a notebook mid-run, so `make` stops it — and says so, because silently
# stopping a service the user started is its own surprise.
db-still: db-up
	@if [ "$$(docker compose ps --status running --services 2>/dev/null | grep -cx gateway)" = "1" ]; then \
	  echo "   stopping gateway: it writes, and the gates read"; \
	  docker compose stop gateway; \
	fi

up:  ## a running plant with a week of history
	docker compose up -d
	$(MAKE) seed
	$(MAKE) wait

seed:  ## a week of plant history, about two minutes
	@echo "── a week of plant history ──"
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
	docker compose --profile scada up -d scada
	@echo "   the editor is off; the flows are generated from the contract"

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
	@echo "── alarm coverage: twelve faults against fourteen rules ──"
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
