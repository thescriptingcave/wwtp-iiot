#!/usr/bin/env bash
# The project's Python, with `.env` in its environment. `PY` in the Makefile.
#
# ## Why this file exists
#
# GNU make has a "fast path": when a recipe line contains **no shell
# metacharacters**, make forks and execs the command *directly* and never starts
# `$(SHELL)` at all. `$(SHELL)` is the only thing that reads `$BASH_ENV`, so on
# that path `tools/env.sh` never runs, `.env` is never sourced, and the command
# inherits make's own environment — which on a laptop is a `POSTGRES_PORT` that
# does not exist.
#
# It is silent, and it looks like a password problem:
#
#     psycopg.OperationalError: connection failed: connection to server at
#     "127.0.0.1", port 5432 failed: FATAL:  password authentication failed
#     for user "wwtp"
#
# The port is the tell. This project's database is on 55433 and nothing here has
# ever run on 5432.
#
# Observed, not theorised. `make query SQL="select 1"` worked and
# `make notebooks-data` failed, in the same file, at the same moment. The only
# difference between the two recipe lines is that the first contains a `"`:
#
#     query:          $(PY) tools/sqlrun.py "$(SQL)"   # has quotes -> shell
#     notebooks-data: $(PY) -m tools.notebook_data       # no quotes -> fast path
#
# Confirmed by bisection — adding one `;` to the end of the failing recipe line
# fixes it, and removing it breaks it again:
#
#     $(PY) -m tools.notebook_data --status      # not available (OperationalError)
#     $(PY) -m tools.notebook_data --status ;    # 4,239,284 readings, the pinned seed
#
# Three of this repository's gates were in the fast-path class: `make test`,
# `make sql` and `make lessons`. `make test` passed anyway, because unit tests
# need no database — which is the worst possible outcome, a gate that reports
# green while running in the wrong environment. `make sql` and `make lessons`
# needed one and failed.
#
# ## Why the fix is here and not in the Makefile
#
# Because the fast path is decided per recipe line, from the line's characters.
# There is no switch to turn it off, and no variable to defeat it: the only ways
# out are to put a metacharacter in every recipe, or to make the *command*
# itself a shell. This file is the second, and it fixes all of them at once.
#
# It sources `env.sh` rather than repeating `set -a; . .env`, so there is still
# exactly one place that knows how `.env` is loaded. That also means this works
# when make takes the fast path and runs the script directly: the kernel honours
# the shebang, bash reads `env.sh`, and the `exec` hands the variables to Python.
#
# ## What this does not change
#
# `.env` is still sourced *after* the inherited environment, so an inherited
# variable does not win — see the "Precedence" section of the Makefile. And a
# recipe that does not go through `$(PY)` still depends on `BASH_ENV`, which is
# unchanged.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./env.sh
. "$here/env.sh"
# ## Why the interpreter is checked rather than exec'd
#
# On a checkout where `uv sync` has never run, `.venv/bin/python` does not exist and
# `exec` fails with
#
#     /path/to/tools/py.sh: line 63: /path/to/tools/../.venv/bin/python:
#       No such file or directory
#
# which names a line of a shell script, not the missing thing and not the command
# that creates it. `$(PY)` appears in 51 recipes, and the first one to need Python
# on a fresh clone reported it 60 times while waiting for a database that was already
# up and healthy -- because `db-live` calls `$(PY)` too, so every poll failed for the
# same reason and the wait timed out.
#
# So: say what is missing, say the one command that fixes it, and fail. A fast,
# specific failure is worth more here than a fast success, because the slow one
# looks like a database problem and sends the reader to the wrong place.
python="$here/../.venv/bin/python"
if [ ! -x "$python" ]; then
  echo "error: no project Python at $python" >&2
  echo "" >&2
  echo "  this checkout has never been installed. One command does it:" >&2
  echo "" >&2
  echo "      make sync" >&2
  echo "" >&2
  echo "  (or: uv sync --all-extras, which is what 'make sync' runs.)" >&2
  exit 1
fi
exec "$python" "$@"
