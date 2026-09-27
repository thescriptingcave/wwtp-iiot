/**
 * The permit page: the numbers a discharge permit is written against.
 *
 * This exists because the Phase 5a Grafana permit dashboard had a bug that no
 * test could see from the dashboard — it read pH from the *TSS* signal. The
 * query was valid, the table was valid, and it returned a plausible number,
 * because **a unit of measure is carried by a signal's identity and by nothing on
 * the row**. So the list below is generated from the contract, and
 * `tests/test_web_page.py` asserts every signal id in it exists.
 *
 * Which is not the same as saying the page is right. A permit is a legal
 * document and this is a demo plant with synthetic data; nothing here is a
 * compliance record and the footer says so, because a green number on a page
 * that looks like a permit is a claim this project cannot support.
 *
 * ## The first version of this list had two signals that do not exist
 *
 * `EFFLUENT:FLOW:BOD` and `EFFLUENT:FLOW:NH4_IN`. I wrote them from memory of
 * what a permit page usually shows, the page **built and ran**, and rendered
 * "no data" for both — correctly, because there is no such signal and a query
 * for one returns nothing rather than failing.
 *
 * `tests/test_web_page.py::test_every_signal_id_the_pages_name_is_in_the_contract`
 * caught both on its first run against the real source. That is the entire
 * argument for the test existing, in one incident: **the page worked, the
 * numbers were plausible, and two of the five rows were about signals the
 * contract has never heard of.** The real ids are `EFFLUENT:FLOW:NH4` and
 * `EFFLUENT:FLOW:TURBIDITY`.
 */
import { ago, num } from '../../lib/format';
import { latest } from '../../lib/queries';
import { signalById } from '../../lib/types';

export const dynamic = 'force-dynamic';

/**
 * The permit parameters, as signal ids from the contract.
 *
 * A list of *ids* and not of objects, so the query and the limit come from the
 * same place. The first version of this page inlined a value and a label
 * together, which is how the TSS/pH mistake was possible at all: two facts about
 * the same measurement, kept side by side, free to disagree.
 */
interface PermitParameter {
  signal: string;
  /** `null` on a side means "no limit on that side", not zero. */
  limit: { min: number | null; max: number | null };
}

/**
 * Typed explicitly rather than `as const`.
 *
 * `as const` made this a tuple of five *different* literal types, and TypeScript
 * then proved the `'none'` branch below unreachable — by narrowing every
 * candidate to `never`:
 *
 *     app/permit/page.tsx(91,34): error TS2339: Property 'min' does not exist
 *     on type 'never'.
 *
 * Which is the type checker being right about the *current* list and wrong about
 * the code: the branch is what renders when a parameter has no limit at all, and
 * the last entry here (`FLOW`) has neither, so any future edit that reaches it
 * would fail to compile. The explicit interface keeps the branch and loses the
 * error, and the compiler still catches a typo'd signal id — which is the check
 * that matters and the one `tests/test_web_page.py` repeats in Python.
 */
const PERMIT: PermitParameter[] = [
  { signal: 'EFFLUENT:FLOW:PH', limit: { min: 6.0, max: 9.0 } },
  { signal: 'EFFLUENT:FLOW:TSS', limit: { min: null, max: 30.0 } },
  { signal: 'EFFLUENT:FLOW:NH4', limit: { min: null, max: 5.0 } },
  { signal: 'EFFLUENT:FLOW:TURBIDITY', limit: { min: null, max: 12.0 } },
  { signal: 'EFFLUENT:FLOW:FLOW', limit: { min: null, max: null } },
];

export default async function PermitPage() {
  const ids = PERMIT.map((p) => p.signal);
  const latestMap = await latest(ids);

  // Every id resolved, or the page is lying about what it monitors. A missing
  // signal renders as a loud row rather than a silently shorter table, because a
  // permit page that quietly drops a parameter is worse than one that errors.
  const unknown = ids.filter((id) => !signalById(id));

  return (
    <>
      <h1>Discharge permit</h1>
      <p className="lede">
        Synthetic data from a simulated plant. <strong>Not a compliance
        record</strong> — there is no permit, and a green number here is not
        evidence of anything.
      </p>

      {unknown.length > 0 && (
        <p className="empty">
          The contract does not declare{' '}
          {unknown.map((id) => <code key={id}>{id}</code>)}. This is a build
          error in <code>ui/web/app/permit/page.tsx</code> and should be fixed at
          the source.
        </p>
      )}

      <table>
        <thead>
          <tr>
            <th>Parameter</th>
            <th>Signal</th>
            <th className="num">Current</th>
            <th className="num">Permit limit</th>
            <th className="num">Last reported</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {PERMIT.map(({ signal, limit }) => {
            const row = latestMap.get(signal);
            const meta = signalById(signal);
            const value = row?.value ?? null;
            const over =
              value !== null &&
              ((limit.max !== null && value > limit.max) ||
                (limit.min !== null && value < limit.min));
            const range =
              limit.min !== null && limit.max !== null
                ? `${limit.min}–${limit.max}`
                : limit.max !== null
                  ? `≤ ${limit.max}`
                  : limit.min !== null
                    ? `≥ ${limit.min}`
                    : 'none';

            return (
              <tr key={signal}>
                <td>{meta ? meta.label : signal}</td>
                <td className="id">{signal}</td>
                <td className="num">
                  {num(value, meta?.precision ?? 2, meta?.eu)}
                </td>
                <td className="num">
                  {range} {meta?.eu}
                </td>
                <td className="num">{ago(row?.age_s ?? null)}</td>
                <td>
                  {value === null ? (
                    <span className="tag tag--stale">no data</span>
                  ) : over ? (
                    <span className="tag tag--crit">over limit</span>
                  ) : (
                    <span className="tag tag--info">within</span>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      <h2>What this page is not</h2>
      <p className="lede">
        It is not the alarm system. An excursion past a permit limit here is a
        comparison against a number somebody typed into a lesson, not a detected
        fault: <code>alarms/rules.py</code> decides what is wrong from a measured
        healthy distribution and writes an <code>event</code> when it decides.
        And the limits above are illustrative. The engineering ranges in the
        contract are a third thing again &mdash; what the instrument can physically
        read, which OPC UA does not enforce on the wire (thread 2), so showing
        them is the only enforcement an operator gets.
      </p>
    </>
  );
}
