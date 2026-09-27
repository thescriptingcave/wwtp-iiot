# 01 — Beginner

**The goal of this stage:** go from *reading a value* to *answering a question*,
and meet `time_bucket` — the function that makes this a time-series course rather
than a SQL course.

You already know the shape of the tables from [00](../00-foundations/). This stage
assumes it.

| Lesson | The question it answers |
|---|---|
| [01-01](01-01_ask_a_question.md) | What is the plant doing right now? |
| [01-02](01-02_filtering.md) | When was it doing that? |
| [01-03](01-03_aggregating.md) | What is the average, and over what? |
| [01-04](01-04_time_buckets.md) | How did it get there? |

---

## The three functions you will use constantly

```sql
time_bucket(INTERVAL '1 hour', time)   -- group timestamps into buckets
COUNT(*) / COUNT(value)                -- rows total / rows usable
mean(value)                            -- ignores NULLs
```

`time_bucket` is the one that makes this different. In a normal database you group
by a column; here you group by a *width of time*, and the width is part of the
question. "What was the average DO" is not a question — "what was the average DO
**per hour**" is, and the answer changes with the interval you pick.

That is the thing that makes time-series querying a distinct skill, and
[01-04](01-04_time_buckets.md) is where it gets interesting: the correct interval
depends on what you are trying to see, and choosing it badly hides the very thing
you were looking for.
