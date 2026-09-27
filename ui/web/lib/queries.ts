/**
 * The page's SQL. Every statement in the project that reads plant data is here,
 * and `tests/test_web_page.py` checks three things about each one:
 *
 *   1. it names only signals `contracts/tags.yaml` declares,
 *   2. it runs against a live database, and
 *   3. it touches only the reviewed set of tables.
 *
 * Which is not ceremony. Phase 5a's permit dashboard read pH from the TSS
 * signal: a valid query against a valid table that returned plausible numbers,
 * because a unit of measure is carried by a signal's identity and by nothing on
 * the row. A wrong signal id is a wrong number, never an error. The same commit
 * also wrote `avg(value)` against `reading_1h`, which has `mean` — that one
 * failed loudly, which is the good case.
 *
 * ## `reading_1m`, and why the column is `mean`
 *
 * Trends come from the continuous aggregate, not from `reading`: a week of
 * 4.29 M rows rendered as an SVG path in a browser is a week of data in every
 * visitor's memory. The aggregate has `mean`, `min`, `max` and `n` — not `avg`
 * — and it is `materialized_only`, so a bucket that has not been refreshed is
 * *absent* rather than silently recomputed at a cost nobody agreed to. That is
 * why `trend()` fills gaps with `null` and the sparkline breaks the line, rather
 * than interpolating across a gap it knows nothing about.
 */
import 'server-only';

import { query, queryOne } from './db';

/** One signal's most recent reading, and how long ago it was. */
export interface Latest {
  signal_id: string;
  value: number | null;
  ts: Date | null;
  age_s: number | null;
}

/**
 * The newest reading per signal.
 *
 * `DISTINCT ON (signal_id)` with an `ORDER BY signal_id, ts DESC` — which is the
 * idiom for "latest per group" in Postgres, and is faster than the
 * `GROUP BY … max(ts)` join that looks equivalent and is not.
 *
 * The signal list comes from `lib/contract.json` and is passed as an array
 * parameter, not interpolated: it is generated, so it cannot contain a quote
 * today, and a query that is only safe because of how its input is produced is a
 * query that breaks the first time someone passes something else.
 */
export async function latest(signalIds: string[]): Promise<Map<string, Latest>> {
  const out = new Map<string, Latest>();
  if (signalIds.length === 0) return out;

  const rows = await query<Latest>(
    `SELECT DISTINCT ON (r.signal_id)
            r.signal_id,
            r.value,
            r.ts,
            extract(epoch FROM (now() - r.ts)) AS age_s
       FROM reading r
      WHERE r.signal_id = ANY($1::text[])
      ORDER BY r.signal_id, r.ts DESC`,
    [signalIds],
  );
  for (const row of rows) out.set(row.signal_id, row);
  return out;
}

export interface Point {
  bucket: Date;
  mean: number | null;
  n: number | null;
}

/**
 * One signal's trend, from the 1-minute aggregate.
 *
 * `generate_series` on the left and a `LEFT JOIN` is what produces the `null`
 * gaps. The obvious alternative — query the aggregate and let the client draw
 * what it gets — makes a missing bucket and a flat line indistinguishable, and
 * this project has thirteen signals that report once a week (thread 8), so
 * "no data" and "steady" look identical on every one of them.
 *
 * `n` is carried so the page can distinguish "one reading in this minute" from
 * "sixty". The deadband means a steady signal legitimately has one.
 */
export async function trend(
  signalId: string,
  hours: number,
): Promise<Point[]> {
  return query<Point>(
    `SELECT gs.bucket,
            a.mean,
            a.n
       FROM generate_series(
                date_trunc('minute', now() - make_interval(hours => $2::int)),
                date_trunc('minute', now()),
                INTERVAL '1 minute'
            ) AS gs(bucket)
       LEFT JOIN reading_1m a
              ON a.bucket = gs.bucket
             AND a.signal_id = $1
      ORDER BY gs.bucket`,
    [signalId, Math.max(1, Math.round(hours))],
  );
}

export interface Alarm {
  raised_at: Date;
  severity: string;
  message: string;
  signal_id: string | null;
  age_s: number;
}

/**
 * What is waiting for a human.
 *
 * The same predicate as `alarms/replay.py` and as the Node-RED annunciator's
 * query, and for the same reason: **a `critical` alarm latches**, so the engine
 * never writes an `alarm_cleared` for one, and "not cleared" is therefore not a
 * test for "still on the panel". An alarm leaves the panel because somebody
 * acknowledged it, or because the condition cleared — two independent reasons.
 *
 * Three implementations of one predicate is one too many, and the honest reason
 * they are not one is that they live in three runtimes that cannot import each
 * other. The test asserts they agree.
 */
export async function outstandingAlarms(hours = 24): Promise<Alarm[]> {
  return query<Alarm>(
    `SELECT r.ts                          AS raised_at,
            r.severity,
            r.message,
            r.signal_id,
            extract(epoch FROM (now() - r.ts)) AS age_s
       FROM event r
       LEFT JOIN LATERAL (
            SELECT ack.id, ack.ts
              FROM event ack
             WHERE ack.kind = 'alarm_acknowledged'
               AND ack.detail->>'rule' = r.detail->>'rule'
               AND ack.ts > r.ts
             ORDER BY ack.ts
             LIMIT 1
       ) a ON TRUE
      WHERE r.kind = 'alarm_raised'
        AND r.severity = 'critical'
        AND r.ts >= now() - make_interval(hours => $1::int)
        AND a.id IS NULL
        AND NOT EXISTS (
            SELECT 1 FROM event c
             WHERE c.kind = 'alarm_cleared'
               AND c.detail->>'rule' = r.detail->>'rule'
               AND c.ts > r.ts
        )
      ORDER BY r.ts DESC
      LIMIT 50`,
    [hours],
  );
}

/**
 * The plant's own liveness, for the header.
 *
 * `count(*)` over 4.29 M rows is a full scan, and this runs on every page load.
 * It is here because "how much data is behind this page" is the one number that
 * distinguishes *a healthy plant* from *a database that has never run* — and
 * with `materialized_only` aggregates and a deadband that reports thirteen
 * signals once a week, an empty page is genuinely ambiguous otherwise.
 *
 * A count on the hypertable is a TimescaleDB metadata operation rather than a
 * scan, which is the one place a `count(*)` is free; the same query against
 * `reading_1h` would not be.
 */
export async function dataSpan(): Promise<{ rows: number; from: Date | null; to: Date | null } | null> {
  return queryOne<{ rows: string; from: Date | null; to: Date | null }>(
    `SELECT count(*)::text AS rows, min(ts) AS "from", max(ts) AS "to" FROM reading`,
  ).then((r) => (r ? { rows: Number(r.rows), from: r.from, to: r.to } : null));
}
