/**
 * The database pool. Server-side only, and the module says so.
 *
 * `server-only` is a package whose entire content is an error thrown on import
 * from a client bundle. It is here because the alternative is a comment saying
 * "do not import this from a client component", and a comment is not enforced:
 *
 *     Error: You're importing a component that needs "server-only". That
 *     component is only rendered on the server but one of its parents is
 *     rendered with "use client" or the `app` directory.
 *
 * The credential never reaches the browser that way, which is the whole point of
 * this page existing alongside Grafana rather than instead of it. `pg` is a
 * server-side driver with no browser build; the danger is not the library, it is
 * somebody writing a `fetch` to a database URL from a client component because
 * the URL was in `NEXT_PUBLIC_`. `lib/contract.json` holds no credential, and
 * `tests/test_web_page.py` asserts that no `NEXT_PUBLIC_POSTGRES` exists.
 *
 * ## Why a pool and not a client per request
 *
 * Postgres connections are expensive and the connection limit is finite. A
 * server component renders per request, so a client per request is a connection
 * per request, and a dashboard somebody leaves open is a connection leak with a
 * dashboard's shape. The pool is capped at 4 — the page issues at most three
 * concurrent queries per render — because a number chosen to be small is easier
 * to defend than a number chosen to be large.
 */
import 'server-only';

import { Pool } from 'pg';

let pool: Pool | undefined;

export function db(): Pool {
  if (pool) return pool;

  const host = process.env.POSTGRES_HOST;
  const password = process.env.POSTGRES_PASSWORD;
  if (!host || !password) {
    // Thrown rather than defaulted. A default of `localhost` inside a container
    // resolves to the container, so the failure would surface as a connection
    // refused on port 5432 and read as "the database is down" — when in fact the
    // configuration was never supplied. The message names the variables because
    // the person reading it is looking at a log, not at a compose file.
    throw new Error(
      'POSTGRES_HOST and POSTGRES_PASSWORD must be set. This page reads the ' +
        'database server-side and deliberately has no NEXT_PUBLIC_ equivalent: ' +
        'a credential in a client bundle is a credential in every user cache.',
    );
  }

  pool = new Pool({
    host,
    port: Number(process.env.POSTGRES_PORT ?? 5432),
    user: process.env.POSTGRES_USER ?? 'wwtp',
    password,
    database: process.env.POSTGRES_DB ?? 'wwtp',
    max: 4,
    // A dashboard request that waits 10 s for a connection is a dashboard that
    // looks broken. Failing fast turns it into a readable error and leaves the
    // page's other panels to render.
    connectionTimeoutMillis: 10_000,
    // A pooled connection left idle holds a Postgres backend forever otherwise.
    idleTimeoutMillis: 30_000,
  });

  // An idle client erroring (a database restart, a network blip) emits on the
  // pool. Unhandled, that is an unhandled 'error' event and the process dies
  // with no stack trace pointing at the pool.
  pool.on('error', (err) => {
    console.error('wwtp-dashboard: idle postgres client errored', err);
  });

  return pool;
}

/** Run one parameterised query and return its rows. */
export async function query<T>(sql: string, params: unknown[] = []): Promise<T[]> {
  const result = await db().query(sql, params);
  return result.rows as T[];
}

/** Run one query expecting at most one row. */
export async function queryOne<T>(
  sql: string,
  params: unknown[] = [],
): Promise<T | null> {
  const rows = await query<T>(sql, params);
  return rows[0] ?? null;
}
