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
.SHELLFLAGS := -euo pipefail -c
.DEFAULT_GOAL := help

# The venv's python, not `uv run` — which needs --no-sync or it strips the
# optional extras and then everything that touches a database fails with an
# ImportError that looks like a code problem.
PY := .venv/bin/python

# Where the tests get a database. POSTGRES_TEST_PORT is deliberately *not* 5432:
# that is where the compose stack lives, and a test that truncates the seeded
# week is a bad afternoon. See the guard in tests/integration/conftest.py.
TEST_DB    ?= wwtp_test
TEST_PORT  ?= 55432

.PHONY: help check lint lint-all lint-debt types test integration sql sql-check \
        up seed wait down clean logs \
        scada scada-flows scada-check dashboards dashboards-check grafana \
        coverage coverage-json alarms browse watch psql query roles contract

help:
	@grep -E '^[a-z][a-zA-Z-]*:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── the gates ────────────────────────────────────────────────────────────────

check: lint lint-debt types test sql lessons  ## everything CI would run
	@echo "── all gates green ──"

# **Scoped, and the scoping is on the label.**
#
# `ruff check .` reports 160 findings, almost all `E501` and `PLC0415` in
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

integration:  ## integration tests, against a throwaway database
	@echo "── integration tests ──"
	-docker exec wwtp-db createdb -U wwtp $(TEST_DB) 2>/dev/null || true
	POSTGRES_TEST_PORT=$(TEST_PORT) POSTGRES_TEST_DB=$(TEST_DB) \
	  $(PY) -m pytest tests/integration -q -p no:cacheprovider

sql:  ## every SQL block in the course, against a real server
	@echo "── the SQL course ──"
	$(PY) tools/check_sql.py sql/

lessons:  ## every python snippet in courses/, against a live OPC UA server
	@echo "── the lesson courses ──"
	$(PY) tools/check_lessons.py

sql-check: seed sql  ## the one gate that needs a seeded week

# ── the stack ────────────────────────────────────────────────────────────────

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
