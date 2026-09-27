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

.PHONY: help check lint types test integration sql sql-check \
        up seed wait down clean logs \
        scada scada-flows scada-check dashboards dashboards-check grafana \
        coverage coverage-json alarms browse watch psql query roles contract

help:
	@grep -E '^[a-z][a-zA-Z-]*:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# ── the gates ────────────────────────────────────────────────────────────────

check: lint types test sql  ## everything CI would run
	@echo "── all gates green ──"

lint:  ## ruff
	@echo "── ruff ──"
	uv run --no-sync ruff check .

types:  ## mypy
	@echo "── mypy ──"
	$(PY) -m mypy softplc gateway storage alarms tools

test:  ## unit tests: no database, no containers, about two minutes
	@echo "── unit tests ──"
	$(PY) -m pytest tests/ -q -p no:cacheprovider --ignore=tests/integration

integration:  ## integration tests, against a throwaway database
	@echo "── integration tests ──"
	-docker exec wwtp-db createdb -U wwtp $(TEST_DB) 2>/dev/null || true
	POSTGRES_TEST_PORT=$(TEST_PORT) POSTGRES_TEST_DB=$(TEST_DB) \
	  $(PY) -m pytest tests/integration -q -p no:cacheprovider

sql:  ## every SQL block in the course, against a real server
	@echo "── the SQL course ──"
	$(PY) tools/check_sql.py sql/

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

grafana:  ## Grafana, provisioned from files in git
	@echo "── Grafana on http://127.0.0.1:$${GRAFANA_PORT:-3000} ──"
	docker compose --profile observability up -d grafana

coverage:  ## the fault x rule matrix, about eight minutes
	@echo "── alarm coverage: eleven faults against thirteen rules ──"
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
