# 00-02 — The six tags, and why every one of them is there

**Previous:** [00-01](00-01_time_series_is_not_relational.md) ·
**Next:** [00-03](00-03_quality_is_data.md)

The six tag columns are not decoration, and they are not a list somebody made up.
Each one answers a question you will actually want to ask.

| Tag | The question it lets you ask |
|---|---|
| `signal` | *Which* measurement — `do_mg_l`, `nh4_out_mg_l` |
| `equipment` | *Which* asset — `AHU-1`, `BLW-2` |
| `area` | Which part of the plant — `AERATION`, `EFFLUENT` |
| `eu` | In what unit — `mg/L`, `m3/h` |
| `site` | Which plant |
| `source` | Which protocol delivered it — `opcua`, `modbus` |

Read that table again and notice the shape of it: five of the six are about
**provenance** — where this number came from and what it means. Only one,
`value`, is a measurement.

### The rule that governs all of it

> **A tag is something that would still be true tomorrow.**

`area=AERATION` is true next week. `value=2.03` is not — it changes every second,
and that is why it is a field.

Put a value in a tag and every distinct reading creates a new series. In
InfluxDB 3 that is fatal in a specific way: a table's tag set is fixed by its first
write, so a tag whose *values* vary is merely expensive, while a tag *key* that
appears later is **rejected outright** with *"Detected a new tag in write."*

### The question

> Every signal in this plant is read twice — once over Modbus TCP and once over
> OPC UA. Show me both readings of dissolved oxygen at the same moment, and prove
> they agree.

### The queries

```sql
SELECT time, source, value
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T00:00:00Z'
  AND time <  '2026-09-26T00:01:00Z'
ORDER BY time;
```

`ORDER BY` accepts `time` and nothing else. `ORDER BY signal` is a parse error that
reads *"invalid ORDER BY, expected TIME column"*.

Now the comparison. **There is a problem, and it is worth meeting head-on:**
InfluxQL has no `CASE` expression, so the pivot idiom every SQL-for-metrics
tutorial is built on — `max(CASE WHEN source = 'opcua' THEN value END)` — is a
parse error here. Verified, and written up in
[`_shared/DIALECT.md`](../_shared/DIALECT.md).

You get **long format** instead, and you will get it a lot:

```sql
SELECT
  time_bucket_nothing,  -- there is no such thing; see below
  source,
  mean(value) AS mean_value
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
GROUP BY time(1m), source
ORDER BY time;
```

which is written, in this dialect, as:

```sql
SELECT time, source, mean(value) AS mean_value
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
GROUP BY time(1m), source
ORDER BY time;
```

and returns one row per minute per protocol, stacked:

```
time                 source   mean_value
2026-09-26T03:14:00Z  modbus   2.0312
2026-09-26T03:14:00Z  opcua    2.0308
2026-09-26T03:15:00Z  modbus   2.0289
2026-09-26T03:15:00Z  opcua    2.0291
```

Note also that the bucket is `GROUP BY time(1m)`, **not** `time_bucket()` in a
`GROUP BY` — this dialect groups by the `time()` clause and expects a bare duration
(`1m`, `1h`, `1d`), not `INTERVAL '1 minute'`. Three things in one sentence that
each cost a parse error to learn.

### Comparing the two

The rows above are adjacent, and comparing them is a job for the client or for
`02-intermediate`. What the query *can* tell you without any reshaping is whether
the two protocols are producing the same number of readings, which is the first
thing to break:

```sql
SELECT time, source, count(*) AS readings, mean(value) AS mean_value
FROM "wwtp"."aeration"
WHERE signal = 'do_mg_l'
  AND time >= '2026-09-26T03:00:00Z'
  AND time <  '2026-09-26T04:00:00Z'
GROUP BY time(1h), source
ORDER BY time;
```

If `readings` differs between the two sources for the same signal, one of them is
dropping points — and you want to know which before you interpret the means.

### What to notice

Now the important part. **The two protocols will not always agree, and when they
disagree the cause is one of exactly three things:**

1. **A word-order bug.** Two of this plant's Modbus registers are low-word-first
   while their neighbours are high-word-first. Read one the wrong way round and
   you get `2.3e-41` — a finite, in-range, *wrong* number. It plots as a flat line
   at zero and reads as a process that has stopped. Nothing in Modbus will ever
   tell you this happened.
2. **A stale reading.** Modbus is polled; OPC UA is subscribed. They are not
   simultaneous.
3. **A real fault.** One of them is reading a broken instrument.

This pair of queries is the cheapest cross-check available for both the gateway and
the Modbus client, and it is why `source` is a tag rather than a comment.

### Exercises

1. Run the comparison over a full hour. Do the two agree everywhere?
2. Restrict to `signal = 'blower_valve_pct'`. This is one of the low-word-first
   traps. What does the `modbus` row show, and what would you conclude if you had
   not known which registers were traps?
3. `eu` is a tag. What would go wrong if it were a field instead?
