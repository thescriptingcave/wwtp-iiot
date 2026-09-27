/**
 * Overview: every signal, grouped by area, with its trend.
 *
 * A server component. There is no client bundle for the data path, which is the
 * point: the credential is read in this process, the query runs in this process,
 * and nothing about the plant crosses into a browser except the numbers on the
 * page.
 *
 * `export const dynamic = 'force-dynamic'` is not a performance decision. A
 * static build of this page would render once at image-build time, when there is
 * no database, and then serve that frozen render forever — a dashboard showing
 * the build host's view of a plant it has never seen. The default for a page
 * that reads a database is already dynamic; this says so explicitly because the
 * failure mode is invisible.
 */
import { Panel } from '../components/Panel';
import { ago } from '../lib/format';
import { dataSpan, latest, outstandingAlarms, trend } from '../lib/queries';
import { allSignalIds, contract } from '../lib/types';

export const dynamic = 'force-dynamic';

//: How much history each panel draws. Six hours is 360 points per signal at the
//: 1-minute aggregate, which is under 26 000 points for the whole page — small
//: enough to render server-side without a charting library's job-queueing, large
//: enough to see a settle.
const TREND_HOURS = 6;

export default async function OverviewPage() {
  // One `latest()` for every signal rather than one per panel: 57 round trips
  // becomes 1. The per-signal `trend()` calls stay separate because each is a
  // different signal and there is no query that returns 57 trends in one shape
  // without a lateral join per row that costs more than it saves.
  const [latestMap, alarms, span] = await Promise.all([
    latest(allSignalIds),
    outstandingAlarms(24),
    dataSpan(),
  ]);

  const areas = await Promise.all(
    contract.areas.map(async (area) => ({
      area,
      trends: await Promise.all(
        area.signals.map((s) => trend(s.id, TREND_HOURS)),
      ),
    })),
  );

  const silent = [...latestMap.values()].filter((l) => l.age_s === null).length;
  const reporting = latestMap.size - silent;

  // **Say so when the data is older than the window being drawn.**
  //
  // The seeder backfills *up to the moment it ran*, so a seeded plant's newest
  // reading is as old as the seed. An hour later the six-hour trend window
  // contains nothing and every panel is honestly empty — which is correct and
  // indistinguishable, from the reader's chair, from a broken dashboard.
  //
  // The alternative — anchoring the window to the newest reading — would draw a
  // beautiful trend of a window nobody asked for and hide the fact that the
  // plant stopped reporting. So the panels stay empty and the page says why.
  const newestAgeS = span?.to
    ? (Date.now() - span.to.getTime()) / 1000
    : null;
  const staleData = newestAgeS !== null && newestAgeS > TREND_HOURS * 3600;

  return (
    <>
      <h1>Plant overview</h1>
      <p className="lede">
        {contract.counts.signals} signals across {contract.counts.areas} areas,
        generated from the contract. Trends are the 1-minute continuous
        aggregate, so a bucket that has not been refreshed is absent rather than
        guessed at.
      </p>

      {staleData && (
        <p className="empty">
          The newest reading in the database is{' '}
          <strong>{ago(newestAgeS)}</strong>, which is older than the{' '}
          {TREND_HOURS}-hour window these panels draw — so they are empty. That
          is the honest rendering: a panel that quietly slid its window back to
          the last data would look alive and be answering a question nobody
          asked. If the gateway is running, the readings will appear within a
          scan.
        </p>
      )}

      <div className="status">
        <span className="status__item">
          reading rows<b>{span ? span.rows.toLocaleString() : '—'}</b>
        </span>
        <span className="status__item">
          since<b>{span?.from ? ago((Date.now() - span.from.getTime()) / 1000) : '—'}</b>
        </span>
        <span className="status__item">
          newest<b>{span?.to ? ago((Date.now() - span.to.getTime()) / 1000) : '—'}</b>
        </span>
        <span className="status__item">
          reporting<b>
            {reporting}/{contract.counts.signals}
          </b>
        </span>
        <span className="status__item">
          waiting for a human<b>{alarms.length}</b>
        </span>
      </div>

      {alarms.length > 0 && (
        <p className="lede">
          <a href="/alarms">{alarms.length} critical alarm(s) outstanding</a> — the
          list is built from the event log, so it survives a restart of anything
          that draws it.
        </p>
      )}

      {areas.map(({ area, trends }) => (
        <section key={area.id}>
          <h2>{area.name}</h2>
          <div className="grid">
            {area.signals.map((signal, i) => (
              <Panel
                key={signal.id}
                signal={signal}
                latest={latestMap.get(signal.id)}
                points={trends[i]}
              />
            ))}
          </div>
        </section>
      ))}
    </>
  );
}
