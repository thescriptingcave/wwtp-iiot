# 00 — Foundations

**The goal of this stage:** stop thinking of a time-series table as a normal table
with a timestamp column, and start seeing why that difference changes almost every
query you would write.

Three lessons. They are short, and they are not optional.

---

## 00-01 — A time-series table is not a normal table

Read `sql/_shared/schema.sql` and look at one table:

```sql
CREATE TABLE IF NOT EXISTS "wwtp"."aeration" (
  "time"     TIMESTAMP NOT NULL,
  "area"     TEXT NOT NULL,
  "equipment" TEXT NOT NULL,
  "signal"   TEXT NOT NULL,
  "eu"       TEXT NOT NULL,
  "site"     TEXT NOT NULL,
  "source"   TEXT NOT NULL,
  "value"    DOUBLE,
  "quality"  BIGINT NOT NULL,
  PRIMARY KEY ("time", "area", "equipment", "signal", "eu", "site", "source"),
);
```

Nine columns, and only one of them is a measurement.

Six are **tags**: `area`, `equipment`, `signal`, `eu`, `site`, `source`. They are
identity. They answer *which* reading, not *what* it was. `area=AERATION,
equipment=AHU-1, signal=do_mg_l, eu=mg/L, site=PLANT-A, source=opcua` describes the
dissolved-oxygen probe in the aeration tank of this plant, as read over OPC UA.

One is a **field**: `value`. And one is the **validity of that field**: `quality`.

That is the whole shape. Everything else in this course is a consequence.

### The question

> How many distinct *things* are being measured in this table, and how many
> readings of each do you have?

### The queries

```sql
-- How many readings, in total?
SELECT COUNT(*) FROM "wwtp"."aeration";

-- How many distinct signals are being measured?
SELECT COUNT(DISTINCT signal) FROM "wwtp"."aeration";

-- Which ones?
SELECT signal, eu, COUNT(*) AS readings
FROM "wwtp"."aeration"
GROUP BY time(1d), signal, eu
ORDER BY time;
```

### What to notice

(You will notice `GROUP BY time(1d), signal, eu` where a relational database
would want neither the bucket nor the tag. That is InfluxQL: `ORDER BY` accepts
`time` and nothing else, and every grouped dimension must be named. See
[`_shared/DIALECT.md`](../_shared/DIALECT.md).)

`COUNT(*)` and `COUNT(DISTINCT signal)` differ by a factor of thousands. That ratio
*is* the sample rate. A historian that stores every scan of a 50 Hz PLC at 1 Hz
holds one reading in fifty; the other forty-nine were filtered by the deadband
because the value had not changed enough to be news.

**This is not waste.** It is the difference between a historian you can query and
one you cannot. And it is why lesson 01-04 exists.

### Exercises

1. Do the same for `effluent`. Which stage is sampled most densely?
2. `SELECT COUNT(DISTINCT source) FROM "wwtp"."aeration";` — what do you expect,
   and why is the answer a number you care about?
3. What is the *earliest* reading in the table? (You will need a function you
   have not met. Guess its name, then check `00-02`.)
