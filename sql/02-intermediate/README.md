# 02 — Intermediate

Four lessons. The questions stop being about one signal and start being about
*relationships*: between signals, between hours, between what the plant did and
what it should have done.

| Lesson | The question |
|---|---|
| [02-01](02-01_ctes.md) | How do you stop a query becoming unreadable? |
| [02-02](02-02_joins.md) | What happens when a reading has no equipment? |
| [02-03](02-03_window_functions.md) | How fast is this signal moving, compared with its own history? |
| [02-04](02-04_gaps.md) | When did the plant stop reporting — and how do you tell? |

## What changed, and why this stage could not exist before

This stage was unwritable in the previous version of the project, and the reason
is worth stating before the lessons, because it is the clearest argument for the
storage engine this course now runs on.

Every lesson below needs something the old dialect did not have: a `CASE`
expression, a subquery in a `FROM`, a correlated subquery, a window function, a
`LEFT JOIN` against a generated table, a `NOT EXISTS`. Not exotic things —
`CASE` and window functions are in the first hour of any SQL course.

The old course's `DIALECT.md` listed six things the query language could not do.
Every one of them is used in this stage. The workarounds were not simplifications
of a hard problem; they were the only way to express these questions at all.

The other reason: **the two databases could not be joined.** Reading a number and
knowing what instrument it came from, in one query, required two connections and
the data in between. Lesson [02-02](02-02_joins.md) is a single `JOIN`.

## Before you start

```bash
docker compose --profile demo run --rm seed
```

## The habit from stage 01 still applies

Anchor to the data, not the clock:

<!-- check: skip -->
```sql
WHERE ts >= (SELECT max(ts) FROM reading) - interval '1 day'
```

And in this stage it starts earning its keep: from here on several lessons compute
the anchor more than once, and a `CTE` is the clean way to compute it once.

## `EXPLAIN` is not optional any more

Stage 01's queries are small enough that you do not need to think about plans.
These are not. When a query in this stage is slow — and some will be, against 4.3
million rows — start here:

<!-- check: skip -->
```sql
EXPLAIN (ANALYZE, BUFFERS) <your query>;
```

`04-expert` is about reading the output. For now, the only two lines that matter:

* **actual time=…** — how long each step took. The number in the *first* line is
  the total.
* **Buffers: shared hit=… read=…** — how many 8 kB pages it pulled off disk.
  `hit` is in cache, `read` is not. A query doing millions of `read`s is doing
  disk seeks, and that is where the time went.
