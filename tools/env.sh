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
set -a
# shellcheck disable=SC1091
. "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.env"
set +a
