#!/usr/bin/env bash
# Sourced by bash at the start of every recipe, via the Makefile's
# `export BASH_ENV`. Puts `.env` into every recipe's environment.
#
# ## Why this file exists rather than `include .env` in the Makefile
#
# Three failures, in order of how long they took to find.
#
# **`.env` is a shell file.** It contains
#
#     SOFTPLC_SEED=${SOFTPLC_SEED:-20260926}
#
# and GNU make reads that as a substitution reference on an undefined variable,
# expanding it to **empty** — not the literal string, not an error. Sourcing it
# gives `20260926`, which is what the seeder expects and what compose hands a
# container.
#
# **`include` also corrupts `$(MAKEFILE_LIST)`**, which the `help` target greps.
# With `.env` in the list, grep prints a filename prefix on every match and
# `make help` lists a target called `Makefile` twenty times instead of the twenty
# targets. That is not hypothetical: it is what happened, in the commit that added
# the `include`, and it went unnoticed because the default goal had just changed
# away from `help` and nobody ran it again.
#
# **Sourcing alone is not enough.** Bash sets shell variables, not environment
# variables, so `.env` was sourced correctly and its values then stopped at the
# shell: `POSTGRES_PORT` read `55433` in the recipe and was `None` in the
# `python` that recipe launched, which asked for port 5432 and failed. Both facts
# were printed in the same run. Hence `set -a` here rather than a bare `. .env`.
#
# ## Why not `set -a` via `.SHELLFLAGS`
#
# Because `.SHELLFLAGS` needs GNU make 3.82 (2010) and the make on this machine is
# **3.81**, which silently ignores it. Every recipe here has therefore run without
# `errexit`, without `nounset` and without `pipefail` since the Makefile was
# written — which is a separate bug, recorded in docs/LEARNING-LOG.md, and not
# something this file can fix for a make that will not read the setting.
#
# The repo root is derived from this file's own location rather than assumed, so
# BASH_ENV works from any directory and needs no make variable.
#
# ## Why a missing `.env` is a warning and not an error
#
# `.env` is gitignored, so **it does not exist in CI at all** — the workflow
# supplies the environment directly, and a checkout has nothing to source. This
# file is sourced by bash at the top of *every* recipe, so the first version's
# bare `.` made the failure total:
#
#     tools/env.sh: line 43: /…/.env: No such file or directory
#     make: *** [Makefile:NNN: some-target] Error 1
#
# and it did so for every target, including the ones that have nothing to do with
# the environment. It also cost a CI run: `tests/test_makefile_env.py` invokes
# the real `$(PY)` wrapper, so four tests failed on a runner that was doing
# nothing wrong and had no `.env` by design.
#
# So: warn, on stderr, once per shell, and carry on. A recipe that genuinely needs
# a variable reports that itself, and the message says where to get the file
# rather than leaving the reader to work out that a *sourced* file was absent.
# Failing here instead would mean `make` could not run at all in the one
# environment where it is supposed to be reproducible.

# Where the repository is, from this file's own location rather than assumed.
# `BASH_ENV` is sourced before any recipe runs, so there is no make variable yet
# and nothing has `cd`-ed anywhere useful.
_ENV_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -f "$_ENV_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${_ENV_ROOT}/.env"
  set +a
elif [ -z "${_ENV_WARNED:-}" ]; then
  echo "warning: no ${_ENV_ROOT}/.env — 'cp .env.example .env' is the usual fix;" >&2
  echo "         continuing with this shell's own variables" >&2
  _ENV_WARNED=1
fi
