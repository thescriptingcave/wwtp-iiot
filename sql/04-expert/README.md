# 04 — Expert

Four lessons about the questions where a query returns a confident, plausible,
wrong answer. Stages 01 and 02 taught you to write a correct `SELECT`; stage 03
taught you about the storage engine underneath. This stage is about the part
where all three go wrong at once.

| Lesson | The question |
|---|---|
| [04-01](04-01_time_weighted_averages.md) | What was the average over that window — and why is `AVG` not the answer? |
| [04-02](04-02_change_detection.md) | Has this signal moved, and is this silence a fault? |
| [04-03](04-03_fixing_a_slow_query.md) | This query is slow. Which of the numbers in the plan can I trust? |
| [04-04](04-04_the_dashboard_query.md) | The query the operators actually look at has a bug in it. |

## What changed, and why this stage could not exist before

**Every earlier stage had a right answer to check against.** "What is the average
DO" has a correct answer, and 04-01 is about the fact that the obvious query does
not produce it. That is a different kind of problem from "how do I write a `JOIN`",
and it needed a stage of its own.

**And the questions became questions about the data rather than the language.**
Some signals here sample once a second; some produce nine readings in a day. A
query that is right for the first and wrong for the second is not wrong — it is
*unqualified*, and unqualified is what this stage is about.

## Before you start

```bash
docker compose --profile demo run --rm seed
```

The whole course needs the full week, and this stage needs it most: 04-01's
comparison is meaningless without a signal that samples at 1 Hz next to one that
produces 39 points in six hours, and 04-02's baselines are computed over seven
days of gaps.

## The habit from stages 01–03, now with a reason

Every lesson anchors to the data rather than the clock:

<!-- check: skip -->
```sql
WHERE ts >= (SELECT max(ts) FROM reading) - interval '1 day'
```

In 04-03 this stops being a correctness convention and becomes a **performance**
mechanism: the subquery is evaluated once as an `InitPlan`, and that is what lets
the engine exclude whole chunks before reading a row. A literal cannot do it.
`now()` would exclude everything.

## The one idea the whole stage rests on

> **A number without a qualifier is a claim you cannot check.**

It is the same rule as [00-foundations](../00-foundations/), arrived at from the
other end. A broken instrument is `value IS NULL` with `quality = 2`. Stage 00
showed that storing the value and the status separately is what lets you recover
one without the other. This stage is what it costs to then *use* them:

| qualifier | the lesson | what goes wrong without it |
|---|---|---|
| `quality` | [04-01](04-01_time_weighted_averages.md), [04-04](04-04_the_dashboard_query.md) | bad readings filtered away, and the gap they covered deleted with them |
| coverage | [04-01](04-01_time_weighted_averages.md) | an average over a window nobody measured all of |
| a per-signal baseline | [04-02](04-02_change_detection.md) | a rain gauge reported as broken at 3am |
| `samples` per bucket | [04-04](04-04_the_dashboard_query.md) | a trend that looks continuous and is not |
| buffers, not ms | [04-03](04-03_fixing_a_slow_query.md) | "it got slower", said on the strength of noise |

## Two of these lessons find bugs in this repository

Not as an exercise — as the actual content.

**04-04** is a defect in `ui/grafana/generate_dashboards.py`: the trend query
uses `avg(value)` inside a `time_bucket` and filters `value IS NOT NULL` in the
`WHERE` clause. On a screen scraper sampled at ~1.2 s that is a mean error of
**1.9** and a worst-case of **6.7** on every five-second bucket. The lesson
explains why the time-weighted fix from 04-01 is *also* wrong here, and what the
right answer is instead.

**04-02** documents the seeder artefact that produces five-day gaps on eight
signals, so that a reader who finds it in the data knows it is a generation
artefact rather than a simulated plant fault.

## A warning about the numbers in these lessons

As in stage 03: every figure was measured on one machine against one seeded week,
and yours will differ. The **structural** results hold, because they follow from
the sampling intervals rather than from the values:

- `AVG`'s error tracks sampling irregularity, not volume — 21,601 points at
  0.001 % error next to 39 points at 2.9 %
- a gap that is unremarkable for a digester probe is a serious fault for a lift
  current, and no constant threshold separates them
- buffers reproduce; wall-clock timings do not — the same query in 04-03 measured
  12.8 ms and 9.8 ms on unchanged data

## Where this stage ends

04-04 assembles everything into the query the plant runs, and the honest summary
of the stage is in its closing note: **the SQL did not get harder.** Every clause
is plain Postgres this course has already met. What changed is that you can now
look at four defensible-looking choices and say which one is wrong, and why.
