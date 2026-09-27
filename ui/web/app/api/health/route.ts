/**
 * The health endpoint the Dockerfile's HEALTHCHECK hits.
 *
 * It answers a *database* question, not a process question, and that is the
 * whole design. `fetch('http://localhost:3000/')` proves the process bound a
 * socket; it does not prove the page can render, and a page that cannot reach
 * its database is exactly the failure an operator needs told about. So the
 * check runs one trivial query.
 *
 * `SELECT 1` rather than a count over `reading`: the health check runs every 15
 * seconds forever, and `count(*)` over 4.29 M rows is a full scan that turns a
 * liveness probe into load. This is the same mistake as a `count(*)` in a
 * dashboard, and the fix is the same: ask the database whether it is *there*,
 * not how much it holds.
 */
import { NextResponse } from 'next/server';
import { db } from '../../../lib/db';

export const dynamic = 'force-dynamic';

export async function GET() {
  const started = Date.now();
  try {
    await db().query('SELECT 1');
    return NextResponse.json({
      ok: true,
      database: 'reachable',
      ms: Date.now() - started,
    });
  } catch (err) {
    // 503, and the reason. A health check that returns 200 while the thing it
    // monitors is down is a health check that has been disabled by accident.
    return NextResponse.json(
      {
        ok: false,
        database: 'unreachable',
        error: err instanceof Error ? err.message : String(err),
      },
      { status: 503 },
    );
  }
}
