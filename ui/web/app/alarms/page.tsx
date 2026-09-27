/**
 * What is waiting for a human.
 *
 * Read from the event log, not from `AlarmEngine`, and the reason is worth
 * stating because it was a bug first: the engine holds its state in memory, so
 * a page built from it comes back empty for a plant that is still in fault, and
 * an acknowledgement the engine forgot is an alarm asking to be acknowledged a
 * second time. `alarms/replay.py` rebuilds the same state from the log, and this
 * page reads the same predicate — so the list survives a restart of the engine,
 * of this page, and of Node-RED.
 *
 * It is read-only. Acknowledging is a write to `event` and it belongs to the
 * Node-RED annunciator (`scada/flows/02-annunciator.json`), which has the
 * credential and the audit trail. A second acknowledge path on a page with no
 * authentication would be a way for anyone who can load the page to silence an
 * alarm.
 */
import { ago } from '../../lib/format';
import { outstandingAlarms } from '../../lib/queries';
import { signalById } from '../../lib/types';

export const dynamic = 'force-dynamic';

export default async function AlarmsPage() {
  const alarms = await outstandingAlarms(24);

  return (
    <>
      <h1>Outstanding alarms</h1>
      <p className="lede">
        Critical alarms raised in the last 24 hours that nobody has acknowledged
        and that have not cleared. Built from the <code>event</code> table, so
        this page is correct even if everything that draws it has restarted.
      </p>

      {alarms.length === 0 ? (
        <p className="empty">
          Nothing is waiting for an acknowledgement. Note that an empty list and
          a database that has never run both look like this from here &mdash; the
          header on the overview page distinguishes them by showing how many rows
          exist.
        </p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Raised</th>
              <th>Severity</th>
              <th>Message</th>
              <th>Signal</th>
              <th className="num">Age</th>
            </tr>
          </thead>
          <tbody>
            {alarms.map((a) => {
              const meta = a.signal_id ? signalById(a.signal_id) : undefined;
              return (
                <tr key={`${a.raised_at.toISOString()}-${a.signal_id ?? 'x'}`}>
                  <td>{a.raised_at.toISOString().replace('T', ' ').slice(0, 19)}</td>
                  <td>
                    <span className="tag tag--crit">{a.severity}</span>
                  </td>
                  <td>{a.message}</td>
                  <td className="id">{a.signal_id ?? '—'}</td>
                  <td className="num">{ago(a.age_s)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h2>Acknowledgement</h2>
      <p className="lede">
        Not available here. An acknowledgement is per <em>rule</em> and clears
        when the condition clears, because an operator acknowledges what they can
        see on a panel and a recurrence is a new event deserving a new
        acknowledgement. The write path is the Node-RED annunciator, which is
        where the audit trail and the credential are.
      </p>
    </>
  );
}
