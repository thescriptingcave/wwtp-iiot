# 01 — Beginner

Four lessons. By the end of them you can ask a question of the plant and get an
answer you would be willing to put in front of an operator.

| Lesson | The question |
|---|---|
| [01-01](01-01_ask_a_question.md) | What is this signal doing right now? |
| [01-02](01-02_filtering.md) | When was it outside its normal band? |
| [01-03](01-03_aggregating.md) | What is the average, and over what? |
| [01-04](01-04_time_buckets.md) | What did each hour look like? |

## Before you start

```bash
docker compose --profile demo run --rm seed
```

A week of history, about 4.3 million readings. If `SELECT count(*) FROM reading;`
returns 0, go back and seed.

Run the queries with `psql` if you have it:

```bash
docker compose exec db psql -U wwtp -d wwtp
```

or with the bundled runner, which needs nothing but Python:

```bash
uv run python tools/sqlrun.py "SELECT 1"
```

## One habit, from the first lesson

**Always know your time window.** Almost every wrong answer in this stage is a
query that was right about the data and wrong about *which* data.

A plant's dissolved oxygen is 2.1 mg/L right now, was 2.4 mg/L during the morning
peak, and averaged 2.0 mg/L over the week. Three true statements. Which one you
need depends on whether you are looking at a trend, a shift report, or a
compliance return — and the query has to say which.

The pattern used throughout:

<!-- check: skip -->
```sql
WHERE ts >= now() - interval '24 hours'
```

and, when you want the last 24 hours *of the data* rather than of the wall clock,
the pattern from [01-04](01-04_time_buckets.md):

<!-- check: skip -->
```sql
WHERE ts >= (SELECT max(ts) FROM reading) - interval '24 hours'
```

The seeded data is in the past, so the second form is the one that shows you
anything.

## A word about the signal ids

Every id in these lessons is a real one from
[`contracts/tags.yaml`](../../contracts/tags.yaml), and they are the kind of thing
you will actually have to type:

```
AREA:HOLDER:MEASUREMENT
AERATION:AHU-1:DO
INFLUENT:FLOW:NH4_IN
EFFLUENT:FLOW:FLOW
```

The middle component is the asset or the grouping, and
[00-02](../00-foundations/00-02_identity_and_values.md) is about why 24 of them
name a grouping rather than an asset.

To find the ids you need:

```sql
SELECT id, unit, range_min, range_max
FROM signal
WHERE id LIKE 'EFFLUENT%'
ORDER BY id;
```

## If a query returns nothing

Almost always one of three things, and in this order of likelihood:

1. **Your time window is empty.** The seeded data ends at seed time. Use
   `(SELECT max(ts) FROM reading)` as the anchor, not `now()`.
2. **The signal id does not exist.** `SELECT id FROM signal ORDER BY id;` and
   look.
3. **The query is wrong.** Which does happen, and is why the exercises ask you to
   predict the answer before running it.
